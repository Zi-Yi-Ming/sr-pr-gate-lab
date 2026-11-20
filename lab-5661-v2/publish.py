import json
import os
import subprocess
from pathlib import Path

REPO = "Zi-Yi-Ming/sr-pr-gate-lab"
TARGET = "lab-target/5661-v2"


def api(path, payload=None):
    args = ["gh", "api", f"repos/{REPO}/{path}"]
    if payload is not None:
        args += ["--method", "POST", "--input", "-"]
    result = subprocess.run(
        args, input=json.dumps(payload) if payload is not None else None,
        check=True, capture_output=True, text=True,
    )
    return json.loads(result.stdout)


pr = int(os.environ["LAB_PR"])
run_id = int(os.environ["LAB_RUN"])
attempt = int(os.environ["LAB_ATTEMPT"])
variant = os.environ["LAB_VARIANT"]
if os.environ["GITHUB_REPOSITORY"] != REPO or variant not in {"legacy", "v2"}:
    raise SystemExit("Outside the isolated laboratory")
r = json.loads(Path(os.environ["RUNNER_TEMP"], "lab-5661-evidence", "lab-5661-receipt.json").read_text())
p = api(f"pulls/{pr}")
run = api(f"actions/runs/{run_id}")
head = p["head"]["sha"]
if (
    p["state"] != "open" or p["base"]["ref"] != TARGET
    or p["head"]["repo"]["full_name"] != REPO
    or not (p["head"]["ref"].startswith("lab/5661-v2-")
            or (p["draft"] and p["user"]["id"] == 37929162
                and p["head"]["ref"].startswith("mergify/merge-queue/")))
):
    raise SystemExit("Wrong laboratory pull request")
if (
    (r["pr"], r["run"], r["attempt"], r["head"], r["base"])
    != (pr, run_id, attempt, head, p["base"]["sha"])
    or run["head_sha"] != head or run["run_attempt"] != attempt
    or run["path"] != ".github/workflows/5661-v2-qualification.yml"
    or run["event"] != "pull_request" or run["status"] != "completed"
):
    raise SystemExit("Qualification does not match the current PR and run")
rev = api(f"git/commits/{r['tested']}")
target = api(f"git/ref/heads/{TARGET}")["object"]["sha"]
# The PR payload's base SHA can lag a target ref update. The checked-out
# merge ref and its Git parents identify the actual tested tree.
if (
    [v["sha"] for v in rev["parents"]] != [target, head]
    or rev["tree"]["sha"] != r["tree"] or r["tested"] != p["merge_commit_sha"]
):
    raise SystemExit("Wrong tested tree or target branch")
contracts = r["contracts"]
if set(contracts) != {"automatic", "manual-a", "manual-b"} or contracts["automatic"] != "success":
    raise SystemExit("Bad contract inventory")
states = set(contracts.values())
if not states <= {"success", "deferred", "failure"}:
    raise SystemExit("Invalid contract state")
verdict = "failure" if "failure" in states else "pending" if "deferred" in states else "success"
if verdict != r["verdict"] or run["conclusion"] != ("failure" if verdict == "failure" else "success"):
    raise SystemExit("Inconsistent receipt")
if variant == "legacy":
    if verdict != "success":
        raise SystemExit("Legacy green needs full qualification")
    if api(f"commits/{head}/check-runs?check_name=PR%20Gate&filter=all")["total_count"]:
        raise SystemExit("Refusing to duplicate the legacy Gate")
    result = api("check-runs", {
        "name": "PR Gate", "head_sha": head, "status": "completed", "conclusion": "success",
        "external_id": f"5661-lab:{pr}:{run_id}:{attempt}", "details_url": run["html_url"],
    })
else:
    result = api(f"statuses/{head}", {
        "context": "PR Gate v2", "state": verdict, "target_url": run["html_url"],
        "description": "Verified isolated 5661 qualification fixture",
    })
print(json.dumps({"id": result["id"], "variant": variant, "state": verdict, "pr": pr,
                  "head": head, "suite": (result.get("check_suite") or {}).get("id")}))
