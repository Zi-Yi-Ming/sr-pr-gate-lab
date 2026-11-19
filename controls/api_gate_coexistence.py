"""Exercise an API-published prior Gate and native qualification in the lab."""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import lab
from controls.pr_event_gate import checked_revision


PUBLISHER_REF = "refs/heads/lab/pr-event-api-publisher"
PRIOR_LABEL = "lab-api-prior-qualification"
SEED_WORKFLOW = ".github/workflows/pr-event-api-seed.yml"
PRIOR_WORKFLOW = ".github/workflows/pr-event-api-prior.yml"


def branch_refs(case):
    return f"lab-target/pr-event-api-{case}", f"lab/pr-event-api-{case}"


def run_contracts(deferred, fixture):
    contracts = []
    for contract in lab.CONTRACTS:
        if contract in deferred:
            status, returncode = "deferred", None
        else:
            code = lab.FAIL_CODE if contract == "automatic" and fixture == "broken" else lab.PASS_CODE
            execution = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, timeout=30)
            returncode = execution.returncode
            status = "passed" if returncode == 0 else "failed"
        contracts.append({"id": contract, "status": status, "returncode": returncode})
    return contracts


def evaluate(case, plan):
    lab.require(case in lab.CASES and type(plan) is dict and set(plan) == {"approved", "fixture"},
                "Invalid current fixture plan")
    deferred = lab.deferred_for(case)
    expected_grants = [contract for contract in lab.CONTRACTS[1:] if contract not in deferred]
    expected_fixture = "broken" if case == "failure" else "ready"
    lab.require(plan["approved"] == expected_grants and plan["fixture"] == expected_fixture,
                "Fixture does not match the selected authorization or failure control")
    contracts = run_contracts(deferred, plan["fixture"])
    return {"contracts": contracts, "verdict": lab.derive_status(contracts)}


def experiment_event(stage):
    pairs = {branch_refs(case): case for case in lab.CASES}
    event, merge = checked_revision(pairs)
    pull = event["pull_request"]
    action = {"seed": "opened", "prior": "labeled", "current": "ready_for_review"}[stage]
    lab.require(event.get("action") == action and pull.get("draft") is (stage != "current"),
                "Unexpected experiment action or draft state")
    if stage == "prior":
        lab.require(event.get("label", {}).get("name") == PRIOR_LABEL, "Incorrect prior qualification label")
    lab.require(lab.positive(pull.get("number")), "Missing PR number")
    identity = lab.source_identity(os.environ)
    receipt = {"pr": pull["number"], "head": pull["head"]["sha"], "base": pull["base"]["sha"], "merge": merge,
               "run": int(identity["source_run_id"]), "attempt": int(identity["source_run_attempt"])}
    return pairs[(pull["base"]["ref"], pull["head"]["ref"])], receipt


def manifest(value):
    required = {"case", "pr", "head", "base", "merge", "seed_run", "prior_run", "prior_attempt"}
    lab.require(type(value) is dict and set(value) == required and value["case"] in lab.CASES,
                "Invalid publisher manifest")
    lab.require(all(type(value[key]) is str and lab.SHA_RE.fullmatch(value[key]) for key in ("head", "base", "merge")),
                "Invalid frozen commit identity")
    lab.require(len({value["head"], value["base"], value["merge"]}) == 3, "Frozen commits must be distinct")
    lab.require(all(lab.positive(value[key]) for key in ("pr", "seed_run", "prior_run", "prior_attempt")),
                "Invalid PR, run or attempt identity")
    lab.require(value["seed_run"] != value["prior_run"], "Seed and qualification must be distinct runs")
    return value


def prior_payload(intent):
    run = intent["prior_run"]
    return {"name": "PR Gate", "head_sha": intent["head"], "status": "completed", "conclusion": "success",
            "external_id": f"semantic-router-ci:{intent['pr']}:{run}:{intent['prior_attempt']}",
            "details_url": f"https://github.com/{lab.REPOSITORY}/actions/runs/{run}",
            "output": {"title": "Lab prior qualification passed",
                       "summary": "Only the earlier lab qualification. The current native Gate runs separately."}}


