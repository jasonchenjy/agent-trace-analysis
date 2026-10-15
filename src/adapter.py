"""Lossless adapter for command/stdout/stderr/content session records."""

from copy import deepcopy
from typing import Any

from .schema import Trace, ValidationError, _fields, _json_value, _string, parse_trace


def adapt_raw_trace(record: Any) -> Trace:
    if not isinstance(record, dict):
        raise ValidationError("record: expected an object")
    _fields(record, {"trace_id", "user_request", "events"}, set(record), "record")
    _json_value(record, "record")
    if not isinstance(record["events"], list):
        raise ValidationError("events: expected an array")
    events = []
    for position, raw in enumerate(record["events"]):
        path = f"events[{position}]"
        if not isinstance(raw, dict):
            raise ValidationError(f"{path}: expected an object")
        kind = raw.get("type")
        if not isinstance(kind, str):
            raise ValidationError(f"{path}.type: expected a string")
        required = {
            "tool_call": {"type", "call_id", "tool", "command"},
            "tool_result": {"type", "call_id", "exit_code", "stdout", "stderr"},
            "assistant_final": {"type", "content"},
        }.get(kind)
        if required is None:
            raise ValidationError(f"{path}.type: unsupported raw event type")
        _fields(raw, required, set(raw), path)
        event = {"type": kind, "event_index": raw.get("event_index", position)}
        if kind == "tool_call":
            _string(raw["command"], f"{path}.command", nonempty=False)
            event.update(call_id=raw["call_id"], tool=raw["tool"],
                         arguments={"command": raw["command"]})
        elif kind == "tool_result":
            for stream in ("stdout", "stderr"):
                _string(raw[stream], f"{path}.{stream}", nonempty=False)
            code = raw["exit_code"]
            if code is not None and type(code) is not int:
                raise ValidationError(f"{path}.exit_code: expected an integer or null")
            # Execution status only, not a task outcome. Original streams remain
            # separate and byte-for-byte in source_record's decoded strings.
            output = raw["stdout"]
            if output and raw["stderr"] and not output.endswith("\n"):
                output += "\n"
            output += raw["stderr"]
            event.update(call_id=raw["call_id"], output=output,
                         status="unknown" if code is None else "success" if code == 0 else "error")
            if code is not None:
                event["exit_code"] = code
        else:
            event["text"] = raw["content"]
        events.append(event)
    trace = parse_trace({"trace_id": record["trace_id"],
                         "user_request": record["user_request"], "events": events})
    return Trace(trace.trace_id, trace.user_request, trace.events, deepcopy(record))


def parse_input(record: Any, input_format: str = "auto") -> Trace:
    if input_format not in {"auto", "raw", "normalized"}:
        raise ValidationError("input_format: expected auto, raw, or normalized")
    raw_markers = {"tool_call": "command", "tool_result": "stdout", "assistant_final": "content"}
    events = record.get("events", []) if isinstance(record, dict) else []
    is_raw = isinstance(events, list) and any(
        isinstance(e, dict) and isinstance(e.get("type"), str)
        and raw_markers.get(e["type"]) in e for e in events
    )
    if input_format == "raw" or (input_format == "auto" and is_raw):
        return adapt_raw_trace(record)
    return parse_trace(record)
