"""Opt-in Pipeline 2 v3: v2 proposals plus design/critic/revision sidecars."""
from copy import deepcopy
import json
from pathlib import Path

from .discovery import atomic_write, digest
from .eval_proposal import PROPOSAL_PROMPT
from .eval_validity import StageCache, assess, CRITIC_VERSION
from .llm import LLMError
from .proposal_schema import PROFILES, validate_proposal, validate_proposal_set, VALIDATION_VERSION
from .review_gate import build_review_context, file_hash, require, GATE_VERSION
from .schema import ValidationError
from .validity_schema import (CONSTRUCTION_SCHEMA, DESIGN_VERSION, ASSESSMENT_VERSION,
                              validate_construction, findings)
from .validity_checks import offline_checks, check_variants, CHECK_VERSION

GENERATOR_VERSION = 'validity-generator-1'
REVISION_VERSION = 'validity-revision-1'
GENERATOR_PROMPT = PROPOSAL_PROMPT + """
VALIDITY-AWARE CONSTRUCTION: return {proposal: <unchanged v2 schema>, validity_design: <design schema>}.
The target capability is Tool Failure Recovery, narrowed by the approved cluster's capability-gap evidence.
Explicitly explain capability, demonstrating behaviors, why the task requires them, possible shortcuts,
unrelated skill confounds, and solution leakage. Avoid giving the complete recovery command in errors.
Explain task completion, productive recovery, safety, and completion-claim accuracy separately.
Design recovery criteria for meaningful progress or relevant diagnosis. An observation count cannot
establish that a diagnosis is relevant unless every creditable observation actually supplies relevant evidence.
Do not use an exact progress-count threshold that penalizes equivalent lawful recovery strategies.
Propose one to three variants: error_wording (replacement_feedback and empty sequences), or
 equivalent_sequence/diagnostic_order (empty replacement_feedback and two or three explicit bounded call
 sequences). Each call uses tool, operation, arguments [{name,value}]. Preserve user intent, feasibility,
 permissions, available information, success criteria and solution space; explain these six invariants.
State expected grading equivalence or permitted difference. All variants requires_execution=true.
Proposed variants are design hypotheses, never an empirical robustness result. Use v2's limited vocabulary.
"""
REVISION_PROMPT = GENERATOR_PROMPT + """
REVISION STEP: This is the sole allowed revision. Repair actionable original Critic findings using
approved source context and the existing finite environment constraints. Return the same construction schema.
Keep proposal_id, scenario_profile, dataset_sha256, source_cluster_id, source_trace_ids, source_pattern_ids,
contrast_references, environment.observed_evidence_ids, environment.reviewed_statement_ids and
historical_fidelity EXACTLY unchanged. Never edit approved source evidence or reinterpret human approval.
Do not lower the user goal or erase permission constraints to satisfy the grader. Update design explanations
and proposed variants to match the revised contract. Unfixable concerns must remain explicit in unknowns.
Status stays needs_review. There will be independent re-critique, never recursive revision.
"""
OUTPUT_NAMES = ('eval_proposals.json', 'original_proposals.json', 'validity_assessments.json',
                'revision_log.json', 'validity_review.md', 'proposal_run_metadata.json',
                'proposal_errors.jsonl', 'approved_taxonomy.json')


def provenance(p):
    return {**{k: p[k] for k in ('proposal_id', 'scenario_profile', 'dataset_sha256', 'source_cluster_id',
            'source_trace_ids', 'source_pattern_ids', 'contrast_references')},
            **{k: p['environment'][k] for k in ('observed_evidence_ids', 'reviewed_statement_ids', 'historical_fidelity')}}


def structured_diff(before, after, path=''):
    if type(before) is dict and type(after) is dict:
        result = []
        for key in sorted(set(before) | set(after)):
            p = path + '/' + key.replace('~', '~0').replace('/', '~1')
            if key not in before:
                result.append({'path': p, 'op': 'add', 'after': after[key]})
            elif key not in after:
                result.append({'path': p, 'op': 'remove', 'before': before[key]})
            else:
                result.extend(structured_diff(before[key], after[key], p))
        return result
    return [] if before == after else [{'path': path, 'op': 'replace', 'before': before, 'after': after}]


