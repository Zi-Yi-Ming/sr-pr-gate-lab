"""Synthetic, lab-only PR Gate calibration. No retries, comments, or admin APIs."""
import argparse
import copy
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.error
import urllib.request

REPOSITORY = "Zi-Yi-Ming/sr-pr-gate-lab"
PREFIX = "/repos/" + REPOSITORY
APP_ID = 15368
CASES = ("waiting", "partial", "failure", "success")
CONTRACTS = ("automatic", "expensive-a", "expensive-b")
SHA_RE = re.compile(r"[0-9a-fA-F]{40}\Z")
POSITIVE_RE = re.compile(r"[1-9][0-9]*\Z")
GRAPHQL_QUERY = '''query LabProbe($node: ID!, $pr: Int!, $head: GitObjectID!) {
  check: node(id: $node) { ... on CheckRun { id isRequired(pullRequestNumber: $pr) } }
  repository(owner: "Zi-Yi-Ming", name: "sr-pr-gate-lab") {
    pullRequest(number: $pr) { headRefOid }
    object(oid: $head) { ... on Commit { oid statusCheckRollup { state } } }
  }
}'''
PASS_CODE = 'import sys; print("LAB synthetic fixture"); sys.exit(0)'
FAIL_CODE = 'import sys; print("LAB synthetic fixture"); print("synthetic failure", file=sys.stderr); sys.exit(1)'


class LabError(Exception):
    """Evidence is invalid or insufficient; never a passing negative probe."""
    outcome = "INCONCLUSIVE"


class PermissionError(LabError):
    def __init__(self, status, body):
        super().__init__("permission error (HTTP " + str(status) + ")")
        self.status, self.body = status, body


class UnsafeError(LabError):
    outcome = "UNSAFE"


def require(condition, message):
    if not condition:
        raise LabError(message)


def positive(value):
    return type(value) is int and value > 0


def parse_json(raw):
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "duplicate JSON object key")
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=unique_object)


def validate_manifest(manifest):
    require(type(manifest) is list and len(manifest) == 4, "manifest must contain exactly four cases")
    prs, heads, bases = set(), set(), set()
    for case, name in zip(manifest, CASES):
        require(type(case) is dict and set(case) == {"case_id", "pr", "head", "base"}, "invalid manifest case keys")
        require(case["case_id"] == name, "manifest cases must be waiting,partial,failure,success in order")
        require(positive(case["pr"]), "PR must be a positive integer")
        require(all(type(case[k]) is str and SHA_RE.fullmatch(case[k]) for k in ("head", "base")), "invalid frozen SHA")
        prs.add(case["pr"]); heads.add(case["head"].lower()); bases.add(case["base"].lower())
    require(len(prs) == 4 and len(heads) == 4 and len(bases) == 1, "PR/head must be unique and base must be shared")
    return manifest


def source_identity(env):
    require(env.get("GITHUB_REPOSITORY") == REPOSITORY, "GITHUB_REPOSITORY must be the lab repository")
    for key in ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT"):
        require(type(env.get(key)) is str and POSITIVE_RE.fullmatch(env[key]), "missing or invalid " + key)
    result = {"source_run_id": env["GITHUB_RUN_ID"], "source_run_attempt": env["GITHUB_RUN_ATTEMPT"]}
    if env.get("GITHUB_SHA"):
        require(SHA_RE.fullmatch(env["GITHUB_SHA"]), "invalid workflow source SHA")
        result["source_workflow_sha"] = env["GITHUB_SHA"]
    return result


def deferred_for(case_id):
    return list(CONTRACTS[1:]) if case_id == "waiting" else (["expensive-b"] if case_id == "partial" else [])


def derive_status(contracts):
    if any(c["status"] == "failed" for c in contracts):
        return "failure"
    if any(c["status"] == "deferred" for c in contracts):
        return "awaiting-approval"
    return "success"


