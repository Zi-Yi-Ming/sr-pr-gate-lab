"""One bounded A merge-decision supplement; never certifies strict recovery.

The original failed baseline and frozen source/controller are preserved. This
driver may only PATCH the original successful Gate after an exact Gate-specific
ordinary-merge refusal. Missing rollup remains missing evidence throughout.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import sys

import ab_lab as ab
import lab

EXPERIMENT = 'merge-decision-supplement-a-v1'
CONTROLLER_REF = 'lab/ab-supplement-a'
SOURCE_RUN_ID = '44048669391'
SOURCE_DIGEST = '48640b36fd5efbef6f09fcca603d1b4a180e506add3f3ff93b7af6ce0aecf9ac'
STAGES = ('baseline', 'recover', 'probe')
require = lab.require


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def validate_source(source):
    require(digest(source) == SOURCE_DIGEST, 'source publication canonical SHA256 mismatch')
    envelope = source['envelope']
    ab.validate_publication(source, envelope)
    require(source['producer']['run_id'] == SOURCE_RUN_ID, 'wrong source artifact run')
    case = ab.selected(envelope)
    require(case['case_id'] == 'A_success' and case['route'] == 'A' and case['fixture'] == 'success',
            'supplement only accepts the frozen complete-success A source')
    require(source['qualification']['gate_status'] == 'success'
            and not source['qualification']['deferred']
            and all(c['status'] == 'passed' for c in source['qualification']['contracts']),
            'all current contracts must actually have passed')
    return envelope


def validate_context(command, manifest, env):
    require(command in STAGES and type(manifest) is dict
            and set(manifest) == {'experiment', 'stage', 'controller_sha'}
            and manifest['experiment'] == EXPERIMENT and manifest['stage'] == 'supplement'
            and type(manifest['controller_sha']) is str and lab.SHA_RE.fullmatch(manifest['controller_sha']),
            'invalid supplemental identity')
    require(env.get('GITHUB_REPOSITORY') == lab.REPOSITORY
            and env.get('GITHUB_EVENT_NAME') == 'workflow_dispatch'
            and env.get('GITHUB_RUN_ATTEMPT') == '1'
            and type(env.get('GITHUB_RUN_ID')) is str and lab.POSITIVE_RE.fullmatch(env['GITHUB_RUN_ID'])
            and env['GITHUB_RUN_ID'] != SOURCE_RUN_ID
            and env.get('GITHUB_SHA') == manifest['controller_sha']
            and env.get('GITHUB_REF') == 'refs/heads/' + CONTROLLER_REF
            and env.get('GITHUB_JOB') == 'supplement_' + command,
            'supplement must use its own original run/attempt/source/job')
    if env.get('GITHUB_WORKFLOW_REF'):
        require(env['GITHUB_WORKFLOW_REF'] == lab.REPOSITORY + '/' + ab.WORKFLOW + '@refs/heads/' + CONTROLLER_REF,
                'supplement workflow identity mismatch')


class API(lab.GitHubAPI):
    """No Gate POST, dispatch, rerun, suite edit, policy edit, or admin operation."""
    _send = ab.API._send

    def __init__(self, command, manifest, source, env):
        validate_context(command, manifest, env)
        self.envelope = validate_source(source)
        super().__init__(env)
        self.command, self.source, self.writes = command, source, set()

    def request(self, method, path, data=None):
        require(self._repository == lab.REPOSITORY, 'lab repository binding changed')
        envelope, source = self.envelope, self.source
        case, current = ab.selected(envelope), source['initial_check']
        allowed = False
        if method == 'GET':
            exact = [lab.PREFIX + '/pulls/' + str(case['pr']),
                     lab.PREFIX + '/git/ref/heads/' + case['base_ref'],
                     lab.PREFIX + '/check-runs/' + str(current['id']), ab.workflow_runs_path(envelope),
                     lab.PREFIX + '/actions/runs/' + envelope['qualification_run_id'] + '/jobs?per_page=100']
            exact += [lab.PREFIX + '/actions/runs/' + r for r in
                      (SOURCE_RUN_ID, envelope['seed_run_id'], envelope['qualification_run_id'])]
            exact += [lab.PREFIX + '/check-suites/' + str(s) for s in
                      (current['check_suite']['id'], source['native']['seed_suite']['id'],
                       source['native']['qualification_suite']['id'])]
            exact += [lab.PREFIX + '/commits/' + case['head'] + '/check-runs?filter=' + f + '&per_page=100'
                      for f in ('all', 'latest')]
            allowed = path in exact or re.fullmatch(re.escape(lab.PREFIX) + r'/git/commits/[a-f0-9]{40}', path) is not None
        elif method == 'POST' and path == '/graphql':
            allowed = data == {'query': lab.GRAPHQL_QUERY, 'variables':
                               {'node': current['node_id'], 'pr': case['pr'], 'head': case['head']}}
        elif method == 'PATCH' and self.command == 'recover':
            allowed = (path == lab.PREFIX + '/check-runs/' + str(current['id'])
                       and data == ab.check_payload(envelope, source['qualification'], patch=True))
        elif method == 'PUT' and self.command in ('baseline', 'probe'):
            allowed = (path == lab.PREFIX + '/pulls/' + str(case['pr']) + '/merge'
                       and data == {'sha': case['head'], 'merge_method': 'merge'})
        require(allowed, 'operation is outside the supplemental lab allowlist')
        if method != 'GET' and path != '/graphql':
            require((method, path) not in self.writes, 'duplicate mutation is forbidden')
            self.writes.add((method, path))
        return self._send(method, path, data)


def fresh_native(client, source, target, *, after_patch=False):
    envelope = source['envelope']
    run = ab.read(client, lab.PREFIX + '/actions/runs/' + SOURCE_RUN_ID)
    p = source['producer']
    require(run.get('id') == int(SOURCE_RUN_ID) and run.get('status') == 'completed'
            and run.get('conclusion') == 'failure' and run.get('run_attempt') == 1
            and run.get('event') == 'workflow_dispatch' and run.get('path') == ab.WORKFLOW
            and run.get('head_sha') == p['sha'] and 'refs/heads/' + run.get('head_branch', '') == p['ref']
            and run.get('repository', {}).get('full_name') == lab.REPOSITORY
            and run.get('head_repository', {}).get('full_name') == lab.REPOSITORY,
            'original publication workflow provenance changed')
    target['source_run'] = run
    native = target['native'] = {}
    for key, id_key in (('seed_run','seed_run_id'), ('qualification_run','qualification_run_id')):
        native[key] = ab.read(client, lab.PREFIX + '/actions/runs/' + envelope[id_key])
    for key, run_key in (('seed_suite','seed_run'), ('qualification_suite','qualification_run')):
        native[key] = ab.read(client, lab.PREFIX + '/check-suites/' + str(native[run_key]['check_suite_id']))
    native['jobs'] = ab.bounded(ab.read(client, lab.PREFIX + '/actions/runs/' + envelope['qualification_run_id'] + '/jobs?per_page=100'), 'jobs')
    native['workflow_runs'] = ab.bounded(ab.read(client, ab.workflow_runs_path(envelope)), 'workflow_runs')
    if after_patch:
        # The PATCH may change its owning suite's aggregate. Preserve that live
        # observation, and validate cancellation provenance from the pinned source.
        ab.validate_suite(native['seed_suite'], source['native']['seed_suite']['id'], ab.selected(envelope))
        provenance = dict(native, seed_suite=source['native']['seed_suite'])
        ab.validate_native(provenance, envelope)
    else:
        ab.validate_native(native, envelope)


def positive_shape(snapshot, source):
    envelope = source['envelope']; current = source['initial_check']
    data = ab.graphql_data(snapshot, envelope, current)
    ab.validate_suite(snapshot['gate_suite'], current['check_suite']['id'], ab.selected(envelope), 'cancelled')
    ab.validate_suite(snapshot['qualification_suite'], source['native']['qualification_suite']['id'], ab.selected(envelope), 'success')
    require(current['check_suite']['id'] == source['native']['seed_suite']['id'], 'original Gate is not in cancelled seed suite')
    obj = data['repository']['object']
    require('statusCheckRollup' in obj and obj['statusCheckRollup'] is None,
            'supplement specifically requires recorded null rollup; strict experiment remains separate')


def stage_proof(document, command, source, manifest, env):
    require(type(document) is dict and document.get('kind') == 'supplement-' + command
            and document.get('manifest') == manifest and document.get('source_digest') == SOURCE_DIGEST
            and document.get('producer') == ab.producer(env) and document.get('all_passed') is True
            and document.get('strict_recovery_verified') is False,
            'upstream supplemental proof is from another run/attempt/source or incomplete')
    envelope = source['envelope']; current = source['initial_check']
    if command == 'baseline':
        require(document.get('source_publication') == source, 'baseline source publication changed')
        require(document.get('outcome') == 'GATE_BLOCKED_MERGE_PROBE_ONLY' and document.get('eligible') is True,
                'no eligible supplemental baseline')
        evidence = document['evidence']
        positive_shape(evidence['before'], source)
        ab.unchanged(source['after_checks'], evidence['before']['all_checks'])
        ab.unchanged(evidence['before']['all_checks'], evidence['after']['all_checks'])
        require(ab.gate_refusal(evidence['merge']), 'baseline is not an exact PR Gate HTTP405')
        ab.negative_safe(evidence['after'], ab.selected(envelope))
    elif command == 'recover':
        stage_proof(document['baseline'], 'baseline', source, manifest, env)
        require(document.get('outcome') == 'PATCHED_PROBE_ONLY', 'no successful exact PATCH proof')
        mutation = document['evidence']['mutation']
        require(mutation.get('status') == 200, 'PATCH was not HTTP200')
        ab.validate_check(mutation['body'], envelope, receipt=current)
        ab.unchanged(document['evidence']['before']['all_checks'], document['evidence']['after']['all_checks'], changed=(current['id'],))


def merge_probe(client, source, evidence):
    envelope = source['envelope']; case = ab.selected(envelope)
    ab.frozen_target(client, envelope, evidence, 'base_ref_before_merge')
    result = evidence['merge'] = {}
    ab.request(client, 'PUT', lab.PREFIX + '/pulls/' + str(case['pr']) + '/merge',
               {'sha': case['head'], 'merge_method': 'merge'}, result)
    return result


def execute(command, manifest, source, document=None, client=None, env=None):
    env = os.environ if env is None else env
    report = {'kind': 'supplement-' + command, 'manifest': copy.deepcopy(manifest), 'source_digest': SOURCE_DIGEST,
              'producer': ab.producer(env), 'strict_recovery_verified': False,
              'all_passed': False, 'eligible': False, 'outcome': 'INCONCLUSIVE', 'evidence': {}}
    try:
        validate_context(command, manifest, env)
        envelope = validate_source(source); current = source['initial_check']; case = ab.selected(envelope)
        if command != 'baseline': stage_proof(document, 'baseline' if command == 'recover' else 'recover', source, manifest, env)
        client = client or API(command, manifest, source, env)
        evidence = report['evidence']
        if command == 'baseline': report['source_publication'] = copy.deepcopy(source)
        elif command == 'probe': report['recovery'] = copy.deepcopy(document)
        fresh_native(client, source, evidence, after_patch=command == 'probe')
        before = evidence['before'] = {}
        ab.snapshot(client, envelope, source, current, before)
        expected = source['after_checks'] if command == 'baseline' else document['evidence']['after']['all_checks']
        ab.unchanged(expected, before['all_checks'])
        if command != 'probe': positive_shape(before, source)
        if command == 'recover':
            report['baseline'] = copy.deepcopy(document)
            ab.frozen_target(client, envelope, evidence, 'base_ref_before_mutation')
            mutation = evidence['mutation'] = {}
            ab.request(client, 'PATCH', lab.PREFIX + '/check-runs/' + str(current['id']),
                       ab.check_payload(envelope, source['qualification'], patch=True), mutation)
            require(mutation['status'] == 200, 'exact PATCH was not HTTP200')
            ab.validate_check(mutation['body'], envelope, receipt=current)
            after = evidence['after'] = {}
            ab.snapshot(client, envelope, source, current, after)
            ab.unchanged(before['all_checks'], after['all_checks'], changed=(current['id'],))
            report.update(outcome='PATCHED_PROBE_ONLY', all_passed=True)
            return report
        merge = merge_probe(client, source, evidence)
        after = evidence['after'] = {}
        ab.snapshot(client, envelope, source, current, after, required=False, state='raw')
        ab.unchanged(before['all_checks'], after['all_checks'])
        if ab.merged_response(merge):
            ab.record_merge_commit(client, envelope, merge, after)
            ab.validate_merge_commit(after, envelope, merge)
            ab.validate_pr(after['pr'], case, merged=True)
            report.update(outcome='BASELINE_MERGED_NO_RECOVERY_NEEDED' if command == 'baseline' else 'MERGED_PROBE_ONLY', all_passed=True)
        else:
            ab.negative_safe(after, case)
            require(ab.gate_refusal(merge), 'ordinary merge refusal is not exact PR Gate HTTP405')
            report.update(outcome='GATE_BLOCKED_MERGE_PROBE_ONLY' if command == 'baseline' else 'STILL_BLOCKED_PROBE_ONLY',
                          all_passed=True, eligible=command == 'baseline')
    except Exception as exc:
        report.update(outcome=getattr(exc, 'outcome', 'INCONCLUSIVE'), all_passed=False, eligible=False, error=str(exc))
        if isinstance(exc, lab.PermissionError): report['permission_error'] = {'status': exc.status, 'body': exc.body}
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=STAGES)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--input', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        require((args.input is not None) == (args.command != 'baseline'), 'stage input mismatch')
        source = lab.parse_json(args.source.read_text(encoding='utf-8'))
        document = lab.parse_json(args.input.read_text(encoding='utf-8')) if args.input else None
        manifest = lab.parse_json(os.environ.get('LAB_AB_MANIFEST', 'null'))
        report = execute(args.command, manifest, source, document)
    except Exception as exc:
        report = {'outcome': 'INCONCLUSIVE', 'all_passed': False, 'eligible': False,
                  'strict_recovery_verified': False, 'error': str(exc)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as f:
            f.write('eligible=' + str(report.get('eligible', False)).lower() + '\n')
    return 0 if report.get('all_passed') is True else 1


if __name__ == '__main__':
    sys.exit(main())
