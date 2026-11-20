"""Lab-only fixtures for the previous qualification and the queue draft."""
import json
import os
import subprocess
from pathlib import Path


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
pr = event["pull_request"]
head, base = pr["head"], pr["base"]
repo = "Zi-Yi-Ming/sr-pr-gate-lab"
assert os.environ["GITHUB_REPOSITORY"] == repo
assert os.environ["GITHUB_EVENT_NAME"] == "pull_request"
assert head["repo"]["full_name"] == base["repo"]["full_name"] == repo
assert base["ref"] == "lab-target/5661-prehandoff"
kind = os.environ["LAB_KIND"]
if kind == "queue":
    assert event["action"] == "unassigned" and pr["draft"]
    assert pr["user"]["login"] == "mergify[bot]"
    assert head["ref"].startswith("mergify/merge-queue/")
else:
    assert event["action"] == {"old": "assigned", "new": "ready_for_review"}[kind]
    assert not pr["draft"] and pr["user"]["login"] == "Zi-Yi-Ming"
    assert head["ref"] in {f"lab/5661-prehandoff-{case}" for case in ("positive", "direct", "queue")}
    assert kind != "old" or event["assignee"]["login"] == "Zi-Yi-Ming"

source = git("rev-parse", "HEAD")
assert source == os.environ["GITHUB_SHA"]
if kind != "new":
    fixture = json.loads(Path("pre-handoff-fixture.json").read_text())
    assert fixture["policy"] == "requires-full-qualification"
    assert sum(fixture["components"]) == 42
    assert fixture["case"] == "positive" if kind == "queue" else True

receipt = {
    "pr": pr["number"], "event": event["action"], "head": head["sha"], "base": base["sha"],
    "tested_revision": source, "tree": git("rev-parse", "HEAD^{tree}"),
    "run": int(os.environ["GITHUB_RUN_ID"]), "attempt": int(os.environ["GITHUB_RUN_ATTEMPT"]),
    "qualification": "awaiting-authorization" if kind == "new" else "passed",
    "kind": kind,
}
Path("pre-handoff.json").write_text(json.dumps(receipt, indent=2) + "\n")
print(json.dumps(receipt))
