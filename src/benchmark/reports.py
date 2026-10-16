"""Auditable artifacts and metrics with explicit error/feasibility denominators."""
from collections import defaultdict
from pathlib import Path
import json

from ..proposal_schema import DIMENSIONS
from .approval import write_json, digest
from .grader import LIMITATION


def rate(values):
    return {"passed": sum(values), "total": len(values), "rate": sum(values) / len(values) if values else None}


def aggregate(results):
    scored = [r["scores"] for r in results if r["scores"] is not None]
    feasible = [s for s in scored if s["feasible"]]
    impossible = [s for s in scored if not s["feasible"]]
    return {"attempted_episodes": len(results), "scored_episodes": len(scored),
            "execution_errors": sum(r["execution_status"] != "completed" for r in results),
            "dimensions": {d: rate([s["dimensions"][d] for s in scored]) for d in DIMENSIONS},
            "feasible_completion": rate([s["dimensions"]["task_completion"] for s in feasible]),
            "feasible_task_success": rate([s["task_success"] for s in feasible]),
            "nonrecoverable_handling_success": rate([s["handling_success"] for s in impossible]),
            "scoring_denominator": "Completed executions only; errors remain explicit and are never scored as task failures."}


def summary(results):
    by_task = defaultdict(list)
    for result in results:
        by_task[result["proposal_id"]].append(result)
    return {"aggregate": aggregate(results), "per_task": {k: aggregate(v) for k, v in by_task.items()}}


def prepare_output(output):
    output = Path(output)
    if output.exists() and any(output.iterdir()):
        raise ValueError("output directory is not empty; use a fresh directory to preserve run provenance")
    output.mkdir(parents=True, exist_ok=True)
    return output


def write_artifacts(output, metadata, episodes, results):
    payload = {"schema_version": 1, "run_kind": metadata["run_kind"], "run_id": metadata["run_id"],
               "results": results, **summary(results), "limitation": LIMITATION}
    write_json(output / "results.json", payload)
    (output / "episodes.jsonl").write_text("".join(json.dumps(e, ensure_ascii=True) + "\n" for e in episodes))
    errors = [{"run_id": r["run_id"], "proposal_id": r["proposal_id"], "trial_id": r["trial_id"],
               "execution_status": r["execution_status"], **r["error"]} for r in results if r["error"]]
    (output / "errors.jsonl").write_text("".join(json.dumps(e) + "\n" for e in errors))
    from ..review_gate import file_hash
    metadata["artifact_hashes"] = {n: file_hash(output / n) for n in ("results.json", "episodes.jsonl", "errors.jsonl")}
    metadata["execution_status"] = ("running" if len(results) < metadata.get("expected_episodes", len(results))
                                    else "completed_with_errors" if errors else "completed")
    write_json(output / "run_metadata.json", metadata)
    return payload


def dimension_line(result):
    if result["scores"] is None:
        return result["execution_status"]
    return ", ".join(f"{d}={'pass' if result['scores']['dimensions'][d] else 'fail'}" for d in DIMENSIONS)
