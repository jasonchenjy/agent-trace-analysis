"""Fake clients test orchestration/contracts, NEVER semantic model accuracy."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from test_proposals import inputs, gate, proposal_response, request_for, ROOT
from test_discovery import FakeClient as BaseFakeClient
from src.llm import LLMError
from src.schema import ValidationError
from src.eval_validity import CRITIC_PROMPT, StageCache, assess
from src.validity_checks import offline_checks, check_variants
from src.validity_pipeline import run_validity_proposals, OUTPUT_NAMES, provenance, structured_diff
from src.validity_schema import validate_assessment, validate_construction
from src.proposal_schema import validate_proposal


class FakeClient(BaseFakeClient):
    def complete(self, **request):
        raw = super().complete(**request)
        value = json.loads(raw)
        if request['schema_name'] == 'validity_critic' and 'original_assessment' in request['payload']:
            remaining = {f['finding_id'] for d in ('construct_validity', 'criterion_validity', 'robustness') for f in value[d]['findings']}
            value['finding_resolutions'] = [
                {'finding_id': f['finding_id'], 'status': 'unresolved' if f['finding_id'] in remaining else 'resolved',
                 'evidence': 'Fake recheck evidence; tests workflow only.'}
                for d in ('construct_validity', 'criterion_validity', 'robustness')
                for f in request['payload']['original_assessment'][d]['findings']]
        return json.dumps(value)


def variant():
    return {'variant_id': 'wording', 'kind': 'error_wording', 'what_changes': 'Rephrase the initial blocker.',
            'invariants': {k: 'Unchanged; only synonymous wording changes.' for k in
                ('user_intent', 'recovery_feasibility', 'permission_boundaries', 'available_information', 'success_criteria', 'valid_solution_space')},
            'capability_preservation': 'The same blocker must be interpreted.',
            'expected_grading': 'Identical scoring for identical successful behavior.',
            'requires_execution': True, 'replacement_feedback': 'The requested tool operation is blocked.', 'sequences': []}


def design():
    return {'capability': 'Tool Failure Recovery', 'demonstrating_behaviors': ['Address the observed blocker.'],
            'necessity': 'The task starts after a failed tool call.', 'possible_shortcuts': ['Guess an option.'],
            'unrelated_skills': ['Read simple tool interfaces.'], 'solution_leakage': 'Inspect diagnostics for complete answers.',
            'criterion_distinctions': {k: 'Independent observable criterion.' for k in
                ('task_completion', 'productive_recovery', 'safety_policy_compliance', 'final_claim_accuracy')},
            'proposed_variants': [variant()]}


def finding(kind='leakage', severity='critical'):
    return {'finding_id': kind, 'severity': severity, 'evidence': 'The error states the exact command.',
            'threat': kind, 'suggested_modification': 'Remove the explicit solution.', 'blocks_human_review': severity == 'critical'}


def assessment(p, fs=None):
    return {'proposal_id': p['proposal_id'],
            'construct_validity': {'status': 'needs_revision' if fs else 'design_check_passed', 'findings': fs or []},
            'criterion_validity': {'status': 'design_check_passed', 'findings': []},
            'robustness': {'status': 'requires_execution', 'findings': [], 'proposed_variants': [variant()]},
            'behavioral_examples': [{'kind': kind, **deepcopy(p['eval_private']['validation'][kind])} for kind in ('good', 'bad', 'superficial')],
            'unresolved_questions': ['Execute controlled variants with agents.'],
            'overall_recommendation': 'revise' if fs else 'ready_for_human_review'}


def responder(request):
    if request['schema_name'] == 'validity_generator':
        return {'proposal': proposal_response(request), 'validity_design': design()}
    if request['schema_name'] == 'validity_revision':
        return deepcopy(request['payload']['original_construction'])
    return assessment(request['payload']['proposal'])


def run(inputs, out, client, **kw):
    return run_validity_proposals(*inputs, out, cluster_id='cluster_01', model=kw.pop('model', 'test-model'), client=client, **kw)


def read(out, name):
    return json.loads((out / name).read_text())


def test_three_candidates_separate_critic_and_cache(inputs, tmp_path):
    client = FakeClient(responder)
    out = tmp_path / 'first'
    assert run(inputs, out, client) == 0
    assert all((out / name).exists() for name in OUTPUT_NAMES)
    proposals = read(out, 'eval_proposals.json')['proposals']
    assert len(proposals) == 3 and all(p['status'] == 'needs_review' and p['schema_version'] == 2 for p in proposals)
    critics = [r for r in client.requests if r['schema_name'] == 'validity_critic']
    assert len(critics) == 3
    assert all(r['instructions'] == CRITIC_PROMPT for r in critics)
    assert 'expected_labels' not in json.dumps(critics) and 'gold_labels' not in json.dumps(critics)
    assert all('approved_target_capability_gap' in r['payload'] for r in critics)
    assert run(inputs, tmp_path / 'cached', FakeClient([]), cache_dir=out / 'cache') == 0
    with pytest.raises(ValidationError, match='already exists'):
        run(inputs, out, FakeClient([]))


@pytest.mark.parametrize('kind', ['explicit_answer_leakage', 'shortcut_to_passing', 'false_positive',
                                  'equivalent_sequence_false_negative', 'irrelevant_diagnostic_credit'])
def test_fake_critic_findings_are_preserved_and_trigger_revision(inputs, tmp_path, kind):
    def respond(req):
        if req['schema_name'] == 'validity_critic':
            return assessment(req['payload']['proposal'], [finding(kind)])
        return responder(req)
    client = FakeClient(respond)
    assert run(inputs, tmp_path / 'out', client) == 0
    assert len([r for r in client.requests if r['schema_name'] == 'validity_revision']) == 3
    assert len([r for r in client.requests if r['schema_name'] == 'validity_critic']) == 6
    rows = read(tmp_path / 'out', 'validity_assessments.json')['assessments']
    assert all(r['readiness'] == 'blocked_for_human_review' for r in rows)
    assert all(r['original_assessment']['construct_validity']['findings'][0]['finding_id'] == kind for r in rows)
    assert all(r['revised_assessment']['construct_validity']['findings'][0]['finding_id'] == kind for r in rows)
    assert all(r['attempts'] == 1 for r in read(tmp_path / 'out', 'revision_log.json')['revisions'])


@pytest.mark.parametrize('mutation', ['source', 'schema', 'approval', 'extra_context'])
def test_invalid_revision_rejected_original_preserved(inputs, tmp_path, mutation):
    def respond(req):
        if req['schema_name'] == 'validity_critic':
            return assessment(req['payload']['proposal'], [finding()])
        value = responder(req)
        if req['schema_name'] == 'validity_revision':
            if mutation == 'source':
                value['proposal']['environment']['reviewed_statement_ids'].append('invented-review')
            elif mutation == 'schema':
                value['proposal']['eval_private']['environment_model']['max_steps'] = 500
            elif mutation == 'approval':
                value['proposal']['status'] = 'approved'
            else:
                value['approved_source_context'] = {'modified': True}
        return value
    out = tmp_path / 'out'
    assert run(inputs, out, FakeClient(respond)) == 1
    assert read(out, 'eval_proposals.json')['proposals'] == [o['proposal'] for o in read(out, 'original_proposals.json')['constructions']]
    assert all(r['status'] == 'rejected_or_failed_original_preserved' for r in read(out, 'revision_log.json')['revisions'])
    assert all(r['readiness'] == 'blocked_for_human_review' for r in read(out, 'validity_assessments.json')['assessments'])
    assert len(list((out / 'cache').glob('*.raw.txt'))) >= 9


def test_revision_cannot_change_even_valid_provenance(inputs, tmp_path):
    def respond(req):
        if req['schema_name'] == 'validity_critic':
            return assessment(req['payload']['proposal'], [finding()])
        value = responder(req)
        if req['schema_name'] == 'validity_revision':
            # Another real reviewed reference is structurally valid, but violates provenance pin.
            value['proposal']['environment']['reviewed_statement_ids'] = [list(req['payload']['reviewed_context']['review_catalog'])[1]]
        return value
    assert run(inputs, tmp_path / 'out', FakeClient(respond)) == 1
    assert 'approved source provenance' in (tmp_path / 'out' / 'proposal_errors.jsonl').read_text()


def test_successful_revision_diff_and_fresh_recritic(inputs, tmp_path):
    def respond(req):
        if req['schema_name'] == 'validity_critic':
            p = req['payload']['proposal']
            assert 'critic_findings' not in req['payload']
            return assessment(p, None if p['rationale'].endswith('Revised.') else [finding()])
        value = responder(req)
        if req['schema_name'] == 'validity_revision':
            value['proposal']['rationale'] += ' Revised.'
        return value
    out = tmp_path / 'out'
    assert run(inputs, out, FakeClient(respond)) == 0
    log = read(out, 'revision_log.json')['revisions']
    assert all(r['attempts'] == 1 and r['diff'][0]['path'] == '/proposal/rationale' for r in log)
    assert all(r['readiness'] == 'ready_for_human_review' for r in read(out, 'validity_assessments.json')['assessments'])


@pytest.mark.parametrize('stage', ['validity_generator', 'validity_critic', 'validity_revision'])
def test_partial_failures_preserve_artifacts(inputs, tmp_path, stage):
    failed = False
    def respond(req):
        nonlocal failed
        if req['schema_name'] == stage and not failed:
            failed = True
            return LLMError('Simulated transport failure')
        if req['schema_name'] == 'validity_critic':
            return assessment(req['payload']['proposal'], [finding()])
        return responder(req)
    out = tmp_path / stage
    assert run(inputs, out, FakeClient(respond)) == 1
    assert all((out / n).exists() for n in OUTPUT_NAMES)
    assert len(read(out, 'eval_proposals.json')['proposals']) == (2 if stage == 'validity_generator' else 3)
    assert 'Simulated transport failure' in (out / 'proposal_errors.jsonl').read_text()


def test_robustness_only_changes_feedback(inputs):
    p = proposal_response(request_for(gate(inputs)))
    before = deepcopy(p)
    result = check_variants(p, [variant()])
    assert p == before and result[0]['grading_equivalent']
    assert result[0]['requires_pipeline3_execution']
    bad = variant()
    bad['requires_execution'] = False
    with pytest.raises(ValidationError):
        validate_construction({'proposal': p, 'validity_design': {**design(), 'proposed_variants': [bad]}})


def test_malformed_assessment_cannot_claim_robustness(inputs):
    p = proposal_response(request_for(gate(inputs)))
    a = assessment(p)
    a['robustness']['status'] = 'design_check_passed'
    with pytest.raises(ValidationError, match='empirical'):
        validate_assessment(a, p['proposal_id'])


def test_offline_detects_scoring_counterexamples_and_caps_search(inputs):
    p = proposal_response(request_for(gate(inputs)))
    result = offline_checks(p)
    assert {'observation_only_recovery_credit', 'equivalent_goal_scoring_difference'} <= {w['kind'] for w in result['witnesses']}
    assert offline_checks(p, max_nodes=1)['truncated']


def test_historical_benchmark_files_unchanged(inputs, tmp_path):
    paths = [p for folder in ('approved', 'qualification', 'benchmark_baseline', 'benchmark_guided', 'comparison', 'proposals_v2')
             for p in (ROOT / 'outputs' / folder).rglob('*') if p.is_file()]
    paths += list((ROOT / 'reviews').glob('*.json'))
    before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    assert run(inputs, tmp_path / 'out', FakeClient(responder)) == 0
    assert before == {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def test_separate_regression_fixtures_valid_and_not_in_prompt(inputs):
    folder = ROOT / 'tests' / 'fixtures' / 'validity'
    for name in ('explicit_hint.json', 'equivalent_wait.json', 'irrelevant_diagnostic.json'):
        construction = json.loads((folder / name).read_text())
        validate_construction(construction)
        validate_proposal(construction['proposal'], gate(inputs))
        assert name not in CRITIC_PROMPT
    p = json.loads((folder / 'equivalent_wait.json').read_text())['proposal']
    assert 'equivalent_goal_scoring_difference' in {w['kind'] for w in offline_checks(p)['witnesses']}


def test_behavioral_variants_expose_equivalent_wait_scoring_difference():
    construction = json.loads((ROOT / 'tests/fixtures/validity/equivalent_wait.json').read_text())
    p = construction['proposal']
    def wait(ticks):
        return {'tool': 'clock', 'operation': 'wait', 'arguments': [{'name': 'ticks', 'value': ticks}]}
    complete = {'tool': 'check', 'operation': 'run', 'arguments': [{'name': 'target', 'value': 'unit'}]}
    v = {**variant(), 'kind': 'equivalent_sequence', 'replacement_feedback': '',
         'sequences': [[wait(2), complete], [wait(1), wait(1), complete]]}
    validate_construction({**construction, 'validity_design': {**design(), 'proposed_variants': [v]}})
    result = check_variants(p, [v])[0]
    assert all(o['goal'] for o in result['outcomes'])
    assert not result['grading_equivalent']
    assert result['outcomes'][0]['dimensions']['recovery_decision_quality']
    assert not result['outcomes'][1]['dimensions']['recovery_decision_quality']
    assert p == construction['proposal']


def test_invalid_variant_cannot_change_permission_or_call_domain(inputs):
    p = proposal_response(request_for(gate(inputs)))
    call = {'tool': 'workspace', 'operation': 'configure', 'arguments': [{'name': 'key', 'value': 'mode'}, {'name': 'value', 'value': 'admin'}]}
    v = {**variant(), 'kind': 'equivalent_sequence', 'replacement_feedback': '', 'sequences': [[call], [call]]}
    with pytest.raises(ValidationError, match='undeclared'):
        check_variants(p, [v])


def test_nonrecoverable_completion_and_handling_are_separate(inputs):
    p = proposal_response(request_for(gate(inputs), 'nonrecoverable', 2))
    result = offline_checks(p)
    assert result['successful_scoring_classes'] == 0
    witness = next(w for w in result['witnesses'] if w['kind'] == 'observation_only_recovery_credit')
    assert witness['dimensions']['task_completion'] is False
    assert all(witness['dimensions'][d] for d in ('recovery_decision_quality', 'safety_policy_compliance', 'final_response_accuracy'))


def test_post_critic_failure_preserves_revised_and_original(inputs, tmp_path):
    def respond(req):
        if req['schema_name'] == 'validity_critic':
            if req['payload'].get('assessment_phase') == 'post_revision':
                return LLMError('Recheck unavailable')
            return assessment(req['payload']['proposal'], [finding()])
        value = responder(req)
        if req['schema_name'] == 'validity_revision':
            value['proposal']['rationale'] += ' Revised.'
        return value
    out = tmp_path / 'out'
    assert run(inputs, out, FakeClient(respond)) == 1
    assert len(read(out, 'original_proposals.json')['constructions']) == 3
    assert all(p['rationale'].endswith('Revised.') for p in read(out, 'eval_proposals.json')['proposals'])
    assert all(r['readiness'] == 'blocked_for_human_review' for r in read(out, 'validity_assessments.json')['assessments'])


def test_unresolved_original_critical_cannot_disappear(inputs):
    from src.validity_schema import validate_recheck
    p = proposal_response(request_for(gate(inputs)))
    before = assessment(p, [finding()])
    after = assessment(p)
    after['finding_resolutions'] = []
    with pytest.raises(ValidationError, match='every original'):
        validate_recheck(after, p['proposal_id'], before)
    after['finding_resolutions'] = [{'finding_id': 'leakage', 'status': 'uncertain', 'evidence': 'Repair not demonstrated.'}]
    with pytest.raises(ValidationError, match='cannot disappear'):
        validate_recheck(after, p['proposal_id'], before)


def test_cache_bound_to_model_prompt_input_and_schema(inputs, tmp_path):
    from src.validity_schema import ASSESSMENT_SCHEMA
    from src.eval_validity import CRITIC_VERSION
    p = proposal_response(request_for(gate(inputs)))
    client = FakeClient(lambda req: assessment(p))
    cache = StageCache(tmp_path / 'cache', client, 'model-a')
    def call(version='1', payload=None, schema_version='1'):
        return cache.call('critic', version, 'instructions', payload or {'proposal': p}, ASSESSMENT_SCHEMA, schema_version,
                          lambda v: validate_assessment(v, p['proposal_id']))
    call(); call()
    assert len(client.requests) == 1
    call(version='2'); call(payload={'proposal': p, 'extra': 'changed'}); call(schema_version='2')
    assert len(client.requests) == 4
    cache.model = 'model-b'
    call()
    assert len(client.requests) == 5


def test_unapproved_gate_never_calls_llm(inputs, tmp_path):
    client = FakeClient([])
    assert run_validity_proposals(*inputs, tmp_path / 'out', cluster_id='cluster_03', model='test', client=client) == 1
    assert not client.requests
    assert all((tmp_path / 'out' / n).exists() for n in OUTPUT_NAMES)


def test_regression_fake_results_labeled_and_errors_saved(inputs, tmp_path):
    from src.validity_regression import run_regressions
    client = FakeClient(lambda req: LLMError('Unavailable'))
    assert run_regressions(ROOT / 'tests/fixtures/validity', gate(inputs), tmp_path / 'out', model='fake', client=client) == 1
    result = read(tmp_path / 'out', 'critic_regression_results.json')
    assert result['provider'] == 'fake_scripted' and len(result['errors']) == 3
    assert 'Fake results do not measure detection accuracy' in result['interpretation']


def test_corrupt_cache_is_reported_not_a_crash(inputs, tmp_path):
    out = tmp_path / 'first'
    assert run(inputs, out, FakeClient(responder)) == 0
    for path in (out / 'cache').glob('*.json'):
        path.write_text('[]')
    second = tmp_path / 'second'
    assert run(inputs, second, FakeClient(responder), cache_dir=out / 'cache') == 1
    assert 'identity/format mismatch' in (second / 'proposal_errors.jsonl').read_text()


def test_cli_validity_flag_routes_to_new_pipeline(inputs, tmp_path, monkeypatch):
    from src.cli import main
    import src.llm
    monkeypatch.setattr(src.llm, 'CodexClient', lambda **kw: FakeClient(responder))
    arguments = ['propose', '--cluster', 'cluster_01', '--model', 'fake', '--validity-aware', '--output', str(tmp_path / 'cli')]
    for name, path in zip(('taxonomy', 'discoveries', 'review', 'traces'), inputs):
        arguments += ['--' + name, str(path)]
    assert main(arguments) == 0
    assert (tmp_path / 'cli' / 'validity_review.md').exists()


def test_checkpoint_not_completed_before_last_critic(inputs, tmp_path):
    out = tmp_path / 'out'
    def respond(req):
        if req['schema_name'] == 'validity_critic':
            assert read(out, 'proposal_run_metadata.json')['status'] == 'in_progress'
        return responder(req)
    assert run(inputs, out, FakeClient(respond)) == 0
    assert read(out, 'proposal_run_metadata.json')['status'] == 'completed'


def test_explicit_hint_fixture_reaches_independent_fake_critic(inputs, tmp_path):
    construction = json.loads((ROOT / 'tests/fixtures/validity/explicit_hint.json').read_text())
    p = construction['proposal']
    client = FakeClient(lambda req: assessment(req['payload']['proposal'], [finding('answer_leakage')]))
    result = assess(StageCache(tmp_path / 'cache', client, 'fake'), p, gate(inputs), construction['validity_design'], offline_checks(p))
    assert result['construct_validity']['findings'][0]['finding_id'] == 'answer_leakage'
    assert 'workspace.configure(key="mode", value="local")' in client.requests[0]['payload']['proposal']['agent_visible']['checkpoint']['recent_tool_failure']
    assert 'answer_leakage' not in client.requests[0]['instructions']


def test_approved_evidence_file_changed_during_revision_is_rejected(inputs, tmp_path):
    def respond(req):
        if req['schema_name'] == 'validity_critic':
            return assessment(req['payload']['proposal'], [finding()])
        if req['schema_name'] == 'validity_revision':
            inputs[2].write_text(inputs[2].read_text() + '\n')
        return responder(req)
    out = tmp_path / 'out'
    assert run(inputs, out, FakeClient(respond)) == 1
    assert 'approved input evidence changed during run' in (out / 'proposal_errors.jsonl').read_text()
    assert all(r['status'] == 'rejected_or_failed_original_preserved' for r in read(out, 'revision_log.json')['revisions'])
