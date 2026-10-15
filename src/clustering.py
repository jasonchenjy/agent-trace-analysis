"""Independent label groups; membership is evidence, not a session failure verdict."""

from typing import Any


def group_analyses(analyses: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[str, dict[str, list[dict[str, Any]]]] = {
        "error_families": {}, "behavior_patterns": {},
    }
    for analysis in analyses:
        for dimension in groups:
            for evidence in analysis["classification"]["label_evidence"][dimension]:
                groups[dimension].setdefault(evidence["label"], []).append({
                    "trace_id": analysis["trace_id"],
                    "source_line": analysis["source_line"],
                    "event_indices": evidence["event_indices"],
                })
    return {
        "schema_version": 1,
        "analyzed_trace_count": len(analyses),
        "groups": {dimension: [
            {"label": label, "trace_count": len(members),
             "members": sorted(members, key=lambda m: m["trace_id"])}
            for label, members in sorted(labels.items())
        ] for dimension, labels in groups.items()},
    }
