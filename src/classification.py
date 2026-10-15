"""Multi-label rules over observations, independent of input fixtures."""

from collections import defaultdict
from typing import Any


def classify(observations: dict[str, Any]) -> dict[str, Any]:
    family_evidence: dict[str, set[int]] = defaultdict(set)
    behavior_evidence: dict[str, set[int]] = defaultdict(set)
    # Attach behaviors to the failed calls that establish them, rather than
    # multiplying unrelated session-wide error and behavior labels.
    responses: dict[str, list[tuple[str, list[int]]]] = defaultdict(list)
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
        for call_id in repetition["call_ids"]:
            responses[call_id].append(("repeated_failed_operation", repetition["event_indices"]))
    for recovery in observations["recoveries"]:
        behavior_evidence["successful_recovery"].update(recovery["event_indices"])
        for call_id in recovery["failed_call_ids"]:
            responses[call_id].append(("successful_recovery", recovery["event_indices"]))

    for candidate in observations["goal_recovery"]["candidates"]:
        for call_id in candidate["failed_call_ids"]:
            responses[call_id].append(("alternative_recovery_candidate", candidate["event_indices"]))

    final = observations["final_outcome_evidence"]
    claim = final["claimed_outcome"]
    assessment = observations["success_claim_assessment"]
    outcome = "unknown"
    if claim == "incomplete":
        outcome = "reported_incomplete"
    elif assessment["status"] in {"verified", "contradicted", "unverified"}:
        outcome = assessment["status"] + "_success_claim"
        if assessment["status"] == "contradicted":
            behavior_evidence["unsupported_success_claim"].update(assessment["event_indices"])
            for call_id in assessment["call_ids"]:
                responses[call_id].append(("unsupported_success_claim", assessment["event_indices"]))
    if not behavior_evidence:
        behavior_evidence["unknown"].update(e["event_index"] for e in [final] if e["event_index"] is not None)

    def labels(evidence: dict[str, set[int]]) -> list[dict[str, Any]]:
        return [{"label": label, "event_indices": sorted(indices)}
                for label, indices in sorted(evidence.items())]

    patterns = []
    for error in observations["tool_errors"]:
        for behavior, indices in responses.get(error["call_id"], [("unknown", error["event_indices"])]):
            for family in error["error_families"]:
                patterns.append({"error_family": family, "response_behavior": behavior,
                                 "failed_call_id": error["call_id"],
                                 "event_indices": sorted(set(error["event_indices"] + indices))})

    return {
        "error_families": sorted(family_evidence),
        "behavior_patterns": sorted(behavior_evidence),
        "label_evidence": {"error_families": labels(family_evidence),
                           "behavior_patterns": labels(behavior_evidence)},
        "capability_gap_candidates": candidates,
        "failure_patterns": patterns,
        "outcome": outcome,
    }
