"""Narrow alternative-operation candidates, never proof of goal recovery."""

import shlex
from typing import Any


def _task_key(call: dict[str, Any]) -> dict[str, Any] | None:
    # A producer's operation ID can connect tool switches, but is weaker than
    # the analyzer's tool-scoped execution identity.
    if "operation" in call:
        return {"basis": "producer_operation_across_tools", "value": call["operation"]}
    arguments = call["arguments"]
    command = arguments.get("command")
    if not isinstance(command, str):
        return None
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    # Only remove the single documented alternative flag. Keep package names,
    # lockfile flags, cwd and other arguments; do not normalize arbitrary shells.
    if words[:2] == ["pnpm", "install"] and not any(c in command for c in ";|&><\n"):
        return {"basis": "pnpm_offline_alternative", "tool": call["tool"],
                "arguments": {**arguments, "command": [w for w in words if w != "--offline"]}}
    return None


def goal_recovery_candidates(attempts, calls):
    candidates = []
    for failure in (a for a in attempts if a["status"] == "error"):
        key = _task_key(calls[failure["call_id"]])
        if key is None:
            continue
        for success in attempts:
            if (success["status"] == "success"
                    and success["call_event_index"] > failure["result_event_index"]
                    and success["operation"] != failure["operation"]
                    and _task_key(calls[success["call_id"]]) == key):
                candidates.append({
                    "status": "candidate", "basis": key["basis"],
                    "failed_call_ids": [failure["call_id"]],
                    "successful_call_id": success["call_id"],
                    "event_indices": sorted(failure["event_indices"] + success["event_indices"]),
                })
                break
    return candidates
