"""Strict normalized input schema; no environment or outcome inference."""

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Iterator


class ValidationError(ValueError):
    """A record is not a normalized trace."""


@dataclass(frozen=True)
class Trace:
    trace_id: str
    user_request: str
    events: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class ReadResult:
    line_number: int
    trace: Trace | None = None
    error: dict[str, Any] | None = None


def _fail(path: str, message: str) -> None:
    raise ValidationError(f"{path}: {message}")


def _fields(value: Any, required: set[str], optional: set[str], path: str) -> None:
    if not isinstance(value, dict):
        _fail(path, "expected an object")
    missing = required - value.keys()
    extra = value.keys() - required - optional
    if missing:
        _fail(path, f"missing required fields: {', '.join(sorted(missing))}")
    if extra:
        _fail(path, f"unexpected fields: {', '.join(sorted(extra))}")


def _string(value: Any, path: str, nonempty: bool = True) -> None:
    if not isinstance(value, str) or (nonempty and not value.strip()):
        _fail(path, "expected a nonempty string" if nonempty else "expected a string")


def _choice(value: Any, choices: tuple[str, ...], path: str) -> None:
    if value not in choices:
        _fail(path, f"expected one of {', '.join(choices)}")


def _json_value(value: Any, path: str) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                _fail(path, "JSON object keys must be strings")
            _json_value(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _json_value(item, f"{path}[{index}]")
    elif value is not None and type(value) not in (str, bool, int, float):
        _fail(path, "expected a JSON value")
    try:
        json.dumps(value, allow_nan=False)
    except (ValueError, TypeError):
        _fail(path, "expected finite JSON values")


def parse_trace(record: Any) -> Trace:
    _fields(record, {"trace_id", "user_request", "events"}, set(), "record")
    for key in ("trace_id", "user_request"):
        _string(record[key], key)
    if not isinstance(record["events"], list):
        _fail("events", "expected an array")
    calls: set[str] = set()
    results: set[str] = set()
    previous_index = -1
    final_seen = False
    for position, event in enumerate(record["events"]):
        path = f"events[{position}]"
        if not isinstance(event, dict):
            _fail(path, "expected an object")
        kind = event.get("type")
        _choice(kind, ("tool_call", "tool_result", "assistant_final"), f"{path}.type")
        required = {"event_index", "type"}
        optional: set[str] = set()
        if kind == "tool_call":
            required |= {"call_id", "tool", "arguments"}
            optional = {"operation"}
        elif kind == "tool_result":
            required |= {"call_id", "status", "output"}
            optional = {"exit_code"}
        else:
            required |= {"text"}
            optional = {"claimed_outcome", "evidence_call_ids"}
        _fields(event, required, optional, path)
        index = event["event_index"]
        if type(index) is not int or index < 0 or index <= previous_index:
            _fail(f"{path}.event_index", "expected a nonnegative, strictly increasing integer")
        previous_index = index
        if final_seen:
            _fail(path, "assistant_final must be the last event")
        if kind in ("tool_call", "tool_result"):
            call_id = event["call_id"]
            _string(call_id, f"{path}.call_id")
            if kind == "tool_call":
                if call_id in calls:
                    _fail(f"{path}.call_id", f"duplicate call_id {call_id!r}")
                calls.add(call_id)
                _string(event["tool"], f"{path}.tool")
                if not isinstance(event["arguments"], dict):
                    _fail(f"{path}.arguments", "expected an object")
                _json_value(event["arguments"], f"{path}.arguments")
                if "operation" in event:
                    _string(event["operation"], f"{path}.operation")
            else:
                if call_id not in calls:
                    _fail(f"{path}.call_id", f"no preceding call for {call_id!r}")
                if call_id in results:
                    _fail(f"{path}.call_id", f"duplicate result for {call_id!r}")
                results.add(call_id)
                _choice(event["status"], ("success", "error", "unknown"), f"{path}.status")
                _string(event["output"], f"{path}.output", nonempty=False)
                if "exit_code" in event:
                    code = event["exit_code"]
                    if type(code) is not int:
                        _fail(f"{path}.exit_code", "expected an integer")
                    if code != 0 and event["status"] != "error":
                        _fail(f"{path}.status", "nonzero exit_code requires error status")
        else:
            final_seen = True
            _string(event["text"], f"{path}.text", nonempty=False)
            _choice(event.get("claimed_outcome", "unknown"),
                    ("success", "incomplete", "unknown"), f"{path}.claimed_outcome")
            refs = event.get("evidence_call_ids", [])
            if not isinstance(refs, list):
                _fail(f"{path}.evidence_call_ids", "expected an array")
            seen: set[str] = set()
            for ref in refs:
                _string(ref, f"{path}.evidence_call_ids")
                if ref not in results:
                    _fail(f"{path}.evidence_call_ids", f"no completed call for {ref!r}")
                if ref in seen:
                    _fail(f"{path}.evidence_call_ids", f"duplicate reference {ref!r}")
                seen.add(ref)
    return Trace(record["trace_id"], record["user_request"], tuple(record["events"]))


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValidationError(f"JSON: duplicate object key {key!r}")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise ValidationError(f"JSON: non-finite number {value}")


def read_jsonl(path: str | Path) -> Iterator[ReadResult]:
    """Yield valid traces or line errors. Blank lines are malformed records too."""
    seen: set[str] = set()
    with Path(path).open("rb") as stream:
        for line_number, raw in enumerate(stream, 1):
            trace_id = None
            try:
                record = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                                    parse_constant=_invalid_constant)
                if isinstance(record, dict) and isinstance(record.get("trace_id"), str):
                    trace_id = record["trace_id"]
                trace = parse_trace(record)
                if trace.trace_id in seen:
                    _fail("trace_id", f"duplicate trace_id {trace.trace_id!r}")
                seen.add(trace.trace_id)
                yield ReadResult(line_number, trace=trace)
            except (ValueError, UnicodeDecodeError, RecursionError) as exc:
                yield ReadResult(line_number, error={
                    "source": str(path), "line_number": line_number,
                    "trace_id": trace_id, "message": str(exc),
                })
