"""Compare paired real-model trials only after verifying their fixed inputs."""
from pathlib import Path

from ..review_gate import file_hash, load_object, require
from ..proposal_schema import DIMENSIONS
from .approval import write_json
from .reports import prepare_output, summary
from .grader import LIMITATION


def load_run(directory):
    directory = Path(directory)
    metadata = load_object(directory / 'run_metadata.json')
    require(metadata['run_kind'] == 'real_model_benchmark', 'scripted qualification cannot be compared as model performance')
    for name in ('results.json', 'episodes.jsonl', 'errors.jsonl'):
        require(file_hash(directory / name) == metadata['artifact_hashes'][name], 'run artifact hash mismatch')
    payload = load_object(directory / 'results.json')
    require(payload['run_kind'] == metadata['run_kind'] and payload['run_id'] == metadata['run_id'], 'run identity mismatch')
    results = payload['results']
    keys = [(r['proposal_id'], r['trial_id']) for r in results]
    expected = {(p, trial) for p in metadata['budgets'] for trial in range(1,metadata['trials']+1)}
    require(len(keys) == len(set(keys)) and set(keys) == expected, 'incomplete or duplicate trial matrix')
    for r in results:
        require(r['model'] == metadata['model'] and r['harness'] == metadata['harness'] and r['run_id'] == metadata['run_id'], 'episode configuration mismatch')
        require(r['max_steps'] == metadata['budgets'][r['proposal_id']], 'episode budget mismatch')
        require(all(r['provenance'][k] == v for k,v in metadata['provenance'].items()), 'episode provenance mismatch')
        require((r['scores'] is not None) == (r['execution_status'] == 'completed'), 'error episodes cannot have scores')
    return metadata, results


def compare(baseline, candidate, output):
    left, a = load_run(baseline)
    right, b = load_run(candidate)
    for key in ('model', 'client_config', 'provenance', 'budgets', 'trials', 'implementation_version', 'implementation_sha256', 'harness_version'):
        require(left[key] == right[key], f'incompatible comparison: {key} differs')
    require(left['harness'] == 'baseline' and right['harness'] == 'recovery_guidance', 'expected baseline versus recovery_guidance')
    by_key = {(r['proposal_id'], r['trial_id']): r for r in b}
    pairs = []
    for r in a:
        c = by_key[r['proposal_id'],r['trial_id']]
        require(r['provenance'] == c['provenance'], 'paired specification provenance mismatch')
        scored = r['scores'] is not None and c['scores'] is not None
        deltas = {d: int(c['scores']['dimensions'][d])-int(r['scores']['dimensions'][d]) for d in DIMENSIONS} if scored else None
        pairs.append({'proposal_id':r['proposal_id'], 'trial_id':r['trial_id'],
                      'baseline_status':r['execution_status'], 'candidate_status':c['execution_status'],
                      'baseline_dimensions':r['scores']['dimensions'] if r['scores'] else None,
                      'candidate_dimensions':c['scores']['dimensions'] if c['scores'] else None,
                      'dimension_deltas':deltas,
                      'regressions':[d for d,v in deltas.items() if v<0] if scored else [],
                      'improvements':[d for d,v in deltas.items() if v>0] if scored else []})
    matched_a = [r for r in a if r['scores'] is not None and by_key[r['proposal_id'],r['trial_id']]['scores'] is not None]
    matched_b = [by_key[r['proposal_id'],r['trial_id']] for r in matched_a]
    output = prepare_output(output)
    report = {'schema_version':1, 'status':'compared' if matched_a else 'inconclusive_no_scored_pairs',
              'baseline_run_id':left['run_id'], 'candidate_run_id':right['run_id'], 'model':left['model'],
              'provenance':left['provenance'], 'baseline_metadata_sha256':file_hash(Path(baseline)/'run_metadata.json'),
              'candidate_metadata_sha256':file_hash(Path(candidate)/'run_metadata.json'),
              'harnesses':{k:{f:m[f] for f in ('harness','harness_version','harness_sha256','harness_instructions')} for k,m in [('baseline',left),('candidate',right)]},
              'all_attempts':{'baseline':summary(a),'candidate':summary(b)},
              'paired_complete_executions':{'baseline':summary(matched_a),'candidate':summary(matched_b)},
              'pairs':pairs, 'scored_pairs':len(matched_a),'unscored_pairs':len(a)-len(matched_a),
              'regression_count':sum(bool(p['regressions']) for p in pairs),
              'improved_pair_count':sum(bool(p['improvements']) for p in pairs),
              'interpretation':'Descriptive smoke benchmark only. No statistically meaningful improvement is established, including from a single trial.',
              'limitation':LIMITATION}
    write_json(output/'comparison.json',report)
    lines=['# Harness comparison','',f"Model: {left['model']}. Paired scored episodes: {len(matched_a)}/{len(a)}.",'',report['interpretation'],'',LIMITATION,'']
    for pair in pairs:
        lines.append(f"- {pair['proposal_id']} / trial {pair['trial_id']}: " +
                     (', '.join(f"{d}: {pair['baseline_dimensions'][d]} → {pair['candidate_dimensions'][d]}" for d in DIMENSIONS)
                      if pair['dimension_deltas'] is not None else f"unscored ({pair['baseline_status']} / {pair['candidate_status']})"))
    for label, rows in [('Baseline',a),('Recovery guidance',b)]:
        agg=summary(rows)['aggregate']
        lines += ['',f"{label}: feasible completion {agg['feasible_completion']['passed']}/{agg['feasible_completion']['total']}; "
                  f"nonrecoverable handling {agg['nonrecoverable_handling_success']['passed']}/{agg['nonrecoverable_handling_success']['total']}; "
                  f"execution errors {agg['execution_errors']}/{agg['attempted_episodes']}."]
    lines += ['', 'Errors are unscored; dimension deltas use only matched completed executions. Raw completion across all tasks includes the deliberately impossible task and is distinct from feasible completion.']
    (output/'comparison.md').write_text('\n'.join(lines)+'\n')
    return 0 if len(matched_a)==len(a) else 1
