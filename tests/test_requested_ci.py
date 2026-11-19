import unittest

import manual_ci
from controls import requested_ci


class NativeGateTests(unittest.TestCase):
    def test_waiting_and_partial_authorization_do_not_pass(self):
        for scopes in ([], ["storage"]):
            codes = requested_ci.run_contracts(
                {"authorized": bool(scopes), "scopes": scopes, "full": False},
                4,
            )
            self.assertIn(None, codes)
            self.assertFalse(requested_ci.passing(codes))

    def test_full_qualification_requires_actual_contracts_to_pass(self):
        auth = {"authorized": True, "scopes": ["affected"], "full": False}
        self.assertEqual(requested_ci.run_contracts(auth, 4), [0, 0, 0])
        self.assertTrue(requested_ci.passing(requested_ci.run_contracts(auth, 4)))
        self.assertFalse(requested_ci.passing(requested_ci.run_contracts(auth, 3)))
        self.assertFalse(requested_ci.passing([0, 0]))
        self.assertFalse(requested_ci.passing([0, False, 0]))

    def test_adapter_rejects_out_of_scope_mutations(self):
        client = requested_ci.LabClient(31)
        for endpoint in (
            "repos/vllm-project/semantic-router/actions/runs/205/rerun",
            f"repos/{requested_ci.REPO}/issues/31/comments",
            f"repos/{requested_ci.REPO}/actions/runs/205/rerun",
        ):
            with self.assertRaises(manual_ci.ManualCIError):
                client.request(endpoint, method="POST")
