"""Independent label groups; membership is evidence, not a session failure verdict."""

from typing import Any


def group_analyses(analyses: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[str, dict[str, list[dict[str, Any]]]] = {
        "error_families": {}, "behavior_patterns": {},
    }
    patterns = {}
    for analysis in analyses:
        for dimension in groups:
            for evidence in analysis["classification"]["label_evidence"][dimension]:
                groups[dimension].setdefault(evidence["label"], []).append({
                    "trace_id": analysis["trace_id"],
                    "source_line": analysis["source_line"],
                    "event_indices": evidence["event_indices"],
                })
        for evidence in analysis["classification"]["failure_patterns"]:
            key = (evidence["error_family"], evidence["response_behavior"])
            members = patterns.setdefault(key, {})
            member = members.setdefault(analysis["trace_id"], {
                "trace_id": analysis["trace_id"], "source_line": analysis["source_line"],
                "failed_call_ids": set(), "event_indices": set(),
            })
            member["failed_call_ids"].add(evidence["failed_call_id"])
            member["event_indices"].update(evidence["event_indices"])
    return {
        "schema_version": 2,
        "analyzed_trace_count": len(analyses),
        "groups": {dimension: [
            {"label": label, "trace_count": len(members),
             "members": sorted(members, key=lambda m: m["trace_id"])}
            for label, members in sorted(labels.items())
        ] for dimension, labels in groups.items()},
        "failure_patterns": [
            {"error_family": family, "response_behavior": behavior,
             "trace_count": len(members), "members": [
                 {**member, "failed_call_ids": sorted(member["failed_call_ids"]),
                  "event_indices": sorted(member["event_indices"])}
                 for _, member in sorted(members.items())]}
            for (family, behavior), members in sorted(patterns.items())],
    }
