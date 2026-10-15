"""Neutral execution facts for discovery. No baseline classifier dependency."""

from collections import defaultdict
from copy import deepcopy
import json

from .schema import Trace


def extract_observations(trace: Trace) -> dict:
    # Allowlist source fields: arbitrary producer metadata (including reviewer
    # annotations) stays in the local provenance artifact, outside LLM inputs.
    fields = {
        "tool_call": ("event_index", "type", "call_id", "tool", "arguments"),
        "tool_result": ("event_index", "type", "call_id", "status", "output", "exit_code"),
        "assistant_final": ("event_index", "type", "text"),
    }
    events = []
    for position, source in enumerate(trace.events):
        event = {k: deepcopy(source[k]) for k in fields[source["type"]] if k in source}
        if trace.source_record is not None and source["type"] == "tool_result":
            raw = trace.source_record["events"][position]
            event.update(stdout=raw["stdout"], stderr=raw["stderr"])
        events.append(event)
    calls = [e for e in events if e["type"] == "tool_call"]
    results = {e["call_id"]: e for e in events if e["type"] == "tool_result"}
    identical = defaultdict(list)
    previous = {}
    changes = []
    for call in calls:
        key = json.dumps([call["tool"], call["arguments"]], sort_keys=True)
        identical[key].append(call)
        before = previous.get(call["tool"])
        if before and before["arguments"] != call["arguments"]:
            changes.append({
                "basis": "consecutive_calls_to_same_tool_not_assumed_same_goal",
                "from_call_id": before["call_id"], "to_call_id": call["call_id"],
                "event_indices": [before["event_index"], call["event_index"]],
                "changed_argument_keys": sorted(k for k in before["arguments"].keys() | call["arguments"].keys()
                                                if (k in before["arguments"]) != (k in call["arguments"])
                                                or before["arguments"].get(k) != call["arguments"].get(k)),
            })
        previous[call["tool"]] = call
    return {
        "trace_id": trace.trace_id, "user_request": trace.user_request, "events": events,
        "repeated_identical_calls": [
            {"call_ids": [c["call_id"] for c in group],
             "event_indices": [c["event_index"] for c in group]}
            for group in identical.values() if len(group) > 1],
        "argument_changes": changes,
        "missing_result_call_ids": [c["call_id"] for c in calls if c["call_id"] not in results],
        "final_response_present": any(e["type"] == "assistant_final" for e in events),
    }
