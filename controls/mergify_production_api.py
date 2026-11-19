"""Publish a check only for the isolated production-shaped qualification fixture."""
import argparse
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory

from mergify_api_gate import APP_ID, CONFIG, REPO, SEED, SHA, TARGET, api, require

HEAD = "lab/mergify-production-api-gate"
QUALIFICATION = ".github/workflows/mergify-sibling-qualification.yml"
QUEUE_LABEL = "lab-mergify-native-gate"


def qualified_receipt(run):
    with TemporaryDirectory() as folder:
        subprocess.run(
            ["gh", "run", "download", str(run), "-R", REPO, "-n",
             "sibling-qualification-attempt-1", "-D", folder],
            timeout=45, check=True,
        )
        return json.loads(Path(folder, "sibling-qualification.json").read_text())


def publish(number, original, seed_id, run_id, request=api, receipt=qualified_receipt):
    require(min(number, original, seed_id, run_id) > 0, "Invalid lab identity.")
    draft = number != original
    original_pr = request(f"pulls/{original}")
    require(
        original_pr["state"] == "open" and not original_pr["draft"]
        and original_pr["user"]["login"] == "Zi-Yi-Ming"
        and original_pr["head"]["ref"] == HEAD
        and original_pr["head"]["repo"]["full_name"] == REPO
        and original_pr["base"]["repo"]["full_name"] == REPO
        and original_pr["base"]["ref"] == TARGET
        and [label["name"] for label in original_pr["labels"]] == ([QUEUE_LABEL] if draft else []),
        "Original PR is outside this control.",
    )
    pull = request(f"pulls/{number}") if draft else original_pr
    head, base = pull["head"]["sha"], pull["base"]["sha"]
    require(
        pull["state"] == "open" and pull["draft"] is draft
        and pull["head"]["repo"]["full_name"] == pull["base"]["repo"]["full_name"] == REPO
        and pull["base"]["ref"] == TARGET and SHA.fullmatch(head) and SHA.fullmatch(base),
        "Unexpected target or PR revision.",
    )
    if draft:
        require(
            (pull["user"]["id"], pull["user"]["login"]) == (37929162, "mergify[bot]")
            and pull["head"]["ref"].startswith("mergify/merge-queue/")
            and pull["title"].startswith(f"merge queue: checking #{original} on "),
            "Draft is not the queue candidate for the original.",
        )
    require(request(f"git/ref/heads/{TARGET}")["object"]["sha"] == base,
            "Protected target moved while checking the fixture.")

    seed = request(f"actions/runs/{seed_id}")
    run = request(f"actions/runs/{run_id}")
    for item, name, path, event, conclusion in (
        (seed, "Community", SEED, "pull_request" if draft else "pull_request_review", "skipped"),
        (run, "Pull Request", QUALIFICATION, "pull_request", "success"),
    ):
        require(
            item["name"] == name and item["path"] == path and item["event"] == event
            and item["head_sha"] == head and item["head_branch"] == pull["head"]["ref"]
            and item["status"] == "completed" and item["conclusion"] == conclusion
            and item["run_attempt"] == 1,
            f"The {name} run does not match the original event or the draft.",
        )
        linked = [pr["number"] for pr in item["pull_requests"]]
        require(linked == [number] or draft and not linked, "Workflow links a different PR.")
    suites = request(f"commits/{head}/check-suites?app_id={APP_ID}&per_page=100")
    observed = {(s["id"], s["status"], s["conclusion"]) for s in suites["check_suites"]}
    require(suites["total_count"] == len(suites["check_suites"]) == 2 and observed == {
        (seed["check_suite_id"], "completed", "skipped"),
        (run["check_suite_id"], "completed", "success"),
    }, "Expected exactly the skipped and successful Actions suites.")

    result = receipt(run_id)
    require(
        (result["pr"], result["head"], result["base"], result["run"], result["attempt"],
         result["draft"], result["author"]) == (
            number, head, base, run_id, 1, draft, pull["user"]["login"]
        ),
        "Qualification receipt does not match the frozen PR, head, base and attempt.",
    )
    source = result["tested_revision"]
    require(SHA.fullmatch(source) and source == pull["merge_commit_sha"],
            "Qualification did not test the current merge revision.")
    commit = request(f"git/commits/{source}")
    require([parent["sha"] for parent in commit["parents"]] == [base, head]
            and commit["tree"]["sha"] == result["tree"], "Qualification tree mismatch.")
    path = f"commits/{head}/check-runs?filter=all&check_name=PR%20Gate&per_page=100"
    require(request(path)["total_count"] == 0, "A Gate already exists on the candidate.")
    current = request(f"pulls/{number}")
    require(current["state"] == "open" and (current["head"]["sha"], current["base"]["sha"],
            current["merge_commit_sha"]) == (head, base, source)
            and request(f"git/ref/heads/{TARGET}")["object"]["sha"] == base,
            "PR or target moved before publication.")

    external = f"semantic-router-ci:{number}:{run_id}:1"
    check = request("check-runs", {
        "name": "PR Gate",
        "head_sha": head,
        "external_id": external,
        "status": "completed",
        "conclusion": "success",
        "details_url": f"https://github.com/{REPO}/actions/runs/{run_id}",
        "output": {"title": "Isolated qualification passed", "summary": f"Tested {source}."},
    })
    require(check["app"]["id"] == APP_ID and check["external_id"] == external
            and check["head_sha"] == head and check["conclusion"] == "success",
            "Unexpected identity in the published check.")
    return {
        **result, "original": original, "api_gate": check["id"],
        "gate_suite": check["check_suite"]["id"], "community_suite": seed["check_suite_id"],
        "qualification_suite": run["check_suite_id"], "external_id": external,
    }


def main():
    require(
        (os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_REF"),
         os.environ.get("GITHUB_EVENT_NAME"), os.environ.get("GITHUB_ACTOR")) ==
        (REPO, f"refs/heads/{CONFIG}", "workflow_dispatch", "Zi-Yi-Ming"),
        "Unexpected publisher execution context.",
    )
    parser = argparse.ArgumentParser()
    for name in ("pr", "original", "seed", "qualification"):
        parser.add_argument(name, type=int)
    args = parser.parse_args()
    print(json.dumps(publish(args.pr, args.original, args.seed, args.qualification)))


if __name__ == "__main__":
    main()