class PublisherAPI:
    def __init__(self, intent, env):
        self.intent = manifest(intent)
        self.env = env
        lab.require(env.get("GITHUB_REPOSITORY") == lab.REPOSITORY and env.get("GH_TOKEN")
                    and env.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
                    and env.get("GITHUB_REF") == PUBLISHER_REF and env.get("GITHUB_SHA") == intent["base"]
                    and env.get("PRIOR_RUN_ID") == str(intent["prior_run"]), "Publisher context does not match frozen manifest")

    def allows(self, method, path, data):
        i = self.intent
        if method == "POST":
            return path == lab.PREFIX + "/check-runs" and data == prior_payload(i)
        allowed = {f"/pulls/{i['pr']}", f"/actions/runs/{i['seed_run']}", f"/actions/runs/{i['prior_run']}"}
        for kind in ("all", "latest"):
            allowed.add(f"/commits/{i['head']}/check-runs?filter={kind}&per_page=100")
        route = path.removeprefix(lab.PREFIX)
        return method == "GET" and data is None and path.startswith(lab.PREFIX + "/") and (
            route in allowed or re.fullmatch(r"/check-suites/[1-9][0-9]*", route) is not None
        )

    def request(self, method, path, data=None):
        lab.require(self.allows(method, path, data), "Request is outside the experiment publisher allowlist")
        args = ["gh", "api", "--method", method, "--include", path.lstrip("/")]
        if data is not None:
            args.extend(["--input", "-"])
        result = subprocess.run(args, input=json.dumps(data) if data is not None else None, env=self.env,
                                capture_output=True, text=True, encoding="utf-8", timeout=40)
        header, separator, body = result.stdout.partition("\n\n")
        match = re.match(r"HTTP/\S+\s+(\d{3})", header)
        lab.require(separator and match is not None, "API transport failed without retry")
        return int(match.group(1)), lab.parse_json(body)


def verify_source(intent, receipt, client):
    expected = {key: intent[key] for key in ("pr", "head", "base", "merge")}
    expected.update(run=intent["prior_run"], attempt=intent["prior_attempt"])
    lab.require(type(receipt) is dict and all(receipt.get(k) == v for k, v in expected.items())
                and all(lab.positive(receipt.get(k)) for k in ("pr", "run", "attempt")), "Prior receipt identity mismatch")
    expected_contracts = [{"id": key, "status": "passed", "returncode": 0} for key in lab.CONTRACTS]
    contracts = receipt.get("contracts")
    lab.require(contracts == expected_contracts and all(type(c["returncode"]) is int for c in contracts),
                "Prior qualification must contain all passing execution receipts")

    def verified_suite(run_id, path, attempt, conclusion):
        run = lab.read(client, f"{lab.PREFIX}/actions/runs/{run_id}")
        lab.require((run.get("id"), run.get("path"), run.get("run_attempt"), run.get("conclusion"))
                    == (run_id, path, attempt, conclusion) and run.get("status") == "completed"
                    and lab.positive(run.get("id")) and lab.positive(run.get("run_attempt"))
                    and run.get("event") == "pull_request" and run.get("head_sha") == intent["head"]
                    and run.get("head_branch") == branch_refs(intent["case"])[1]
                    and all(run.get(key, {}).get("full_name") == lab.REPOSITORY
                            for key in ("repository", "head_repository"))
                    and [p.get("number") for p in run.get("pull_requests", [])] == [intent["pr"]],
                    "Run does not match the frozen qualification or seed identity")
        suite_id = run.get("check_suite_id")
        lab.require(lab.positive(suite_id), "Run has no identifiable suite")
        suite = lab.read(client, f"{lab.PREFIX}/check-suites/{suite_id}")
        lab.require((suite.get("id"), suite.get("head_sha"), suite.get("status"), suite.get("conclusion"))
                    == (suite_id, intent["head"], "completed", conclusion)
                    and suite.get("app", {}).get("id") == lab.APP_ID, "Suite does not match the recorded run conclusion")
        return suite_id

    seed = verified_suite(intent["seed_run"], SEED_WORKFLOW, 1, "cancelled")
    prior = verified_suite(intent["prior_run"], PRIOR_WORKFLOW, intent["prior_attempt"], "success")
    lab.require(seed != prior, "Seed and prior qualification did not have distinct suites")
    return seed, prior


