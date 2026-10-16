"""Real-model benchmark entrypoint with immutable run identity and provenance."""
from datetime import datetime, timezone
from pathlib import Path
import uuid

from . import VERSION
from .approval import digest, load_approved
from .model_client import BoundedCodexClient
from .reports import prepare_output, write_artifacts
from .runner import ModelAgent, HARNESSES, HARNESS_VERSION, run_episode
from ..review_gate import file_hash


def implementation_hash():
    root = Path(__file__).resolve().parents[1]
    files = list((root / 'benchmark').glob('*.py')) + [root / n for n in
            ('llm.py','proposal_schema.py','proposal_contract.py','discovery_schema.py','review_gate.py')]
    return digest({str(p.relative_to(root)): file_hash(p) for p in sorted(files)})


def benchmark(specs_path, output, *, model, harness, trials=1, max_steps=None, timeout_seconds=60,
              manifest_path=None, client=None, progress=print):
    if not model.strip() or harness not in HARNESSES or type(trials) is not int or not 1 <= trials <= 100:
        raise ValueError('require a model, a known harness and 1..100 trials')
    if max_steps is not None and (type(max_steps) is not int or max_steps < 1):
        raise ValueError('max_steps must be a positive integer')
    proposals, provenance = load_approved(specs_path, manifest_path)
    output = prepare_output(output)
    client = client or BoundedCodexClient(timeout_seconds)
    run_id = 'benchmark-' + uuid.uuid4().hex
    metadata = {"schema_version": 1, "run_kind": "real_model_benchmark", "run_id": run_id,
                "created_at": datetime.now(timezone.utc).isoformat(), "implementation_version": VERSION,
                "implementation_sha256": implementation_hash(), "model": model,
                "model_version_note": "Requested model ID recorded; provider does not expose immutable weight revision through this adapter.",
                "client_config": client.cache_identity, "harness": harness, "harness_version": HARNESS_VERSION,
                "harness_instructions": HARNESSES[harness], "harness_sha256": digest(HARNESSES[harness]),
                "trials": trials, "expected_episodes": trials * len(proposals), "budgets": {p['proposal_id']: min(max_steps or p['eval_private']['environment_model']['max_steps'],
                                                                    p['eval_private']['environment_model']['max_steps']) for p in proposals},
                "provenance": provenance}
    episodes, results = [], []
    for trial in range(1, trials+1):
        for p in proposals:
            progress(f"{harness}: trial {trial}/{trials}, {p['proposal_id']}")
            episode, result = run_episode(p, ModelAgent(client, model, harness), model=model, harness=harness,
                                         run_id=run_id, trial_id=trial, provenance=provenance, max_steps=max_steps)
            episodes.append(episode);results.append(result)
            write_artifacts(output, metadata, episodes, results)
            progress(f"  {result['execution_status']}; {result['steps']} tool calls; goal={result['observed_goal']}")
    return 1 if any(r['error'] for r in results) else 0