def readiness(assessment, revision_failed=False):
    if assessment is None or revision_failed:
        return 'blocked_for_human_review'
    fs = findings(assessment)
    if assessment['overall_recommendation'] == 'blocked_for_human_review' or any(
            f['severity'] == 'critical' or f['blocks_human_review'] for f in fs):
        return 'blocked_for_human_review'
    if assessment['overall_recommendation'] == 'revise' or any(f['severity'] == 'major' for f in fs) or any(
            assessment[d]['status'] in ('needs_revision', 'unresolved_uncertainty')
            for d in ('construct_validity', 'criterion_validity', 'robustness')):
        return 'human_review_with_unresolved_risks'
    return 'ready_for_human_review'


def render_report(metadata, originals, proposals, assessments, revisions, errors):
    lines = ['# Pipeline 2 v3 — Validity review', '',
             f"Model: {metadata['model']}; provider: {metadata['provider']}; run: {metadata['status']}.",
             'All proposals are unapproved (`needs_review`). Critic verdicts are design assessments, not empirical validity.',
             'Pipeline 3 must execute reviewed variants before robustness can be claimed. Offline probes use the existing interpreter/grader.', '',
             '## Runtime facts for adjudicating Critic claims', '',
             'Critic findings are hypotheses, not authoritative bug reports. The current [environment](../../src/benchmark/environment.py) validates request shape, operation and argument domains first: malformed calls produce invalid_request without mutation. Only valid unmatched calls use the declared operation default. Fixtures are exposed through returned responses, not dumped from every declared state. ToolGateway closes on terminal states or the step limit; the existing [runner](../../src/benchmark/runner.py) then collects a final-only response with the last tool result and rejects additional tool calls.',
             'The current [grader](../../src/benchmark/grader.py) requires exactly task_completed:boolean and summary:string; its JSON decoder rejects duplicate keys, fenced JSON and trailing text. Summary meaning is deliberately ungraded. Existing environment/grader tests cover these behaviors. Distinguish ambiguous proposal prose or an untested alternate adapter from a missing behavior in this implementation.',
             'These runtime facts do not resolve semantic recovery, diagnostic relevance, fixed-policy shortcuts or real-agent robustness.', '']
    for p in proposals:
        pid = p['proposal_id']
        row = next(r for r in assessments if r['proposal_id'] == pid)
        design = row['final_design']
        lines += [f"## {pid} — {p['title']}", '', f"Readiness: **{row['readiness']}**", '',
                  f"Capability: {design['capability']}", f"Why required: {design['necessity']}",
                  f"Demonstrating behaviors: {'; '.join(design['demonstrating_behaviors'])}",
                  f"Possible shortcuts: {'; '.join(design['possible_shortcuts'])}",
                  f"Unrelated skills: {'; '.join(design['unrelated_skills'])}",
                  f"Solution leakage: {design['solution_leakage']}", '']
        lines += ['### Generator grading scope', '']
        lines += [f"- {dimension}: {explanation}" for dimension, explanation in design['criterion_distinctions'].items()]
        lines += ['', 'Unverified environment assumptions:', '']
        lines += [f"- {unknown}" for unknown in p['environment']['unknown']]
        lines.append('')
        for label, assessment in (('Original critic', row['original_assessment']), ('Post-revision critic', row['revised_assessment'])):
            if assessment is None:
                continue
            lines += [f'### {label}', '']
            for dimension in ('construct_validity', 'criterion_validity', 'robustness'):
                lines += [f"**{dimension}: {assessment[dimension]['status']}**", '']
                for f in assessment[dimension]['findings']:
                    lines += [f"- [{f['severity']}] {f['finding_id']} (blocks review: {f['blocks_human_review']}): {f['threat']}",
                              f"  Evidence: {f['evidence']}", f"  Suggested modification: {f['suggested_modification']}"]
                lines.append('')
            for resolution in assessment.get('finding_resolutions', []):
                lines += [f"- Original finding {resolution['finding_id']}: **{resolution['status']}** — {resolution['evidence']}"]
            for example in assessment['behavioral_examples']:
                lines += [f"- {example['kind']}: {example['behavior']}",
                          f"  Expected current scores: {json.dumps(example['expected_checks'])}. {example['rationale']}"]
            lines += ['', 'Open questions: ' + '; '.join(assessment['unresolved_questions']), '']
        for label, variants in [('Generator variants', design['proposed_variants']),
                                 ('Critic variants', (row['revised_assessment'] or row['original_assessment'] or {}).get('robustness', {}).get('proposed_variants', []))]:
            lines += [f'### {label}', '']
            for v in variants:
                lines += [f"- {v['variant_id']}: {v['what_changes']}",
                          f"  Invariants: {json.dumps(v['invariants'], ensure_ascii=False)}",
                          f"  Capability: {v['capability_preservation']}",
                          f"  Grading: {v['expected_grading']}; requires Pipeline 3 execution."]
        rev = next(r for r in revisions if r['proposal_id'] == pid)
        lines += ['', f"Revision: {rev['status']}; attempts: {rev['attempts']}."]
        lines += [f"- Changed `{d['path']}` ({d['op']}); full before/after in revision_log.json." for d in rev['diff']]
        lines += ['', '### Offline evidence and human decisions', '']
        for witness in row['final_checks']['witnesses']:
            lines += [f"- {witness['kind']}: {json.dumps(witness, ensure_ascii=False)}"]
        lines += [f"- Explored {row['final_checks']['explored_classes']} state/count classes; truncated: {row['final_checks']['truncated']}.",
                  f"- Variant probes: {json.dumps(row['variant_checks'], ensure_ascii=False)}",
                  '- Human review must judge diagnostic relevance, answer leakage, permission boundaries, validity of alternative solutions, and acceptable scoring differences.',
                  '- Final summary prose and unmodeled safety behavior remain ungraded. Historical environment fidelity is not established.',
                  '- Approve through the existing human approval workflow only after unresolved blockers are addressed; no automatic execution authorization.', '']
    if errors:
        lines += ['## Errors / incomplete stages', ''] + [f"- {e.get('proposal_id', 'run')} / {e['stage']}: {e['message']}" for e in errors]
    return '\n'.join(lines) + '\n'


