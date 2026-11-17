"""The versioned B continuation preserves legacy strict evidence semantics."""
import copy
import unittest

import test_ab_lab as f
import ab_lab as ab

B2_REF = 'lab/ab-b2-controller'


def envelope(index=4, stage='driver'):
    e = f.envelope(index, stage)
    e['controller_ref'] = B2_REF
    for c in e['cases'][4:]:
        c['base_ref'] = 'lab-target/b2-' + c['fixture']
        c['head_ref'] = 'lab/ab-b2-' + c['fixture']
    return e


def api_for(index):
    api = f.FakeAPI(index)
    api.case = copy.deepcopy(envelope(index)['cases'][index])
    for run in api.runs.values(): run['head_branch'] = api.case['head_ref']
    return api


def run(stage, index, api, document=None):
    e = envelope(index, 'qualification' if stage == 'qualify' else 'driver')
    return ab.execute(stage, e, api, document, f.environment(e))


def published(index):
    api = api_for(index)
    q = run('qualify', index, api)
    return api, run('publish', index, api, q)


class B2Tests(unittest.TestCase):
    def test_only_fixed_b2_refs_and_b_indices_are_accepted(self):
        ab.validate_envelope(envelope())
        for change in ('index', 'refs', 'controller'):
            e = envelope()
            if change == 'index': e['index'] = 0
            elif change == 'refs': e['cases'][4]['head_ref'] = 'lab/ab-b-waiting'
            else: e['controller_ref'] = 'lab/unrelated'
            with self.assertRaises(f.lab.LabError): ab.validate_envelope(e)

    def test_pending_partial_and_real_failure_remain_blocked_despite_old_success(self):
        for index in (4, 5, 6):
            api, publication = published(index)
            baseline = run('baseline', index, api, publication)
            self.assertTrue(baseline['eligible'], baseline.get('error'))
            recovery = run('recover', index, api, baseline)
            result = run('probe', index, api, recovery)
            self.assertEqual(result['outcome'], 'PASS', result.get('error'))
            self.assertFalse(api.merged)
            current = recovery['authoritative_check']
            self.assertEqual((current['status'], current['conclusion']),
                             ('completed', 'failure') if index == 6 else ('in_progress', None))

    def test_success_with_null_rollup_uses_explicit_merge_only_outcomes(self):
        api, publication = published(7); api.null_rollup = True
        baseline = run('baseline', 7, api, publication)
        self.assertEqual(baseline['outcome'], 'ELIGIBLE_MERGE_PROBE_ONLY', baseline.get('error'))
        self.assertFalse(baseline['evidence']['strict_recovery_verified'])
        recovery = run('recover', 7, api, baseline)
        self.assertTrue(recovery['all_passed'], recovery.get('error'))
        old = publication['initial_check']; new = recovery['authoritative_check']
        self.assertNotEqual(old['id'], new['id'])
        self.assertEqual(api.checks[old['id']], old)
        result = run('probe', 7, api, recovery)
        self.assertEqual(result['outcome'], 'MERGED_PROBE_ONLY', result.get('error'))
        self.assertFalse(result['evidence']['strict_recovery_verified'])
        advanced = run('advance', 7, api, result)
        self.assertTrue(advanced['batch_complete'], advanced.get('error'))
        forged = copy.deepcopy(result); forged['outcome'] = 'RECOVERED'
        self.assertFalse(run('advance', 7, api, forged)['all_passed'])

    def test_other_required_context_cannot_authorize_replacement(self):
        api, publication = published(7); api.null_rollup = True
        api.merge_result = 405, {'message':'Required status check "Security" is expected.'}
        baseline = run('baseline', 7, api, publication)
        self.assertFalse(baseline['eligible'])
        api.calls.clear()
        self.assertFalse(run('recover', 7, api, baseline)['all_passed'])
        self.assertFalse(any(m == 'POST' and p.endswith('/check-runs') for m,p,d in api.calls))

    def test_qualification_attempt_change_after_publication_blocks_post(self):
        api, publication = published(7)
        baseline = run('baseline', 7, api, publication)
        api.runs[200]['run_attempt'] = 2; api.calls.clear()
        result = run('recover', 7, api, baseline)
        self.assertFalse(result['all_passed'])
        self.assertFalse(any(m == 'POST' and p.endswith('/check-runs') for m,p,d in api.calls))

    def test_complete_baseline_merge_skips_replacement_and_can_end_batch(self):
        api, publication = published(7); api.null_rollup = True
        api.merge_result = 200, {'merged':True}
        baseline = run('baseline', 7, api, publication)
        self.assertEqual(baseline['outcome'], 'BASELINE_MERGED_NO_RECOVERY_NEEDED', baseline.get('error'))
        self.assertFalse(baseline['eligible'])
        self.assertTrue(run('advance', 7, api, baseline)['batch_complete'])

    def test_missing_requiredness_or_changed_other_check_blocks_final_merge(self):
        for change in ('required', 'foreign'):
            api, publication = published(7); api.null_rollup = True
            baseline = run('baseline', 7, api, publication)
            recovery = run('recover', 7, api, baseline)
            if change == 'required': api.required = False
            else: api.checks[11]['conclusion'] = 'neutral'
            api.calls.clear()
            result = run('probe', 7, api, recovery)
            self.assertFalse(result['all_passed'])
            self.assertFalse(any(m == 'PUT' for m,p,d in api.calls))

    def test_seed_suite_can_change_but_qualification_provenance_stays_fixed(self):
        api, publication = published(7); api.null_rollup = True
        baseline = run('baseline', 7, api, publication)
        recovery = run('recover', 7, api, baseline)
        api.suites[501]['conclusion'] = 'success'
        result = run('probe', 7, api, recovery)
        self.assertEqual(result['outcome'], 'MERGED_PROBE_ONLY', result.get('error'))
        self.assertEqual(result['evidence']['native_before_merge']['seed_suite']['conclusion'], 'success')
        self.assertEqual(publication['native']['seed_suite']['conclusion'], 'cancelled')

    def test_current_qualification_change_after_post_blocks_merge(self):
        for change in ('attempt', 'failure', 'pending'):
            api, publication = published(7); api.null_rollup = True
            baseline = run('baseline', 7, api, publication)
            recovery = run('recover', 7, api, baseline)
            if change == 'attempt': api.runs[200]['run_attempt'] = 2
            elif change == 'failure': api.runs[200]['conclusion'] = 'failure'
            else: api.runs[200]['status'] = 'in_progress'
            api.calls.clear()
            result = run('probe', 7, api, recovery)
            self.assertFalse(result['all_passed'])
            self.assertFalse(any(m == 'PUT' for m,p,d in api.calls))

    def test_replacement_in_cancelled_suite_is_observed_and_can_remain_blocked(self):
        api, publication = published(7); api.null_rollup = True
        baseline = run('baseline', 7, api, publication)
        request = api.request
        def attach_to_seed(method, path, data=None):
            status, body = request(method, path, data)
            if method == 'POST' and path == f.P + '/check-runs':
                api.checks[body['id']]['check_suite'] = {'id':501}
                body = copy.deepcopy(api.checks[body['id']])
            return status, body
        api.request = attach_to_seed
        recovery = run('recover', 7, api, baseline)
        self.assertTrue(recovery['all_passed'], recovery.get('error'))
        self.assertEqual(recovery['authoritative_check']['check_suite']['id'], 501)
        result = run('probe', 7, api, recovery)
        self.assertEqual(result['outcome'], 'STILL_BLOCKED_PROBE_ONLY', result.get('error'))
        self.assertFalse(result['evidence']['strict_recovery_verified'])
        self.assertTrue(run('advance', 7, api, result)['batch_complete'])


if __name__ == '__main__': unittest.main()
