import json
import os
import subprocess
from pathlib import Path


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
pull = event["pull_request"]
label = event["label"]["name"]
prefix = "lab-5661-qualify-"
mode = label.removeprefix(prefix) if label.startswith(prefix) else "success"
if mode not in {"success", "waiting", "partial", "failure"}:
    raise SystemExit("Unknown laboratory fixture")
if pull["draft"] and not (
    pull["user"]["id"] == 37929162
    and pull["user"]["login"] == "mergify[bot]"
    and pull["head"]["repo"]["full_name"] == os.environ["GITHUB_REPOSITORY"]
    and pull["head"]["ref"].startswith("mergify/merge-queue/")
):
    raise SystemExit("An ordinary draft cannot qualify")
if Path("lab-5661-v2/fixture.txt").read_text().splitlines() != ["semantic router gate fixture"]:
    raise SystemExit("The automatic contract failed")
contracts = {"automatic": "success", "manual-a": "deferred", "manual-b": "deferred"}
if mode in {"success", "partial", "failure"}:
    contracts["manual-a"] = "success"
if mode in {"success", "failure"}:
    expected = 4 if mode == "success" else 5
    contracts["manual-b"] = "success" if 2 + 2 == expected else "failure"
states = set(contracts.values())
verdict = "failure" if "failure" in states else "pending" if "deferred" in states else "success"
tested = git("rev-parse", "HEAD")
if tested != os.environ["GITHUB_SHA"]:
    raise SystemExit("Wrong tested revision")
receipt = {
    "pr": event["number"], "run": int(os.environ["GITHUB_RUN_ID"]),
    "attempt": int(os.environ["GITHUB_RUN_ATTEMPT"]),
    "head": pull["head"]["sha"], "base": pull["base"]["sha"],
    "tested": tested, "tree": git("rev-parse", "HEAD^{tree}"),
    "contracts": contracts, "verdict": verdict,
}
Path(os.environ["RUNNER_TEMP"], "lab-5661-receipt.json").write_text(json.dumps(receipt))
