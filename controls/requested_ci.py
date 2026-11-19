"""Isolated replay of a CI request without creating GitHub comments."""

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import manual_ci

REPO = "Zi-Yi-Ming/sr-pr-gate-lab"
TARGET = "lab-target/ci-run-native"
HEAD = "lab/ci-run-native"
ACTOR = "Zi-Yi-Ming"
CARRIER = ".github/workflows/calibration.yml"
AUTH_JOB = manual_ci.AUTHORIZE_JOB
COMMAND = "/ci run"


def require(condition, message):
    if not condition:
        raise manual_ci.ManualCIError(message)


class LabClient(manual_ci.GitHubClient):
    """Translate only lab ref and event metadata. Leave authorization logic intact."""

    def __init__(self, number, rerun_id=None):
        self.number = number
        self.rerun_id = rerun_id
        self.root = f"repos/{REPO}/"

    def write(self, *args, **kwargs):
        raise manual_ci.ManualCIError("The lab has no feedback or check-publication writes.")

    def live_pull(self):
        pull = super().request(f"{self.root}pulls/{self.number}")
        require(
            pull["state"] == "open"
            and not pull["draft"]
            and pull["base"]["ref"] == TARGET
            and pull["head"]["ref"] == HEAD
            and pull["base"]["repo"]["full_name"] == REPO
            and pull["head"]["repo"]["full_name"] == REPO,
            "Expected the open, ready, same-repository lab PR.",
        )
        return pull

    def is_carrier(self, run):
        return (
            run.get("event") == "workflow_dispatch"
            and run.get("path") == CARRIER
            and run.get("head_branch") == TARGET
            and run.get("head_repository", {}).get("full_name") == REPO
            and run.get("actor", {}).get("login") == ACTOR
        )

    def comment(self, carrier_id):
        run = super().request(f"{self.root}actions/runs/{carrier_id}")
        require(self.is_carrier(run), "Unexpected replay carrier identity.")
        return {
            "id": carrier_id,
            "body": COMMAND,
            "issue_url": f"https://api.github.com/repos/{REPO}/issues/{self.number}",
            "user": {"login": ACTOR},
            "created_at": run["created_at"],
            "updated_at": run["created_at"],
        }

    def request(self, endpoint, *, method="GET"):
        require(endpoint.startswith(self.root), "The API request left the lab.")
        if method != "GET":
            require(
                method == "POST"
                and self.rerun_id
                and endpoint == f"{self.root}actions/runs/{self.rerun_id}/rerun",
                "Unexpected lab API write.",
            )
            print(f"Requesting GitHub rerun for original pull_request run {self.rerun_id}.")
            return super().request(endpoint, method=method)

        relative = endpoint[len(self.root):]
        if relative == f"pulls/{self.number}":
            pull = self.live_pull()
            return {**pull, "base": {**pull["base"], "ref": "main"}}
        if relative == "git/ref/heads/main":
            ref = super().request(f"{self.root}git/ref/heads/{TARGET}")
            require(ref["ref"] == f"refs/heads/{TARGET}", "Unexpected target ref.")
            return {**ref, "ref": "refs/heads/main"}
        if relative.startswith("compare/") and relative.endswith("...main"):
            return super().request(endpoint.removesuffix("main") + TARGET)
        if relative.startswith("issues/comments/"):
            return self.comment(int(relative.split("/")[-1]))
        if relative == "actions/workflows/manual-ci.yml":
            return super().request(f"{self.root}actions/workflows/calibration.yml")

        result = super().request(endpoint)
        if (
            relative.startswith("actions/runs/")
            and relative.split("/")[-1].isdigit()
            and self.is_carrier(result)
        ):
            return {
                **result, "event": "issue_comment",
                "path": manual_ci.CONTROLLER, "head_branch": "main",
            }
        return result


def run_contracts(auth, fixture):
    enabled = auth["authorized"] and (auth["full"] or "affected" in auth["scopes"])
    storage = enabled or auth["authorized"] and "storage" in auth["scopes"]
    contracts = [
        ("assert value > 0", True),
        ("assert value % 2 == 0", storage),
        ("assert value * value == 16", enabled),
    ]
    return [
        subprocess.run(
            [sys.executable, "-B", "-c", f"value = {fixture}\n{assertion}"],
            check=False,
        ).returncode if selected else None
        for assertion, selected in contracts
    ]


def passing(codes):
    return len(codes) == 3 and all(type(code) is int and code == 0 for code in codes)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def revision(client, event):
    head = client.live_pull()["head"]["sha"]
    source = os.environ["GITHUB_SHA"]
    require(
        os.environ["GITHUB_EVENT_NAME"] == "pull_request"
        and event["pull_request"]["head"]["sha"] == head
        and Path(".git").exists()
        and subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == source,
        "The native Gate is not running on the expected checked-out merge revision.",
    )
    parents = client.request(f"{client.root}git/commits/{source}")["parents"]
    require(len(parents) == 2 and parents[1]["sha"] == head, "Invalid test merge parents.")
    return source, parents[0]["sha"], head


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("plan", "gate", "authorize", "rerun"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    require(os.environ.get("GITHUB_REPOSITORY") == REPO, "Unexpected repository.")
    if args.phase in ("authorize", "rerun"):
        require(
            os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
            and os.environ.get("GITHUB_REF") == f"refs/heads/{TARGET}"
            and os.environ.get("GITHUB_ACTOR") == ACTOR,
            "Unexpected controller dispatch.",
        )
        number, run_id = int(os.environ["LAB_PR"]), int(os.environ["LAB_SOURCE_RUN"])
        client = LabClient(number, run_id if args.phase == "rerun" else None)
        if args.phase == "rerun":
            record = json.loads(args.output.read_text(encoding="utf-8"))
            require(
                record["pr_number"] == number and record["pr_run_id"] == run_id,
                "Authorization belongs to a different run.",
            )
            require(
                client.request(f"{client.root}actions/runs/{run_id}")["run_attempt"] == 1,
                "Expected exactly the first attempt before rerunning.",
            )
            manual_ci.rerun(client, record, wait_seconds=0)
            return 0

        comment = client.comment(int(os.environ["GITHUB_RUN_ID"]))
        event = {
            "action": "created", "repository": {"full_name": REPO},
            "sender": comment["user"], "comment": comment,
            "issue": {"number": number, "pull_request": {"lab": True}},
        }
        os.environ["GITHUB_EVENT_NAME"] = "issue_comment"
        record = manual_ci.authorize(client, event)
        require(record and record["pr_run_id"] == run_id, "No authorization for the pinned run.")
        write_json(args.output, record)
        print(json.dumps(record, sort_keys=True))
        return 0

    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    client = LabClient(int(event["number"]))
    source, base, head = revision(client, event)
    if args.phase == "plan":
        plan = {"schema_version": 1, "source_sha": source, "base_sha": base}
        raw = json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()
        plan["plan_sha256"] = hashlib.sha256(raw).hexdigest()
        write_json(args.output, plan)
        print(json.dumps(plan, sort_keys=True))
        return 0

    auth = manual_ci.resolve(client, event, source=source, base=base)
    fixture = int(Path("requested-ci-fixture.txt").read_text())
    codes = run_contracts(auth, fixture)
    result = {
        "authorization": auth, "contract_exit_codes": codes,
        "head": head, "base": base, "source": source,
        "run_id": int(os.environ["GITHUB_RUN_ID"]),
        "attempt": int(os.environ["GITHUB_RUN_ATTEMPT"]),
        "passed": passing(codes),
    }
    write_json(args.output, result)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
