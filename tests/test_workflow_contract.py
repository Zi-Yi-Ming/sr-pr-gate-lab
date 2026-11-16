"""Local guards on the workflow's authority and trigger boundaries."""
import re
import unittest
from pathlib import Path


class WorkflowContractTests(unittest.TestCase):
    def setUp(self):
        self.text = (Path(__file__).resolve().parents[1] / ".github/workflows/calibration.yml").read_text(encoding="utf-8")

    def test_only_manual_dispatch_can_start_calibration(self):
        events = self.text.split("\non:\n", 1)[1].split("\npermissions:", 1)[0]
        self.assertRegex(events, r"(?m)^  workflow_dispatch:")
        self.assertNotRegex(events, r"(?m)^  (push|pull_request|schedule|workflow_run|repository_dispatch):")

    def test_publisher_cannot_merge_and_probe_cannot_rewrite_checks(self):
        publisher = self.text.split("\n  publish:\n", 1)[1].split("\n  probe:\n", 1)[0]
        probe = self.text.split("\n  probe:\n", 1)[1]
        self.assertIn("checks: write", publisher)
        self.assertNotIn("contents: write", publisher)
        self.assertIn("contents: write", probe)
        self.assertIn("checks: read", probe)
        self.assertNotIn("checks: write", probe)
        self.assertNotIn("pull-requests: write", self.text)

    def test_actions_are_immutable_and_shell_does_not_interpolate_inputs(self):
        actions = re.findall(r"uses: (\S+)", self.text)
        self.assertTrue(actions)
        self.assertTrue(all(re.fullmatch(r"actions/[\w-]+@[a-f0-9]{40}", action) for action in actions))
        for line in self.text.splitlines():
            if "run:" in line:
                self.assertNotIn("${{", line)
        self.assertEqual(self.text.count("persist-credentials: false"), 3)

    def test_probe_resume_reuses_existing_publication(self):
        self.assertIn("probe-only", self.text)
        self.assertIn("inputs.source_run_id || github.run_id", self.text)
        self.assertIn("name: basic-publication", self.text)
        self.assertNotIn("sleep ", self.text)


if __name__ == "__main__":
    unittest.main()