def qualify(env=None):
    env = os.environ if env is None else env
    identity = source_identity(env)
    try:
        manifest = validate_manifest(parse_json(env.get("LAB_MANIFEST", "null")))
    except (ValueError, TypeError) as exc:
        raise LabError("LAB_MANIFEST must be valid JSON") from exc
    result = {"schema_version": 1, "kind": "qualification", "repository": REPOSITORY,
              **identity, "manifest": copy.deepcopy(manifest), "qualifications": []}
    for case in manifest:
        deferred = deferred_for(case["case_id"])
        contracts = []
        for contract in CONTRACTS:
            if contract in deferred:
                item = {"id": contract, "status": "deferred", "execution": None}
            else:
                code = FAIL_CODE if case["case_id"] == "failure" and contract == "automatic" else PASS_CODE
                try:
                    completed = subprocess.run([sys.executable, "-I", "-c", code], shell=False,
                                               capture_output=True, text=True, timeout=30, check=False)
                    execution = {"returncode": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr}
                    item = {"id": contract, "status": "passed" if completed.returncode == 0 else "failed", "execution": execution}
                except (OSError, subprocess.SubprocessError):
                    item = {"id": contract, "status": "failed", "execution": {"returncode": None,
                            "stdout": "", "stderr": "fixed synthetic subprocess failed to execute"}}
            contracts.append(item)
        result["qualifications"].append({**case, "required_contracts": list(CONTRACTS), "contracts": contracts,
                                         "deferred": deferred, "gate_status": derive_status(contracts)})
    return result


