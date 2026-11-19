"""Publish the old API Gate for one verified, isolated Mergify control."""
import argparse
import base64
import json
import os
import re
import subprocess

REPO = "Zi-Yi-Ming/sr-pr-gate-lab"
CONFIG = "lab/mergify-native-config"
TARGET = "lab-target/mergify-native-gate"
ORDER_HEAD = "lab/mergify-old-api-sibling-first-gate"
REVIEW_HEAD = "lab/mergify-old-api-review-gate"
HEADS = {
    ORDER_HEAD, REVIEW_HEAD, "lab/mergify-old-api-gate", "lab/mergify-old-api-sibling-gate",
    "lab/mergify-old-api-sibling-clean-gate",
}
APP_ID = 15368
SEED = ".github/workflows/mergify-old-api-community.yml"
SHA = re.compile(r"[0-9a-f]{40}")


def require(condition, message):
    if not condition:
        raise SystemExit(message)


def api(path, payload=None):
    command = ["gh", "api", f"repos/{REPO}/{path}"]
    if payload is not None:
        require(path == "check-runs", "Unexpected remote write.")
        command += ["--method", "POST", "--input", "-"]
    result = subprocess.run(
        command, input=json.dumps(payload) if payload is not None else None,
        text=True, encoding="utf-8", capture_output=True, timeout=45, check=True,
    )
    return json.loads(result.stdout)


def publish(number, head, base, seed_id, run_id, attempt, request=api):
    require(number > 0 and seed_id > 0 and run_id > 0 and attempt == 1
            and SHA.fullmatch(head) and SHA.fullmatch(base), "Invalid frozen identity.")
    pull = request(f"pulls/{number}")
    head_branch = pull["head"]["ref"]
    require(
        pull["state"] == "open" and not pull["draft"]
        and pull["user"]["login"] == "Zi-Yi-Ming"
        and pull["head"]["repo"]["full_name"] == pull["base"]["repo"]["full_name"] == REPO
        and head_branch in HEADS and pull["head"]["sha"] == head
        and (pull["base"]["ref"], pull["base"]["sha"]) == (TARGET, base)
        and not pull["labels"],
        "The original test PR changed or was already submitted to Mergify.",
    )
    target = request(f"git/ref/heads/{TARGET}")
    require(target["object"]["sha"] == base, "Experimental target changed.")

    seed = request(f"actions/runs/{seed_id}")
    seed_event = "pull_request_review" if head_branch == REVIEW_HEAD else "pull_request"
    require(
        seed["event"] == seed_event and seed["name"] == "Community"
        and seed["path"] == SEED and seed["head_sha"] == head and seed["head_branch"] == head_branch
        and seed["status"] == "completed" and seed["conclusion"] == "skipped"
        and [p["number"] for p in seed["pull_requests"]] == [number],
        "Expected the original PR's skipped Community run.",
    )
    seed_suite = seed["check_suite_id"]
    expected_suites = {(seed_suite, "completed", "skipped")}
    if head_branch in {ORDER_HEAD, REVIEW_HEAD}:
        runs = request(f"actions/runs?head_sha={head}&event=pull_request&per_page=30")
        qualifications = [
            run for run in runs["workflow_runs"]
            if run["path"] == ".github/workflows/mergify-sibling-qualification.yml"
        ]
        require(len(qualifications) == 1, "Expected one independent Pull Request run.")
        qualification = qualifications[0]
        require(
            qualification["event"] == "pull_request"
            and qualification["name"] == "Pull Request"
            and qualification["head_sha"] == head
            and qualification["head_branch"] == head_branch
            and qualification["status"] == "completed"
            and qualification["conclusion"] == "success"
            and qualification["run_attempt"] == 1
            and [p["number"] for p in qualification["pull_requests"]] == [number]
            and qualification["check_suite_id"] != seed_suite,
            "The independent Pull Request suite must succeed before publishing.",
        )
        expected_suites.add((qualification["check_suite_id"], "completed", "success"))

    suites = request(f"commits/{head}/check-suites?app_id={APP_ID}&per_page=100")
    observed = {
        (suite["id"], suite["status"], suite["conclusion"])
        for suite in suites["check_suites"]
    }
    require(
        suites["total_count"] == len(suites["check_suites"]) == len(expected_suites)
        and observed == expected_suites,
        "The Actions suite inventory differs from the frozen control.",
    )

    source = pull["merge_commit_sha"]
    require(isinstance(source, str) and SHA.fullmatch(source), "Missing tested merge revision.")
    commit = request(f"git/commits/{source}")
    require([p["sha"] for p in commit["parents"]] == [base, head], "Unexpected merge-tree parents.")
    fixture = request(f"contents/mergify-gate-fixture.json?ref={source}")
    require(fixture["encoding"] == "base64", "Invalid fixture encoding.")
    data = json.loads(base64.b64decode(fixture["content"]))
    require(
        data["contract"] == "native-gate-mergify"
        and all(type(component) is int for component in data["components"])
        and sum(data["components"]) == 42,
        "The same qualification fixture did not pass.",
    )

    checks_path = f"commits/{head}/check-runs?filter=all&per_page=100"
    checks = request(checks_path)
    require(
        checks["total_count"] == len(checks["check_runs"])
        and not any(check["name"] == "PR Gate" for check in checks["check_runs"]),
        "A PR Gate exists or check inventory is incomplete.",
    )
    external = f"semantic-router-ci:{number}:{run_id}:{attempt}"
    check = request("check-runs", {
        "name": "PR Gate", "head_sha": head, "external_id": external,
        "status": "completed", "conclusion": "success",
        "details_url": f"https://github.com/{REPO}/actions/runs/{run_id}",
        "output": {
            "title": "Old API Gate control fixture passed",
            "summary": f"Tested merge {source}, tree {commit['tree']['sha']}.",
        },
    })
    require(
        check["name"] == "PR Gate" and check["head_sha"] == head
        and check["app"]["id"] == APP_ID and check["conclusion"] == "success"
        and check["external_id"] == external and check["check_suite"]["id"] == seed_suite,
        "The API check did not bind to the skipped Community suite. Keep the PR out of the queue.",
    )
    return {"pr": number, "head": head, "base": base, "tested_revision": source,
            "tree": commit["tree"]["sha"], "seed_run": seed_id, "seed_suite": seed_suite,
            "check": check["id"], "publisher": run_id, "external_id": external}


def main():
    require(
        (os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_REF"),
         os.environ.get("GITHUB_EVENT_NAME"), os.environ.get("GITHUB_ACTOR"))
        == (REPO, f"refs/heads/{CONFIG}", "workflow_dispatch", "Zi-Yi-Ming"),
        "Unexpected trusted publisher context.",
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument("--head", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--seed", required=True, type=int)
    args = parser.parse_args()
    print(json.dumps(publish(args.pr, args.head, args.base, args.seed,
                             int(os.environ["GITHUB_RUN_ID"]), int(os.environ["GITHUB_RUN_ATTEMPT"]))))


if __name__ == "__main__":
    main()
