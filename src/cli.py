"""CLI for the stdlib baseline and optional Codex-assisted discovery."""

import argparse
import json
from pathlib import Path
import sys

from .analysis import analyze_trace
from .classification import classify
from .clustering import group_analyses
from .schema import read_jsonl


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Analyze raw or normalized agent traces deterministically.")
    commands = parser.add_subparsers(dest="command", required=True)
    analyze = commands.add_parser("analyze")
    analyze.add_argument("--input", required=True, type=Path)
    analyze.add_argument("--output", required=True, type=Path)
    analyze.add_argument("--input-format", choices=("auto", "raw", "normalized"), default="auto")
    discover = commands.add_parser("discover", help="Discover behavioral patterns using an LLM")
    discover.add_argument("--input", required=True, type=Path)
    discover.add_argument("--output", required=True, type=Path)
    discover.add_argument("--model", required=True)
    discover.add_argument("--input-format", choices=("auto", "raw", "normalized"), default="auto")
    discover.add_argument("--cache-dir", type=Path)
    discover.add_argument("--max-attempts", type=int, default=3)
    propose = commands.add_parser("propose", help="Construct review-gated evaluation task proposals")
    for flag in ("taxonomy", "discoveries", "review", "traces", "output"):
        propose.add_argument("--" + flag, required=True, type=Path)
    propose.add_argument("--cluster", required=True)
    propose.add_argument("--model", required=True)
    propose.add_argument("--cache-dir", type=Path)
    propose.add_argument("--max-attempts", type=int, default=3)
    args = parser.parse_args(argv)
    if args.command == "propose":
        from .eval_proposal import run_proposals
        from .llm import CodexClient
        try:
            code = run_proposals(args.taxonomy, args.discoveries, args.review, args.traces, args.output,
                                 cluster_id=args.cluster, model=args.model,
                                 client=CodexClient(max_attempts=args.max_attempts), cache_dir=args.cache_dir,
                                 progress=lambda message: print(message, file=sys.stderr))
            print(f"Proposals finished with exit code {code}. Review: {args.output / 'proposal_review.md'}")
            return code
        except (OSError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    if args.command == "discover":
        from .discovery import run_discovery
        from .llm import CodexClient
        try:
            client = CodexClient(max_attempts=args.max_attempts)
            code = run_discovery(args.input, args.output, model=args.model, client=client,
                                 input_format=args.input_format, cache_dir=args.cache_dir,
                                 progress=lambda message: print(message, file=sys.stderr))
            print(f"Discovery finished with exit code {code}. Review: {args.output / 'discovery_review.md'}")
            return code
        except (OSError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    analyses = []
    errors = []
    try:
        destinations = [args.output / name for name in
                        ("trace_analysis.jsonl", "failure_clusters.json", "validation_errors.jsonl")]
        if args.input.resolve() in {path.resolve() for path in destinations}:
            raise ValueError("input must not be an output file")
        for item in read_jsonl(args.input, args.input_format):
            if item.error is not None:
                errors.append(item.error)
                print(f"{args.input}:{item.line_number}: {item.error['message']}", file=sys.stderr)
                continue
            trace = item.trace
            assert trace is not None
            observations = analyze_trace(trace)
            analyses.append({
                "schema_version": 2, "trace_id": trace.trace_id,
                "user_request": trace.user_request, "source_line": item.line_number,
                "source_format": "raw" if trace.source_record is not None else "normalized",
                **({"source_record": trace.source_record} if trace.source_record is not None else {}),
                "observations": observations, "classification": classify(observations),
            })
        args.output.mkdir(parents=True, exist_ok=True)
        destinations[0].write_text("".join(json.dumps(a, ensure_ascii=True) + "\n" for a in analyses), encoding="utf-8")
        clusters = group_analyses(analyses)
        clusters["invalid_record_count"] = len(errors)
        destinations[1].write_text(json.dumps(clusters, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
        destinations[2].write_text("".join(json.dumps(e, ensure_ascii=True) + "\n" for e in errors), encoding="utf-8")
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"Analyzed {len(analyses)} traces; {len(errors)} invalid records. Output: {args.output}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