def validate_qualification(document, env=None):
    require(type(document) is dict, "qualification must be an object")
    expected_keys = {"schema_version", "kind", "repository", "source_run_id", "source_run_attempt", "manifest", "qualifications"}
    require(set(document) in (expected_keys, expected_keys | {"source_workflow_sha"}), "invalid qualification keys")
    require(type(document["schema_version"]) is int and document["schema_version"] == 1
            and document["kind"] == "qualification" and document["repository"] == REPOSITORY,
            "invalid qualification provenance")
    for key in ("source_run_id", "source_run_attempt"):
        require(type(document[key]) is str and POSITIVE_RE.fullmatch(document[key]), "invalid qualification run binding")
    if "source_workflow_sha" in document:
        require(type(document["source_workflow_sha"]) is str and SHA_RE.fullmatch(document["source_workflow_sha"]), "invalid source workflow SHA")
    if env is not None:
        current = source_identity(env)
        require(all(document.get(key) == value for key, value in current.items()), "qualification does not match publishing run/attempt/SHA")
        require(("source_workflow_sha" in document) == ("source_workflow_sha" in current), "source workflow SHA binding missing")
    manifest = validate_manifest(document["manifest"])
    require(type(document["qualifications"]) is list and len(document["qualifications"]) == 4, "four qualifications required")
    for case, qualification in zip(manifest, document["qualifications"]):
        require(type(qualification) is dict and set(qualification) == set(case) | {"required_contracts", "contracts", "deferred", "gate_status"},
                "invalid qualification case keys")
        require(all(qualification[k] == v for k, v in case.items()), "qualification case binding differs from manifest")
        require(qualification["required_contracts"] == list(CONTRACTS), "required contracts were changed")
        contracts = qualification["contracts"]
        require(type(contracts) is list and len(contracts) == 3, "all required contract results must be retained")
        for contract, contract_id in zip(contracts, CONTRACTS):
            require(type(contract) is dict and set(contract) == {"id", "status", "execution"} and contract["id"] == contract_id,
                    "invalid contract result")
            if contract_id in deferred_for(case["case_id"]):
                require(contract["status"] == "deferred" and contract["execution"] is None, "deferred contract cannot be upgraded")
            else:
                execution = contract["execution"]
                require(type(execution) is dict and set(execution) == {"returncode", "stdout", "stderr"}, "execution receipt required")
                require(all(type(execution[k]) is str for k in ("stdout", "stderr")), "invalid process output")
                rc = execution["returncode"]
                require(rc is None or type(rc) is int, "invalid returncode")
                require(contract["status"] == ("passed" if type(rc) is int and rc == 0 else "failed"), "process status contradicts returncode")
                expected_rc = 1 if case["case_id"] == "failure" and contract_id == "automatic" else 0
                require(rc == expected_rc, "synthetic fixture did not produce its fixed expected exit code")
        require(qualification["deferred"] == deferred_for(case["case_id"]), "deferred contracts differ from synthetic fixture")
        require(qualification["gate_status"] == derive_status(contracts), "gate status contradicts execution/deferred evidence")
    return document


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GitHubAPI:
    """One-shot API calls, constrained to fixed lab endpoints and static GraphQL."""
    def __init__(self, env=None):
        env = os.environ if env is None else env
        require(env.get("GITHUB_REPOSITORY") == REPOSITORY, "GITHUB_REPOSITORY must be the lab repository")
        require(bool(env.get("GH_TOKEN")), "GH_TOKEN is required")
        self._token = env["GH_TOKEN"]
        self._repository = env["GITHUB_REPOSITORY"]
        self._opener = urllib.request.build_opener(NoRedirect())

    def request(self, method, path, data=None):
        require(self._repository == REPOSITORY, "repository binding changed")
        read_patterns = (r"/pulls/[1-9][0-9]*", r"/check-runs/[1-9][0-9]*", r"/check-suites/[1-9][0-9]*",
                         r"/commits/[0-9a-fA-F]{40}/check-runs\?filter=(all|latest)&per_page=100")
        allowed = method == "GET" and any(re.fullmatch(re.escape(PREFIX) + p, path) for p in read_patterns)
        allowed |= method == "POST" and path == PREFIX + "/check-runs"
        allowed |= method == "PUT" and bool(re.fullmatch(re.escape(PREFIX) + r"/pulls/[1-9][0-9]*/merge", path))
        if method == "POST" and path == "/graphql":
            allowed = (type(data) is dict and set(data) == {"query", "variables"} and data["query"] == GRAPHQL_QUERY
                       and type(data["variables"]) is dict and set(data["variables"]) == {"node", "pr", "head"}
                       and type(data["variables"]["node"]) is str and positive(data["variables"]["pr"])
                       and type(data["variables"]["head"]) is str and bool(SHA_RE.fullmatch(data["variables"]["head"])))
        require(allowed, "API method/path/query is outside the lab allowlist")
        body = None if data is None else json.dumps(data).encode("utf-8")
        request = urllib.request.Request("https://api.github.com" + path, data=body, method=method, headers={
            "Authorization": "Bearer " + self._token, "Accept": "application/vnd.github+json",
            "Content-Type": "application/json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "sr-pr-gate-lab"})
        try:
            with self._opener.open(request, timeout=30) as response:
                status, raw = response.status, response.read()
        except urllib.error.HTTPError as exc:
            status, raw = exc.code, exc.read()
        except (urllib.error.URLError, OSError) as exc:
            raise LabError("API transport failed without retry") from exc
        try:
            parsed = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            parsed = {"message": "API returned a non-JSON body"}
        if status in (401, 403):
            raise PermissionError(status, parsed)
        return status, parsed


def call(client, method, path, data=None):
    status, body = client.request(method, path, data)
    if status in (401, 403):
        raise PermissionError(status, body)
    return status, body


def read(client, path):
    status, body = call(client, "GET", path)
    require(status == 200 and type(body) is dict, "API read did not return HTTP 200 object")
    return body


def validate_pr(body, case, merged=False):
    require(body.get("number") == case["pr"] and body.get("draft") is False, "PR identity/draft mismatch")
    require(body.get("head", {}).get("sha") == case["head"], "PR head differs from frozen SHA")
    base = body.get("base", {})
    require(base.get("ref") == "main" and base.get("repo", {}).get("full_name") == REPOSITORY,
            "PR base is not lab main")
    if merged:
        require(body.get("state") == "closed" and body.get("merged") is True and bool(body.get("merged_at")), "merged PR was not confirmed")
    else:
        require(base.get("sha") == case["base"], "PR base differs from frozen SHA")
        require(body.get("state") == "open" and body.get("merged") is False and not body.get("merged_at"), "PR is no longer open/unmerged")


def external_id(document, case):
    return "sr-pr-gate-lab:basic:" + str(case["pr"]) + ":" + document["source_run_id"] + ":" + document["source_run_attempt"]


def expected_state(qualification):
    gate = qualification["gate_status"]
    return ("in_progress", None) if gate == "awaiting-approval" else ("completed", gate)


def validate_check(check, document, qualification):
    status, conclusion = expected_state(qualification)
    require(type(check) is dict and check.get("name") == "PR Gate" and check.get("head_sha") == qualification["head"]
            and check.get("external_id") == external_id(document, qualification), "Gate check identity mismatch")
    require(check.get("app", {}).get("id") == APP_ID, "Gate check must be owned by GitHub Actions App 15368")
    require(check.get("status") == status and check.get("conclusion") == conclusion, "Gate receipt state contradicts qualification")
    require(positive(check.get("id")) and type(check.get("node_id")) is str and bool(check["node_id"])
            and positive(check.get("check_suite", {}).get("id")), "incomplete check receipt")


def checks_at(client, head, filter_name):
    body = read(client, PREFIX + "/commits/" + head + "/check-runs?filter=" + filter_name + "&per_page=100")
    runs, count = body.get("check_runs"), body.get("total_count")
    require(type(runs) is list and type(count) is int and 0 <= count <= 100 and len(runs) == count,
            "check list is truncated or malformed; refusing unbounded pagination")
    require(all(type(c) is dict for c in runs), "invalid check list item")
    return runs


def publish(document, client, env=None):
    report = {"schema_version": 1, "kind": "publication", "repository": REPOSITORY,
              "qualification": copy.deepcopy(document), "receipts": [], "all_passed": False}
    try:
        validate_qualification(document, env)
        for case, qualification in zip(document["manifest"], document["qualifications"]):
            validate_pr(read(client, PREFIX + "/pulls/" + str(case["pr"])), case)
            existing = checks_at(client, case["head"], "all")
            require(not any(c.get("external_id") == external_id(document, case) for c in existing),
                    "same external_id already exists; use probe-only with the original publication artifact")
            status, conclusion = expected_state(qualification)
            payload = {"name": "PR Gate", "head_sha": case["head"], "external_id": external_id(document, case), "status": status,
                       "output": {"title": "LAB synthetic qualification: " + case["case_id"],
                                  "summary": "Synthetic lab fixtures only; this is not production /ci run evidence. Gate: " + qualification["gate_status"]}}
            if conclusion is not None:
                payload["conclusion"] = conclusion
            http_status, check = call(client, "POST", PREFIX + "/check-runs", payload)
            report["receipts"].append({"case_id": case["case_id"], "http_status": http_status, "check": check})
            require(http_status == 201, "check creation did not return HTTP 201")
            validate_check(check, document, qualification)
        report["all_passed"] = True
    except Exception as exc:
        report["error"] = str(exc)
        if isinstance(exc, PermissionError):
            report["permission_error"] = {"status": exc.status, "body": exc.body}
    return report


def validate_publication(publication):
    require(type(publication) is dict and publication.get("schema_version") == 1 and publication.get("kind") == "publication"
            and publication.get("repository") == REPOSITORY and publication.get("all_passed") is True and "error" not in publication,
            "publication is incomplete or untrusted")
    document = validate_qualification(publication.get("qualification"))
    receipts = publication.get("receipts")
    require(type(receipts) is list and len(receipts) == 4, "four publication receipts required")
    ids, nodes = set(), set()
    for qualification, receipt in zip(document["qualifications"], receipts):
        require(type(receipt) is dict and receipt.get("case_id") == qualification["case_id"] and receipt.get("http_status") == 201,
                "receipt case/order/status mismatch")
        validate_check(receipt.get("check"), document, qualification)
        ids.add(receipt["check"]["id"]); nodes.add(receipt["check"]["node_id"])
    require(len(ids) == 4 and len(nodes) == 4, "check IDs must be unique")
    return document


def same_check(actual, receipt, document, qualification):
    validate_check(actual, document, qualification)
    require(all(actual.get(key) == receipt.get(key) for key in ("id", "node_id", "check_suite")), "check receipt identity changed")


def probe(publication, client):
    report = {"schema_version": 1, "kind": "probe", "repository": REPOSITORY, "cases": [], "all_passed": False}
    current = None
    try:
        document = validate_publication(publication)
        report.update({key: value for key, value in document.items() if key.startswith("source_")})
        for case, qualification, receipt in zip(document["manifest"], document["qualifications"], publication["receipts"]):
            current = {"case_id": case["case_id"], "outcome": "INCONCLUSIVE", "evidence": {}}
            report["cases"].append(current)
            evidence, expected = current["evidence"], receipt["check"]
            pull_path = PREFIX + "/pulls/" + str(case["pr"])
            evidence["pr_before"] = read(client, pull_path)
            validate_pr(evidence["pr_before"], case)
            evidence["exact_check"] = read(client, PREFIX + "/check-runs/" + str(expected["id"]))
            same_check(evidence["exact_check"], expected, document, qualification)
            for filter_name in ("all", "latest"):
                runs = checks_at(client, case["head"], filter_name)
                evidence[filter_name + "_checks"] = runs
                matching = [c for c in runs if c.get("id") == expected["id"]]
                require(len(matching) == 1, "Gate check is not uniquely visible in " + filter_name)
                same_check(matching[0], expected, document, qualification)
            status, graphql = call(client, "POST", "/graphql", {"query": GRAPHQL_QUERY, "variables": {
                "node": expected["node_id"], "pr": case["pr"], "head": case["head"]}})
            evidence["graphql"] = {"status": status, "body": graphql}
            require(status == 200 and type(graphql) is dict and not graphql.get("errors"), "GraphQL evidence unavailable")
            data = graphql.get("data", {})
            require(data.get("check", {}).get("id") == expected["node_id"] and data["check"].get("isRequired") is True,
                    "exact Gate check is not confirmed required")
            repository = data.get("repository", {})
            require(repository.get("pullRequest", {}).get("headRefOid") == case["head"]
                    and repository.get("object", {}).get("oid") == case["head"], "GraphQL head binding mismatch")
            if case["case_id"] == "success":
                evidence["suite"] = read(client, PREFIX + "/check-suites/" + str(expected["check_suite"]["id"]))
                suite = evidence["suite"]
                require(suite.get("id") == expected["check_suite"]["id"] and suite.get("app", {}).get("id") == APP_ID
                        and suite.get("head_sha") == case["head"] and suite.get("status") == "completed"
                        and suite.get("conclusion") == "success", "Gate suite is not completed/successful on frozen head")
                require(repository["object"].get("statusCheckRollup", {}).get("state") == "SUCCESS", "head rollup is incomplete or unsuccessful")
            status, merged = call(client, "PUT", pull_path + "/merge", {"sha": case["head"], "merge_method": "merge"})
            evidence["merge"] = {"status": status, "body": merged}
            if case["case_id"] != "success" and type(merged) is dict and merged.get("merged") is True:
                raise UnsafeError("negative fixture unexpectedly merged; stopped immediately")
            if case["case_id"] == "success":
                require(status == 200 and type(merged) is dict and merged.get("merged") is True, "positive merge was not confirmed by HTTP 200 merged:true")
                evidence["pr_after"] = read(client, pull_path)
                validate_pr(evidence["pr_after"], case, merged=True)
            else:
                evidence["pr_after"] = read(client, pull_path)
                if evidence["pr_after"].get("merged") is True or evidence["pr_after"].get("merged_at"):
                    raise UnsafeError("negative fixture merged according to PR postread; stopped immediately")
                validate_pr(evidence["pr_after"], case)
                require(status == 405 and type(merged) is dict, "negative refusal was not HTTP 405")
                message = merged.get("message", "")
                require(type(message) is str and ("pr gate" in message.lower() or "required status check" in message.lower()),
                        "HTTP 405 refusal cannot be attributed to required PR Gate checks")
            current["outcome"] = "PASS"
        report["all_passed"] = True
    except Exception as exc:
        report["error"] = str(exc)
        if current is not None:
            current["outcome"] = getattr(exc, "outcome", "INCONCLUSIVE")
            current["error"] = str(exc)
            if isinstance(exc, PermissionError):
                current["permission_error"] = {"status": exc.status, "body": exc.body}
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("qualify", "publish", "probe"))
    parser.add_argument("--input", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "qualify":
            require(args.input is None, "qualify does not accept --input")
            result = qualify()
            validate_qualification(result)
            passed = True
        else:
            require(args.input is not None, args.command + " requires --input")
            document = parse_json(args.input.read_text(encoding="utf-8"))
            client = GitHubAPI()
            result = publish(document, client, os.environ) if args.command == "publish" else probe(document, client)
            passed = result["all_passed"]
    except Exception as exc:
        result = {"schema_version": 1, "kind": args.command, "repository": REPOSITORY, "all_passed": False, "error": str(exc)}
        passed = False
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
