"""Multi-label rules over observations, independent of input fixtures."""

from collections import defaultdict
from typing import Any


def classify(observations: dict[str, Any]) -> dict[str, Any]:
    family_evidence: dict[str, set[int]] = defaultdict(set)
    behavior_evidence: dict[str, set[int]] = defaultdict(set)
    candidates = []
    for error in observations["tool_errors"]:
        for family in error["error_families"]:
            family_evidence[family].update(error["event_indices"])
        if "network_permission_denied" in error["error_families"]:
            candidates.append({
                "kind": "network_access", "status": "candidate",
                "reason": "A tool reported a network permission barrier; cause and scope are unverified.",
                "call_id": error["call_id"], "event_indices": error["event_indices"],
            })
    for repetition in observations["repeated_failed_operations"]:
        behavior_evidence["repeated_failed_operation"].update(repetition["event_indices"])
    for recovery in observations["recoveries"]:
        behavior_evidence["successful_recovery"].update(recovery["event_indices"])

    final = observations["final_outcome_evidence"]
    claim = final["claimed_outcome"]
    refs = final["referenced_results"]
    # Only explicit evidence links establish which tool results the final claim
    # purports to rely on. An unrelated failed subtask must not contradict success.
    contradictory = [ref for ref in refs if ref["status"] == "error"]
    outcome = "unknown"
    if claim == "success" and contradictory:
        behavior_evidence["unsupported_success_claim"].add(final["event_index"])
        for ref in contradictory:
            behavior_evidence["unsupported_success_claim"].update(ref["event_indices"])
        outcome = "contradicted_success_claim"
    elif claim == "incomplete":
        outcome = "reported_incomplete"
    elif claim == "success" and refs and all(ref["status"] == "success" for ref in refs):
        outcome = "tool_supported_success_claim"
    if not behavior_evidence:
        behavior_evidence["unknown"].update(e["event_index"] for e in [final] if e["event_index"] is not None)

    def labels(evidence: dict[str, set[int]]) -> list[dict[str, Any]]:
        return [{"label": label, "event_indices": sorted(indices)}
                for label, indices in sorted(evidence.items())]

    return {
        "error_families": sorted(family_evidence),
        "behavior_patterns": sorted(behavior_evidence),
        "label_evidence": {"error_families": labels(family_evidence),
                           "behavior_patterns": labels(behavior_evidence)},
        "capability_gap_candidates": candidates,
        "outcome": outcome,
    }
