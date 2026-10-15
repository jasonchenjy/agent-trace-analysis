"""Extract observable facts, keeping original source indices throughout."""

from collections import defaultdict
import json
import re
from typing import Any

from .schema import Trace


def error_families(output: str) -> list[str]:
    """Conservative message signatures, applied only to explicit error results."""
    text = output.lower()
    families = []
    denied = re.search(r"permission denied|operation not permitted|access denied|\bblocked\b", text)
    network = re.search(r"\bnetwork\b|\bsocket\b|\bconnect\b|\bconnection\b|\begress\b|https?://", text)
    if denied and network:
        families.append("network_permission_denied")
    if re.search(r"invalid (?:tool )?arguments?\b|unexpected keyword argument|missing required argument|argument validation failed", text):
        families.append("invalid_tool_arguments")
    if re.search(r"no such file or directory|file not found|filenotfounderror", text):
        families.append("missing_file")
    if re.search(r"\b[1-9]\d* (?:tests? )?failed\b|\btests? failed\b|\bassertionerror\b|^FAILED\s", output, re.IGNORECASE | re.MULTILINE):
        families.append("test_failure")
    return families or ["unknown"]


def _operation(call: dict[str, Any]) -> dict[str, Any]:
    if "operation" in call:
        return {"tool": call["tool"], "basis": "explicit_operation", "value": call["operation"]}
    return {"tool": call["tool"], "basis": "exact_arguments", "value": call["arguments"]}


def _key(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _indices(attempts: list[dict[str, Any]]) -> list[int]:
    return sorted({index for attempt in attempts for index in attempt["event_indices"]})


def analyze_trace(trace: Trace) -> dict[str, Any]:
    calls = {e["call_id"]: e for e in trace.events if e["type"] == "tool_call"}
    results = {e["call_id"]: e for e in trace.events if e["type"] == "tool_result"}
    attempts = []
    errors = []
    operations: dict[str, list[dict[str, Any]]] = defaultdict(list)
    commands: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for call_id, call in calls.items():
        result = results.get(call_id)
        attempt = {
            "call_id": call_id, "tool": call["tool"], "operation": _operation(call),
            "call_event_index": call["event_index"],
            "result_event_index": result["event_index"] if result else None,
            "status": result["status"] if result else "pending",
            "event_indices": [call["event_index"]] + ([result["event_index"]] if result else []),
        }
        attempts.append(attempt)
        operations[_key(attempt["operation"])].append(attempt)
        if result and result["status"] == "error":
            errors.append({**attempt, "output": result["output"],
                           "exit_code": result.get("exit_code"),
                           "error_families": error_families(result["output"])})
            command = call["arguments"].get("command")
            if isinstance(command, str):
                commands[command].append(attempt)

    repeated_commands = [
        {"command": command, "call_ids": [a["call_id"] for a in group],
         "count": len(group), "event_indices": _indices(group)}
        for command, group in commands.items() if len(group) >= 2
    ]
    repeated_operations = []
    recoveries = []
    unresolved = []
    for group in operations.values():
        failures = [a for a in group if a["status"] == "error"]
        if len(failures) >= 2:
            repeated_operations.append({
                "operation": group[0]["operation"], "count": len(failures),
                "call_ids": [a["call_id"] for a in failures], "event_indices": _indices(failures),
            })
        # A successful attempt must START after a failure is observed. Overlapping
        # concurrent calls cannot establish that the agent recovered from an error.
        for success in (a for a in group if a["status"] == "success"):
            recovered = [a for a in failures
                         if a["result_event_index"] < success["call_event_index"]]
            if recovered:
                recoveries.append({
                    "operation": success["operation"],
                    "failed_call_ids": [a["call_id"] for a in recovered],
                    "successful_call_id": success["call_id"],
                    "event_indices": _indices(recovered + [success]),
                })
                failures = [a for a in failures if a not in recovered]
        unresolved.extend(failures)

    final = next((e for e in trace.events if e["type"] == "assistant_final"), None)
    by_id = {a["call_id"]: a for a in attempts}
    references = [by_id[ref] for ref in final.get("evidence_call_ids", [])] if final else []
    final_evidence = {
        "event_index": final["event_index"] if final else None,
        "text": final["text"] if final else None,
        "claimed_outcome": final.get("claimed_outcome", "unknown") if final else "unknown",
        "referenced_results": references,
        "event_indices": sorted(set(_indices(references) + ([final["event_index"]] if final else []))),
    }
    return {
        "attempts": attempts, "tool_errors": errors,
        "repeated_exact_failed_commands": repeated_commands,
        "repeated_failed_operations": repeated_operations,
        "recoveries": recoveries,
        "unresolved_failed_call_ids": [a["call_id"] for a in unresolved],
        "pending_calls": [a for a in attempts if a["status"] == "pending"],
        "final_outcome_evidence": final_evidence,
    }
