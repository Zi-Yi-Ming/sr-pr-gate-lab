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
        def job(name):
            return re.search(r"(?ms)^  " + name + r":\n(.*?)(?=^  [a-z_]+:\n|\Z)", self.text).group(1)
        for name in ("publish", "recover"):
            self.assertIn("checks: write", job(name))
            self.assertNotIn("contents: write", job(name))
        for name in ("baseline", "probe"):
            self.assertIn("contents: write", job(name))
            self.assertIn("checks: read", job(name))
            self.assertNotIn("checks: write", job(name))
        for name in ("seed", "relay", "advance"):
            self.assertIn("actions: write", job(name))
            self.assertNotIn("contents: write", job(name))
            self.assertNotIn("checks: write", job(name))
        self.assertNotIn("pull-requests: write", self.text)

    def test_actions_are_immutable_and_shell_does_not_interpolate_inputs(self):
        actions = re.findall(r"uses: (\S+)", self.text)
        self.assertTrue(actions)
        self.assertTrue(all(re.fullmatch(r"actions/[\w-]+@[a-f0-9]{40}", action) for action in actions))
        for line in self.text.splitlines():
            if "run:" in line:
                self.assertNotIn("${{", line)
        self.assertEqual(self.text.count("persist-credentials: false"), self.text.count("uses: actions/checkout@"))

    def test_finite_chain_and_original_dispatch_schema(self):
        self.assertIn("probe-only", self.text)
        self.assertIn("source_run_id:", self.text)
        self.assertIn("group: sr-pr-gate-lab-ab", self.text)
        self.assertIn("cancel-in-progress: false", self.text)
        self.assertIn("needs.baseline.outputs.eligible == 'true'", self.text)
        self.assertIn("needs.probe.outputs.advance == 'true'", self.text)
        self.assertIn("name: ab-baseline", self.text)
        self.assertNotIn("sleep ", self.text)


if __name__ == "__main__":
    unittest.main()