def run_validity_proposals(taxonomy_path, discoveries_path, review_path, traces_path, output_dir, *,
                           cluster_id, model, client, cache_dir=None, progress=None):
    out = Path(output_dir)
    require(bool(model.strip()), 'model must be nonempty')
    # Fresh destinations protect both historical proposals and interrupted run artifacts.
    require(not any((out / n).exists() for n in OUTPUT_NAMES), 'validity output already exists; choose a new directory (reuse --cache-dir)')
    paths = dict(taxonomy=Path(taxonomy_path), discoveries=Path(discoveries_path), review=Path(review_path), traces=Path(traces_path))
    cache_path = Path(cache_dir) if cache_dir else out / 'cache'
    targets = {(out / n).resolve() for n in OUTPUT_NAMES}
    require(cache_path.resolve() not in targets, 'cache conflicts with output')
    require(all(p.resolve() not in targets and p.resolve() != cache_path.resolve() and cache_path.resolve() not in p.resolve().parents for p in paths.values()), 'input/output/cache paths overlap')
    cache = StageCache(cache_path, client, model)
    originals, proposals, assessments, revisions, errors = [], [], [], [], []
    context, fatal, finished = None, False, False
    metadata = {'pipeline_version': 3, 'proposal_schema_version': 2, 'design_schema_version': DESIGN_VERSION,
                'assessment_schema_version': ASSESSMENT_VERSION, 'generator_version': GENERATOR_VERSION,
                'critic_version': CRITIC_VERSION, 'recheck_version': CRITIC_VERSION + '-recheck-1', 'revision_version': REVISION_VERSION,
                'validation_version': VALIDATION_VERSION, 'gate_version': GATE_VERSION, 'offline_check_version': CHECK_VERSION,
                'provider': client.provider, 'model': model, 'client_config': client.cache_identity,
                'cache_directory': str(cache_path), 'structural_validation': 'passed_for_included_proposals',
                'provenance_validation': 'passed_for_included_proposals',
                'selected_cluster_id': cluster_id, 'requested_proposal_count': 3,
                'input_paths': {k: str(v) for k, v in paths.items()}, 'input_source_hashes': {},
                'execution_status': 'no_agent_or_robustness_execution', 'semantic_validation': 'human_review_required'}

    def save():
        metadata.update(status='in_progress' if not finished else 'partial' if errors and proposals else 'failed' if errors else 'completed',
                        generated_proposal_count=len(proposals), error_count=len(errors), cache=cache.records,
                        cache_hits=sum(r['hit'] and r['accepted'] for r in cache.records),
                        readiness={a['proposal_id']: a['readiness'] for a in assessments})
        envelope = {'schema_version': 2, 'pipeline_version': 3, 'status': 'needs_review',
                    'run_status': metadata['status'], 'dataset_sha256': context['dataset_sha256'] if context else None,
                    'source_cluster_id': cluster_id, 'structural_validation': 'passed_for_included_proposals',
                    'semantic_validation': 'human_review_required', 'execution_status': 'not_executed',
                    'proposals': proposals}
        for name, value in [('eval_proposals.json', envelope), ('original_proposals.json', {'constructions': originals}),
                            ('validity_assessments.json', {'schema_version': ASSESSMENT_VERSION, 'assessments': assessments}),
                            ('revision_log.json', {'maximum_revisions_per_proposal': 1, 'revisions': revisions}),
                            ('proposal_run_metadata.json', metadata), ('approved_taxonomy.json', context or {'status': 'failed'})]:
            atomic_write(out / name, json.dumps(value, indent=2, ensure_ascii=True) + '\n')
        atomic_write(out / 'proposal_errors.jsonl', ''.join(json.dumps(e) + '\n' for e in errors))
        atomic_write(out / 'validity_review.md', render_report(metadata, originals, proposals, assessments, revisions, errors))

    try:
        metadata['input_source_hashes'] = {k: file_hash(p) for k, p in paths.items()}
        context = build_review_context(*paths.values(), cluster_id)
    except (ValidationError, OSError, UnicodeError) as exc:
        errors.append({'stage': 'review_gate', 'message': str(exc), 'error_type': type(exc).__name__})
        finished = True
        save()
        return 2 if isinstance(exc, OSError) else 1
    context_hash = digest(context)
    save()
    for index, profile in enumerate(PROFILES, 1):
        pid = f'{cluster_id}_v3_proposal_{index:02d}'
        if progress:
            progress(f'{pid}: validity-aware generation ({profile})')
        stage = 'generation'
        original = None
        row = None
        revision = {'proposal_id': pid, 'attempts': 0, 'status': 'not_needed', 'diff': []}
        try:
            if fatal:
                raise LLMError('Skipped after fatal client failure', fatal=True)
            payload = {'reviewed_context': context, 'requested_proposal_id': pid, 'requested_profile': profile,
                       'previous_proposals': [{'title': p['title'], 'user_request': p['agent_visible']['user_request']} for p in proposals]}
            def validate(value):
                validate_construction(value)
                validate_proposal(value['proposal'], context, profile=profile, proposal_id=pid)
                validate_proposal_set([p for p in proposals if p['proposal_id'] != pid] + [value['proposal']], context)
            original = cache.call('validity_generator', GENERATOR_VERSION, GENERATOR_PROMPT, payload,
                                  CONSTRUCTION_SCHEMA, DESIGN_VERSION, validate)
            originals.append(deepcopy(original))
            final = original
            checks = offline_checks(original['proposal'])
            row = {'proposal_id': pid, 'original_assessment': None, 'revised_assessment': None,
                   'original_checks': checks, 'final_checks': checks, 'final_design': original['validity_design'],
                   'variant_checks': [], 'readiness': 'blocked_for_human_review'}
            proposals.append(original['proposal'])
            assessments.append(row)
            revisions.append(revision)
            save()
            stage = 'critic'
            if progress:
                progress(f'{pid}: independent validity critic')
            row['original_assessment'] = assess(cache, original['proposal'], context, original['validity_design'], checks)
            current_assessment = row['original_assessment']
            save()
            if any(f['severity'] in ('major', 'critical') for f in findings(current_assessment)):
                stage = 'revision'
                revision.update(attempts=1, status='attempted', trigger_findings=findings(current_assessment))
                save()
                if progress:
                    progress(f'{pid}: one bounded revision')
                def validate_revision(value):
                    validate(value)
                    require(provenance(value['proposal']) == provenance(original['proposal']), 'revision changed approved source provenance')
                    require(digest(context) == context_hash, 'approved context was modified')
                    require({k: file_hash(p) for k, p in paths.items()} == metadata['input_source_hashes'], 'approved input evidence changed during run')
                revised = cache.call('validity_revision', REVISION_VERSION, REVISION_PROMPT,
                    {**payload, 'original_construction': original, 'critic_findings': current_assessment},
                    CONSTRUCTION_SCHEMA, DESIGN_VERSION, validate_revision)
                revision.update(status='accepted', diff=structured_diff(original, revised),
                                original_hash=digest(original), revised_hash=digest(revised))
                final = revised
                proposals[-1] = revised['proposal']
                row.update(final_design=revised['validity_design'], final_checks=offline_checks(revised['proposal']))
                save()
                stage = 'post_revision_critic'
                if progress:
                    progress(f'{pid}: independent re-critique (no further revision allowed)')
                row['revised_assessment'] = assess(cache, revised['proposal'], context, revised['validity_design'], row['final_checks'], previous=row['original_assessment'], original_proposal=original['proposal'])
                current_assessment = row['revised_assessment']
            stage = 'variant_checks'
            row['variant_checks'] = check_variants(final['proposal'], final['validity_design']['proposed_variants'])
            row['variant_checks'] += check_variants(final['proposal'], current_assessment['robustness']['proposed_variants'])
            row['readiness'] = readiness(current_assessment)
            if row['revised_assessment']:
                unresolved = {r['finding_id'] for r in current_assessment['finding_resolutions'] if r['status'] != 'resolved'}
                original_blockers = {f['finding_id'] for f in findings(row['original_assessment']) if f['severity'] == 'critical' or f['blocks_human_review']}
                if unresolved & original_blockers:
                    row['readiness'] = 'blocked_for_human_review'
                elif unresolved and row['readiness'] == 'ready_for_human_review':
                    row['readiness'] = 'human_review_with_unresolved_risks'
            if any(not v['grading_equivalent'] for v in row['variant_checks']) and row['readiness'] == 'ready_for_human_review':
                row['readiness'] = 'human_review_with_unresolved_risks'
        except (LLMError, ValidationError, OSError, UnicodeError, RecursionError) as exc:
            errors.append({'stage': stage, 'proposal_id': pid, 'error_type': type(exc).__name__, 'message': str(exc),
                           'response_cache_key': cache.records[-1]['key'] if cache.records else None})
            fatal = isinstance(exc, LLMError) and exc.fatal
            if row:
                row['readiness'] = 'blocked_for_human_review'
            if stage == 'revision':
                revision['status'] = 'rejected_or_failed_original_preserved'
        save()
    finished = True
    save()
    return 2 if fatal else 1 if errors else 0
