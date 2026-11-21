import re
import unittest
from pathlib import Path


class SupplementalWorkflowTests(unittest.TestCase):
    def test_supplement_uses_source_run_and_separates_patch_from_merge(self):
        text = (Path(__file__).resolve().parents[1] / '.github/workflows/calibration.yml').read_text(encoding='utf-8')
        def job(name):
            found = re.search(r'(?ms)^  supplement_' + name + r':\n(.*?)(?=^  [a-z_]+:\n|\Z)', text)
            self.assertIsNotNone(found, 'missing bounded supplemental job ' + name)
            return found.group(1)
        for name in ('baseline', 'recover', 'probe'):
            current = job(name)
            self.assertIn("run-id: '44048669391'", current)
            self.assertIn('name: ab-publication', current)
            self.assertNotIn('actions: write', current)
            self.assertNotIn('administration:', current)
            self.assertNotIn('ab_lab.py publish', current)
            self.assertIn('persist-credentials: false', current)
        self.assertIn('checks: write', job('recover'))
        self.assertNotIn('contents: write', job('recover'))
        for name in ('baseline', 'probe'):
            self.assertIn('contents: write', job(name))
            self.assertIn('checks: read', job(name))
            self.assertNotIn('checks: write', job(name))
        self.assertIn("needs.supplement_baseline.outputs.eligible == 'true'", job('recover'))


if __name__ == '__main__':
    unittest.main()
