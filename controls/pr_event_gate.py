"""Check the merge revision for the isolated GitHub pull request control."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


REPOSITORY = "Zi-Yi-Ming/sr-pr-gate-lab"
BRANCH_PAIRS = {
    ("lab-target/pr-event-green", "lab/pr-event-green"),
    ("lab-target/pr-event-red", "lab/pr-event-red"),
}
COEXISTENCE_BRANCH_PAIRS = {
    ("lab-target/pr-event-stale-green", "lab/pr-event-stale-green"),
    ("lab-target/pr-event-stale-red", "lab/pr-event-stale-red"),
}
COEXISTENCE_STAGES = {
    "seed": ("opened", True, "seed-qualification.txt"),
    "current": ("ready_for_review", False, "qualification.txt"),
}


def checked_revision(pairs):
    if os.environ["GITHUB_EVENT_NAME"] != "pull_request":
        raise ValueError("The control requires a pull_request event")

    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    pull = event["pull_request"]
    base, head = pull["base"], pull["head"]
    if any(
        repository != REPOSITORY
        for repository in (
            os.environ["GITHUB_REPOSITORY"],
            base["repo"]["full_name"],
            head["repo"]["full_name"],
        )
    ):
        raise ValueError("Both branches must belong to the lab repository")
    if (base["ref"], head["ref"]) not in pairs:
        raise ValueError("The pull request does not match a control branch pair")

    revision, *parents = subprocess.check_output(
        ["git", "log", "-1", "--format=%H %P"], text=True
    ).split()
    if revision != os.environ["GITHUB_SHA"] or parents != [base["sha"], head["sha"]]:
        raise ValueError("Checkout and merge parents must match the pull_request event")
    return event, revision


def qualify(stage="baseline"):
    pairs = BRANCH_PAIRS if stage == "baseline" else COEXISTENCE_BRANCH_PAIRS
    event, revision = checked_revision(pairs)
    pull = event["pull_request"]
    fixture_path = "qualification.txt"
    if stage != "baseline":
        action, draft, fixture_path = COEXISTENCE_STAGES[stage]
        if event.get("action") != action or pull.get("draft") is not draft:
            description = "an opened draft" if draft else "a ready_for_review transition"
            raise ValueError(f"The {stage} Gate requires {description}")

    fixture = Path(fixture_path)
    if not fixture.is_file() or fixture.read_bytes() != b"ready\n":
        raise ValueError("The qualification fixture is missing or does not contain ready")
    print(f"Qualified merge revision {revision} for base {pull['base']['sha']} and head {pull['head']['sha']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=("baseline", *COEXISTENCE_STAGES), default="baseline")
    try:
        qualify(parser.parse_args().stage)
    except (KeyError, TypeError, ValueError, OSError, subprocess.CalledProcessError) as error:
        sys.exit(str(error))
