import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


GATE = Path(__file__).resolve().parents[1] / "controls" / "pr_event_gate.py"
REPOSITORY = "Zi-Yi-Ming/sr-pr-gate-lab"


class PullRequestGateTests(unittest.TestCase):
    def run_gate(self, **kwargs):
        return self.run_gates([(None, "opened", False)], **kwargs)[0]

    def run_gates(
        self,
        transitions,
        *,
        case="green",
        marker=b"ready\n",
        seed_marker=None,
        mutation=None,
        event="pull_request",
    ):
        self.assertTrue(GATE.is_file(), "The pull-request control has no Gate implementation")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()

            def git(*arguments):
                return subprocess.run(
                    [
                        "git",
                        "-c", "user.name=PR Gate Lab",
                        "-c", "user.email=lab@example.invalid",
                        *arguments,
                    ],
                    cwd=checkout,
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout.strip()

            git("init", "-q", "-b", "target")
            (checkout / ".gitattributes").write_bytes(b"* text=auto eol=lf\n")
            git("add", ".gitattributes")
            if seed_marker is not None:
                (checkout / "seed-qualification.txt").write_bytes(seed_marker)
                git("add", "seed-qualification.txt")
            git("commit", "-qm", "Target")
            base = git("rev-parse", "HEAD")
            git("switch", "-qc", "topic")
            qualification = checkout / "qualification.txt"
            if marker is not None:
                qualification.write_bytes(marker)
                git("add", "qualification.txt")
            git("commit", "--allow-empty", "-qm", "Qualification fixture")
            head = git("rev-parse", "HEAD")
            git("switch", "-q", "target")
            git("merge", "--no-ff", "--no-edit", "topic")
            merge = git("rev-parse", "HEAD")

            payload = {
                "pull_request": {
                    "base": {"ref": f"lab-target/pr-event-{case}", "sha": base, "repo": {"full_name": REPOSITORY}},
                    "head": {"ref": f"lab/pr-event-{case}", "sha": head, "repo": {"full_name": REPOSITORY}},
                },
            }
            if mutation is not None:
                mutation(payload["pull_request"])
            event_file = root / "event.json"
            results = []
            for stage, action, draft in transitions:
                payload["action"] = action
                payload["pull_request"]["draft"] = draft
                event_file.write_text(json.dumps(payload), encoding="utf-8")
                command = [sys.executable, "-B", str(GATE)]
                if stage is not None:
                    command.extend(["--stage", stage])
                results.append(subprocess.run(
                    command,
                    cwd=checkout,
                    env={
                        **os.environ,
                        "GITHUB_EVENT_NAME": event,
                        "GITHUB_EVENT_PATH": str(event_file),
                        "GITHUB_REPOSITORY": REPOSITORY,
                        "GITHUB_SHA": merge,
                    },
                    capture_output=True,
                    text=True,
                    check=False,
                ))
            return results

    def test_qualifies_the_verified_pull_request_merge_commit(self):
        result = self.run_gate()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_failing_qualification_fixture_propagates_failure(self):
        result = self.run_gate(case="red", marker=b"broken\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("qualification fixture", result.stderr)

    def test_missing_fixture_fails_closed(self):
        result = self.run_gate(marker=None)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("qualification fixture", result.stderr)

    def test_dispatch_does_not_qualify_as_a_pull_request_event(self):
        result = self.run_gate(event="workflow_dispatch")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pull_request event", result.stderr)

    def test_fork_cannot_provide_the_control_fixture(self):
        def change_repository(pull):
            pull["head"]["repo"]["full_name"] = "example/sr-pr-gate-lab"

        result = self.run_gate(mutation=change_repository)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("lab repository", result.stderr)

    def test_merge_commit_must_include_the_event_head(self):
        def change_head(pull):
            pull["head"]["sha"] = "f" * 40

        result = self.run_gate(mutation=change_head)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("merge parents", result.stderr)

    def test_a_different_topic_cannot_qualify_for_the_target(self):
        def change_branch(pull):
            pull["head"]["ref"] = "lab/pr-event-red"

        result = self.run_gate(mutation=change_branch)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("branch pair", result.stderr)

    def test_old_green_and_new_green_qualify_the_same_revision(self):
        seed, current = self.run_gates(
            [("seed", "opened", True), ("current", "ready_for_review", False)],
            case="stale-green",
            seed_marker=b"ready\n",
        )
        self.assertEqual(seed.returncode, 0, seed.stderr)
        self.assertEqual(current.returncode, 0, current.stderr)
        self.assertEqual(seed.stdout, current.stdout)

    def test_old_green_does_not_satisfy_the_new_qualification_on_the_same_revision(self):
        seed, current = self.run_gates(
            [("seed", "opened", True), ("current", "ready_for_review", False)],
            case="stale-red",
            marker=b"broken\n",
            seed_marker=b"ready\n",
        )
        self.assertEqual(seed.returncode, 0, seed.stderr)
        self.assertEqual(current.returncode, 1, current.stderr)
        self.assertIn("qualification fixture", current.stderr)

    def test_seed_requires_opening_as_a_draft(self):
        result, = self.run_gates(
            [("seed", "opened", False)], case="stale-green", seed_marker=b"ready\n"
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("opened draft", result.stderr)

    def test_current_requires_the_draft_to_be_marked_ready(self):
        first, second = self.run_gates(
            [("current", "opened", False), ("current", "ready_for_review", True)],
            case="stale-green",
        )
        for result in (first, second):
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("ready_for_review", result.stderr)

    def test_coexistence_stage_cannot_qualify_a_clean_control_branch(self):
        result, = self.run_gates([("current", "ready_for_review", False)])
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("branch pair", result.stderr)


if __name__ == "__main__":
    unittest.main()
