"""Supplemental merge evidence cannot substitute for strict reproduction."""
import copy
import json
import unittest
from unittest.mock import patch

import test_ab_lab as fixture
import supplement_a as supp


class SupplementTests(unittest.TestCase):
    def setUp(self):
        self.api, self.source = fixture.published(3)
        self.api.null_rollup = True
        self.api.runs[300] = dict(self.source['native']['qualification_run'], id=300,
            head_sha=fixture.CONTROLLER, head_branch='lab/ab-controller', conclusion='failure')
        self.manifest = {'experiment': 'merge-decision-supplement-a-v1', 'stage': 'supplement',
                         'controller_sha': 'd' * 40}
        self.env = dict(fixture.environment(fixture.envelope(3)), GITHUB_RUN_ID='400',
                        GITHUB_REF='refs/heads/lab/ab-supplement-a', GITHUB_SHA='d' * 40)
        self.pins = patch.multiple(supp, SOURCE_RUN_ID='300', SOURCE_DIGEST=supp.digest(self.source))
        self.pins.start(); self.addCleanup(self.pins.stop)

    def run_stage(self, stage, proof=None):
        env = dict(self.env, GITHUB_JOB='supplement_' + stage)
        return supp.execute(stage, self.manifest, self.source, proof, self.api, env)

    def test_null_rollup_baseline_needs_exact_gate_405_and_no_publication(self):
        self.api.calls.clear()
        result = self.run_stage('baseline')
        self.assertEqual(result['outcome'], 'GATE_BLOCKED_MERGE_PROBE_ONLY', result.get('error'))
        self.assertTrue(result['eligible'])
        self.assertFalse(result['strict_recovery_verified'])
        self.assertEqual([m for m, p, d in self.api.calls if m != 'GET' and p != '/graphql'], ['PUT'])

    def test_other_required_check_generic_refusal_or_permissions_cannot_authorize_patch(self):
        for status, message in ((405, 'Required status check "Security" is expected.'),
                                (405, 'Required status check "PR Gateway" is expected.'),
                                (409, 'head changed'), (403, 'forbidden')):
            self.api.merge_result = status, {'message': message}
            baseline = self.run_stage('baseline')
            self.assertFalse(baseline['eligible'])
            self.api.calls.clear()
            self.assertFalse(self.run_stage('recover', baseline)['all_passed'])
            self.assertFalse(any(m == 'PATCH' for m, p, d in self.api.calls))

    def test_only_exact_patch_preserves_other_checks_and_never_claims_strict_recovery(self):
        baseline = self.run_stage('baseline'); before = copy.deepcopy(self.api.checks)
        self.api.calls.clear()
        recovery = self.run_stage('recover', baseline)
        self.assertTrue(recovery['all_passed'], recovery.get('error'))
        writes = [(m, p) for m, p, d in self.api.calls if m != 'GET' and p != '/graphql']
        self.assertEqual(writes, [('PATCH', fixture.P + '/check-runs/' + str(self.source['initial_check']['id']))])
        self.assertEqual(self.api.checks, before)
        self.api.merge_result = 200, {'merged': True}
        result = self.run_stage('probe', recovery)
        self.assertEqual(result['outcome'], 'MERGED_PROBE_ONLY', result.get('error'))
        self.assertFalse(result['strict_recovery_verified'])
        self.assertTrue(result['evidence']['merge']['body']['merged'])
        self.assertEqual(result['recovery']['baseline']['source_publication'], self.source)

    def test_still_blocked_and_baseline_already_mergeable_are_distinct(self):
        baseline = self.run_stage('baseline')
        recovery = self.run_stage('recover', baseline)
        result = self.run_stage('probe', recovery)
        self.assertEqual(result['outcome'], 'STILL_BLOCKED_PROBE_ONLY')
        self.api.merge_result = 200, {'merged': True}
        result = self.run_stage('baseline')
        self.assertEqual(result['outcome'], 'BASELINE_MERGED_NO_RECOVERY_NEEDED')
        self.assertFalse(result['eligible'])

    def test_changed_source_old_proof_or_wrong_source_attempt_stops_before_writes(self):
        baseline = self.run_stage('baseline')
        original_api, original_source = copy.deepcopy(self.api), copy.deepcopy(self.source)
        for change in ('source', 'producer', 'attempt', 'base', 'required', 'foreign'):
            with self.subTest(change=change):
                api, source = copy.deepcopy(original_api), copy.deepcopy(original_source)
                self.api, self.source = api, source
                candidate = copy.deepcopy(baseline)
                if change == 'source': self.source['qualification']['gate_status'] = 'failure'
                elif change == 'producer': candidate['producer']['run_id'] = '399'
                elif change == 'attempt': self.api.runs[200]['run_attempt'] = 2
                elif change == 'base': self.api.base_sha = 'e' * 40
                elif change == 'required': self.api.required = False
                else: self.api.checks[11]['conclusion'] = 'neutral'
                self.api.calls.clear()
                self.assertFalse(self.run_stage('recover', candidate)['all_passed'])
                self.assertFalse(any(m in ('PUT','PATCH') for m, p, d in self.api.calls))
    def test_waiting_partial_failure_and_old_success_cannot_be_positive_source(self):
        for index in (0, 1, 2):
            api, source = fixture.published(index)
            with patch.object(supp, 'SOURCE_DIGEST', supp.digest(source)):
                result = supp.execute('baseline', self.manifest, source, None, api,
                                      dict(self.env, GITHUB_JOB='supplement_baseline'))
            self.assertFalse(result['all_passed'])
            self.assertFalse(any(m == 'PUT' for m, p, d in api.calls))

    def test_transport_disallows_gate_post_other_patch_merge_payload_and_other_repo(self):
        env = dict(self.env, GITHUB_JOB='supplement_recover', GH_TOKEN='unit-test-only')
        client = supp.API('recover', self.manifest, self.source, env)
        with patch.object(client, '_send', return_value=(200, {})) as transport:
            for method, path, payload in (
                ('POST', fixture.P + '/check-runs', {}),
                ('PATCH', fixture.P + '/check-runs/42', {}),
                ('PUT', fixture.P + '/pulls/13/merge', {'sha': 'e'*40}),
                ('PATCH', '/repos/vllm-project/semantic-router/check-runs/42', {})):
                with self.assertRaises(fixture.lab.LabError): client.request(method, path, payload)
            self.assertFalse(transport.called)

    def test_new_driver_attempt_or_sha_mismatch_stops_before_api_calls(self):
        for changes in ({'GITHUB_RUN_ATTEMPT':'2'}, {'GITHUB_SHA':'c'*40},
                        {'GITHUB_RUN_ID':'300'}, {'GITHUB_JOB':'supplement_recover'}):
            env = dict(self.env, GITHUB_JOB='supplement_baseline')
            env.update(changes); self.api.calls.clear()
            result = supp.execute('baseline', self.manifest, self.source, None, self.api, env)
            self.assertFalse(result['all_passed'])
            self.assertEqual(self.api.calls, [])

    def test_seed_suite_may_change_after_patch_without_rewriting_cancellation_provenance(self):
        baseline = self.run_stage('baseline')
        recovery = self.run_stage('recover', baseline)
        self.api.suites[501]['conclusion'] = 'success'
        self.api.merge_result = 200, {'merged': True}
        result = self.run_stage('probe', recovery)
        self.assertEqual(result['outcome'], 'MERGED_PROBE_ONLY', result.get('error'))
        self.assertEqual(result['evidence']['native']['seed_suite']['conclusion'], 'success')
        self.assertEqual(self.source['native']['seed_suite']['conclusion'], 'cancelled')
        self.assertFalse(result['strict_recovery_verified'])


if __name__ == '__main__':
    unittest.main()