def check_identity(check):
    keys = ("id", "node_id", "name", "status", "conclusion", "head_sha", "external_id")
    return {**{key: check.get(key) for key in keys}, "app": check.get("app", {}).get("id"),
            "suite": check.get("check_suite", {}).get("id")}


def publish_prior(intent, receipt, client):
    intent = manifest(intent)
    seed, prior = verify_source(intent, receipt, client)
    existing = lab.checks_at(client, intent["head"], "all")
    lab.require(not any(check.get("name") == "PR Gate" for check in existing), "A PR Gate already exists")
    pull = lab.read(client, f"{lab.PREFIX}/pulls/{intent['pr']}")
    lab.require(pull.get("number") == intent["pr"] and pull.get("state") == "open" and pull.get("draft") is True
                and pull.get("merged") is False and pull.get("merge_commit_sha") == intent["merge"],
                "PR must remain the frozen draft")
    for key, ref in zip(("base", "head"), branch_refs(intent["case"])):
        side = pull.get(key, {})
        lab.require((side.get("ref"), side.get("sha"), side.get("repo", {}).get("full_name"))
                    == (ref, intent[key], lab.REPOSITORY), "PR branch, revision or repository changed")

    payload = prior_payload(intent)
    status, check = lab.call(client, "POST", lab.PREFIX + "/check-runs", payload)
    assigned = check.get("check_suite", {}).get("id")
    lab.require(status == 201 and lab.positive(check.get("id")) and lab.positive(assigned)
                and type(check.get("node_id")) is str and bool(check["node_id"])
                and check.get("app", {}).get("id") == lab.APP_ID
                and all(check.get(key) == payload[key] for key in ("name", "head_sha", "external_id", "status", "conclusion")),
                "API did not return the expected prior Gate receipt")
    fingerprint = check_identity(check)
    all_visible, latest_visible = (
        any(check_identity(item) == fingerprint for item in lab.checks_at(client, intent["head"], scope))
        for scope in ("all", "latest")
    )
    suite = lab.read(client, f"{lab.PREFIX}/check-suites/{assigned}")
    lab.require(suite.get("id") == assigned and suite.get("head_sha") == intent["head"]
                and suite.get("app", {}).get("id") == lab.APP_ID, "Assigned suite does not match the check receipt")
    return {"intent": intent, "seed_suite": seed, "prior_suite": prior, "check": fingerprint,
            "matched_cancelled_suite": assigned == seed and (suite.get("status"), suite.get("conclusion")) == ("completed", "cancelled"),
            "visible_in_all": all_visible, "visible_in_latest": latest_visible}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("seed", "prior", "current", "publish"))
    parser.add_argument("--receipt", type=Path)
    cli = parser.parse_args()
    if cli.stage == "publish":
        intent = manifest(lab.parse_json(os.environ["LAB_MANIFEST"]))
        receipt = lab.parse_json(cli.receipt.read_text(encoding="utf-8"))
        evidence = publish_prior(intent, receipt, PublisherAPI(intent, os.environ))
        print(json.dumps(evidence, sort_keys=True))
        return 0 if evidence["matched_cancelled_suite"] and evidence["visible_in_latest"] and evidence["visible_in_all"] else 1

    case, receipt = experiment_event(cli.stage)
    if cli.stage == "seed":
        print(json.dumps(receipt, sort_keys=True))
    elif cli.stage == "prior":
        lab.require(Path("seed-qualification.txt").read_bytes() == b"ready\n", "Prior qualification fixture failed")
        receipt["contracts"] = run_contracts((), "ready")
        lab.require(lab.derive_status(receipt["contracts"]) == "success", "Prior qualification did not pass")
        cli.receipt.parent.mkdir(parents=True, exist_ok=True)
        cli.receipt.write_text(json.dumps(receipt, sort_keys=True) + "\n", encoding="utf-8")
    else:
        result = evaluate(case, lab.parse_json(Path("qualification.json").read_text(encoding="utf-8")))
        print(json.dumps({**receipt, **result}, sort_keys=True))
        return 0 if result["verdict"] == "success" else 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (lab.LabError, KeyError, TypeError, ValueError, OSError, subprocess.SubprocessError, AttributeError) as exc:
        sys.exit(f"Lab experiment input or evidence rejected: {exc}")
