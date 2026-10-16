"""Held-out critic runs. Expected issue labels never enter production payloads.

Run explicitly with a real client; Fake Client unit tests are orchestration tests.
"""
import argparse
import json
from pathlib import Path

from .discovery import atomic_write
from .eval_validity import StageCache, assess, CRITIC_VERSION
from .llm import CodexClient, LLMError
from .proposal_schema import validate_proposal
from .schema import ValidationError
from .validity_schema import validate_construction
from .validity_checks import offline_checks


def run_regressions(fixtures, context, output, *, model, client, cache_dir=None, progress=None):
    output = Path(output)
    if (output / 'critic_regression_results.json').exists():
        raise ValueError('regression output exists; use a fresh directory')
    cache = StageCache(cache_dir or output / 'cache', client, model)
    results, errors = [], []
    for path in sorted(Path(fixtures).glob('*.json')):
        try:
            value = json.loads(path.read_text())
            validate_construction(value)
            p = value['proposal']
            validate_proposal(p, context)
            if progress:
                progress(f'Critic regression: {path.name}')
            # No filenames, expected labels or expected findings in the model request.
            assessment = assess(cache, p, context, value['validity_design'], offline_checks(p))
            results.append({'fixture': path.name, 'assessment': assessment})
        except (ValueError, OSError, LLMError, ValidationError) as exc:
            errors.append({'fixture': path.name, 'error_type': type(exc).__name__, 'message': str(exc)})
        atomic_write(output / 'critic_regression_results.json', json.dumps({
            'model': model, 'provider': client.provider, 'critic_version': CRITIC_VERSION,
            'interpretation': 'Observed critic outputs; manual issue matching required. Fake results do not measure detection accuracy.',
            'results': results, 'errors': errors, 'cache': cache.records}, indent=2) + '\n')
    return 1 if errors else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixtures', type=Path, required=True)
    parser.add_argument('--context', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--model', required=True)
    parser.add_argument('--cache-dir', type=Path)
    args = parser.parse_args()
    return run_regressions(args.fixtures, json.loads(args.context.read_text()), args.output,
                           model=args.model, client=CodexClient(), cache_dir=args.cache_dir, progress=print)


if __name__ == '__main__':
    raise SystemExit(main())
