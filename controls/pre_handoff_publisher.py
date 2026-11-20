"""Trusted lab-only publisher for the previously qualified generation."""
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import os
import subprocess


REPO = "Zi-Yi-Ming/sr-pr-gate-lab"
CONFIG = "lab/mergify-native-config"
TARGET = "lab-target/5661-prehandoff"
APP = 15368


def api(path, payload=None):
    args = ["gh", "api", f"repos/{REPO}/{path}"]
    if payload is not None:
        assert path == "check-runs"
        args += ["--method", "POST", "--input", "-"]
    result = subprocess.run(args, input=json.dumps(payload) if payload else None,
                            text=True, encoding="utf-8", check=True, timeout=35,
                            stdout=subprocess.PIPE)
    return json.loads(result.stdout)


def scope(pull):
    head, base = pull["head"], pull["base"]
    assert pull["state"] == "open" and not pull["draft"] and not pull["labels"]
    assert pull["user"]["login"] == "Zi-Yi-Ming"
    assert head["repo"]["full_name"] == base["repo"]["full_name"] == REPO
    assert base["ref"] == TARGET
    assert head["ref"] in {f"lab/5661-prehandoff-{c}" for c in ("positive", "direct", "queue")}
    assert api(f"git/ref/heads/{TARGET}")["object"]["sha"] == base["sha"]
    return (head["sha"], base["sha"], pull["merge_commit_sha"])


def publish(number, seed_id, run_id):
    head, base, source = scope(api(f"pulls/{number}"))
    seed, run = (api(f"actions/runs/{i}") for i in (seed_id, run_id))
    for item, filename, conclusion in (
        (seed, "pre-handoff-community.yml", "skipped"),
        (run, "pre-handoff-qualification.yml", "success"),
    ):
        assert item["path"] == f".github/workflows/{filename}"
        assert (item["head_sha"], item["event"], item["status"], item["conclusion"], item["run_attempt"]) == (
            head, "pull_request", "completed", conclusion, 1
        )
        assert [p["number"] for p in item["pull_requests"]] == [number]
    assert seed["check_suite_id"] != run["check_suite_id"]
    with TemporaryDirectory() as tmp:
        subprocess.run(["gh", "run", "download", str(run_id), "-R", REPO, "-n",
                        "old-receipt-1", "-D", tmp], check=True, timeout=45)
        receipt = json.loads(Path(tmp, "pre-handoff.json").read_text())
    assert receipt == {
        "pr": number, "event": "assigned", "head": head, "base": base,
        "tested_revision": source, "tree": receipt["tree"], "run": run_id, "attempt": 1,
        "qualification": "passed", "kind": "old",
    }
    tested = api(f"git/commits/{source}")
    assert [p["sha"] for p in tested["parents"]] == [base, head]
    assert tested["tree"]["sha"] == receipt["tree"]
    assert api(f"commits/{head}/check-runs?filter=all&check_name=PR%20Gate")["total_count"] == 0
    assert scope(api(f"pulls/{number}")) == (head, base, source)
    external_id = f"semantic-router-ci:{number}:{run_id}:1"
    check = api("check-runs", {
        "name": "PR Gate", "head_sha": head, "external_id": external_id,
        "status": "completed", "conclusion": "success",
        "details_url": f"https://github.com/{REPO}/actions/runs/{run_id}",
        "output": {"title": "Lab previous qualification passed",
                   "summary": f"Qualified tree {receipt['tree']} at {source}."},
    })
    assert check["app"]["id"] == APP and check["external_id"] == external_id
    print(json.dumps({**receipt, "check": check["id"], "suite": check["check_suite"]["id"],
                      "community_suite": seed["check_suite_id"]}))


if __name__ == "__main__":
    assert (os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_REF"],
            os.environ["GITHUB_EVENT_NAME"], os.environ["GITHUB_ACTOR"]) == (
        REPO, f"refs/heads/{CONFIG}", "workflow_dispatch", "Zi-Yi-Ming"
    )
    publish(*(int(os.environ[f"LAB_{key}"]) for key in ("PR", "SEED", "RUN")))
