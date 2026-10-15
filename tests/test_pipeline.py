import copy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from src.analysis import analyze_trace, error_families
from src.classification import classify
from src.cli import main
from src.clustering import group_analyses
from src.schema import ValidationError, parse_trace, read_jsonl


ROOT = Path(__file__).resolve().parents[1]
RECORDS = {r["trace_id"]: r for r in map(json.loads, (ROOT / "data/traces.jsonl").read_text().splitlines())}
EXPECTED = json.loads((ROOT / "tests/expected_labels.json").read_text())


def observe(name):
    return analyze_trace(parse_trace(copy.deepcopy(RECORDS[name])))


@pytest.mark.parametrize("trace_id", EXPECTED)
def test_synthetic_labels(trace_id):
    actual = classify(observe(trace_id))
    for key, value in EXPECTED[trace_id].items():
        assert actual[key] == value


def test_fixture_expectations_are_separate():
    assert RECORDS.keys() == EXPECTED.keys()
    for record in RECORDS.values():
        assert set(record) == {"trace_id", "user_request", "events"}


def test_exact_commands_and_operations_are_distinct():
    observed = observe("denied-repeated-calls")
    assert observed["repeated_exact_failed_commands"] == [{
        "command": "curl https://example.invalid", "call_ids": ["a", "b"],
        "count": 2, "event_indices": [0, 1, 3, 4],
    }]
    assert observed["repeated_failed_operations"][0]["count"] == 3
    assert observed["repeated_failed_operations"][0]["event_indices"] == [0, 1, 3, 4, 7, 8]


def test_single_retry_is_not_repeated_failure():
    observed = observe("transient-retry")
    assert observed["repeated_exact_failed_commands"] == []
    assert observed["repeated_failed_operations"] == []
    assert observed["unresolved_failed_call_ids"] == []


def test_no_invented_capability_facts():
    result = classify(observe("honest-inability"))
    assert result["capability_gap_candidates"][0]["status"] == "candidate"
    assert result["capability_gap_candidates"][0]["event_indices"] == [0, 2]
    assert classify(observe("invalid-arguments"))["capability_gap_candidates"] == []
    assert "environment" not in result


def test_changed_arguments_without_operation_do_not_imply_recovery():
    record = copy.deepcopy(RECORDS["successful-recovery"])
    for event in record["events"]:
        event.pop("operation", None)
    assert analyze_trace(parse_trace(record))["recoveries"] == []


def test_operation_identity_includes_tool():
    record = copy.deepcopy(RECORDS["successful-recovery"])
    record["events"][2]["tool"] = "different_tool"
    assert analyze_trace(parse_trace(record))["recoveries"] == []


def test_interleaved_calls_pair_by_id_and_do_not_imply_recovery():
    record = copy.deepcopy(RECORDS["transient-retry"])
    events = record["events"]
    record["events"] = [events[0], events[2], events[3], events[1], events[4]]
    for index, event in enumerate(record["events"]):
        event["event_index"] = index * 3
    observed = analyze_trace(parse_trace(record))
    assert observed["tool_errors"][0]["event_indices"] == [0, 9]
    assert observed["attempts"][1]["event_indices"] == [3, 6]
    assert observed["recoveries"] == []


def test_repetition_and_recovery_can_coexist():
    record = copy.deepcopy(RECORDS["denied-repeated-calls"])
    record["events"][5].update(status="success", output="Fetched", exit_code=0)
    record["events"][6].update(claimed_outcome="success")
    result = classify(analyze_trace(parse_trace(record)))
    assert result["behavior_patterns"] == ["repeated_failed_operation", "successful_recovery"]


@pytest.mark.parametrize("output, expected", [
    ("Permission denied reading config.json", ["unknown"]),
    ("Connection refused", ["unknown"]),
    ("0 failed, 10 passed", ["unknown"]),
    ("10 failed", ["test_failure"]),
    ("Invalid tool arguments", ["invalid_tool_arguments"]),
    ("Network permission denied; File not found", ["network_permission_denied", "missing_file"]),
])
def test_error_signature_boundaries(output, expected):
    assert error_families(output) == expected


def test_success_output_containing_error_words_is_not_tool_error():
    record = copy.deepcopy(RECORDS["plain-success"])
    record["events"][1]["output"] = "Documentation says: Network permission denied; tests failed"
    assert analyze_trace(parse_trace(record))["tool_errors"] == []


def test_unlinked_error_does_not_contradict_success():
    record = copy.deepcopy(RECORDS["unsupported-success"])
    record["events"][-1].pop("evidence_call_ids")
    result = classify(analyze_trace(parse_trace(record)))
    assert result["outcome"] == "unknown"
    assert "unsupported_success_claim" not in result["behavior_patterns"]


def test_free_text_claim_is_not_inferred():
    record = copy.deepcopy(RECORDS["unsupported-success"])
    record["events"][-1].pop("claimed_outcome")
    assert classify(analyze_trace(parse_trace(record)))["outcome"] == "unknown"


def test_unknown_result_is_not_failure_or_success():
    record = copy.deepcopy(RECORDS["plain-success"])
    record["events"][1]["status"] = "unknown"
    observed = analyze_trace(parse_trace(record))
    assert observed["tool_errors"] == []
    assert classify(observed)["outcome"] == "unknown"


def test_pending_calls_are_preserved():
    pending = observe("pending-call")["pending_calls"]
    assert pending[0]["event_indices"] == [6]
    assert pending[0]["result_event_index"] is None
    assert observe("empty-session")["final_outcome_evidence"]["text"] is None


@pytest.mark.parametrize("mutate, message", [
    (lambda r: r.pop("trace_id"), "missing required fields: trace_id"),
    (lambda r: r.update(events={}), "events: expected an array"),
    (lambda r: r.update(expected_labels=[]), "unexpected fields"),
    (lambda r: r["events"][0].update(event_index=True), "strictly increasing integer"),
    (lambda r: r["events"][1].update(event_index=0), "strictly increasing integer"),
    (lambda r: r["events"][1].update(call_id="absent"), "no preceding call"),
    (lambda r: r["events"][1].update(status="failure"), "expected one of"),
    (lambda r: r["events"][1].update(exit_code=1), "requires error status"),
    (lambda r: r["events"][1].update(exit_code=True), "expected an integer"),
    (lambda r: r["events"][0].update(arguments=[]), "expected an object"),
    (lambda r: r["events"][0].update(arguments={"n": float("nan")}), "finite JSON"),
    (lambda r: r["events"][2].update(evidence_call_ids=["absent"]), "no completed call"),
    (lambda r: r["events"][2].update(evidence_call_ids=["a", "a"]), "duplicate reference"),
    (lambda r: r["events"][2].update(claimed_outcome="failed"), "expected one of"),
])
def test_schema_errors(mutate, message):
    record = copy.deepcopy(RECORDS["plain-success"])
    mutate(record)
    with pytest.raises(ValidationError, match=message):
        parse_trace(record)


@pytest.mark.parametrize("position, message", [(0, "duplicate call_id"), (1, "duplicate result"), (2, "must be the last")])
def test_duplicate_events_rejected(position, message):
    record = copy.deepcopy(RECORDS["plain-success"])
    record["events"].insert(position + 1, copy.deepcopy(record["events"][position]))
    for index, event in enumerate(record["events"]):
        event["event_index"] = index
    with pytest.raises(ValidationError, match=message):
        parse_trace(record)


def test_bad_lines_do_not_stop_valid_records(tmp_path):
    path = tmp_path / "mixed.jsonl"
    valid = json.dumps(RECORDS["plain-success"])
    path.write_bytes(b'\xff\n\n{"x":1,"x":2}\n{"n":NaN}\n' + valid.encode() + b"\n" + valid.encode() + b"\n")
    rows = list(read_jsonl(path))
    assert [r.line_number for r in rows if r.error] == [1, 2, 3, 4, 6]
    assert rows[4].trace.trace_id == "plain-success"
    assert "duplicate trace_id" in rows[5].error["message"]


def test_invalid_fixture_errors():
    rows = list(read_jsonl(ROOT / "data/invalid_traces.jsonl"))
    assert len(rows) == 6
    assert all(row.error and row.error["line_number"] == row.line_number for row in rows)


def test_clusters_have_exact_source_evidence():
    observed = observe("unsupported-success")
    clusters = group_analyses([{"trace_id": "unsupported-success", "source_line": 5,
                               "classification": classify(observed)}])
    assert clusters["groups"]["error_families"][0]["members"] == [
        {"trace_id": "unsupported-success", "source_line": 5, "event_indices": [4, 9]}]
    assert clusters["groups"]["behavior_patterns"][0]["members"][0]["event_indices"] == [4, 9, 12]


def test_cli_entrypoint_and_deterministic_outputs(tmp_path):
    command = [sys.executable, "-m", "src.cli", "analyze", "--input", "data/traces.jsonl", "--output", str(tmp_path)]
    first = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
    assert first.returncode == 0, first.stderr
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    assert subprocess.run(command, cwd=ROOT, capture_output=True).returncode == 0
    assert before == {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    records = [json.loads(line) for line in (tmp_path / "trace_analysis.jsonl").read_text().splitlines()]
    assert len(records) == len(RECORDS)
    clusters = json.loads((tmp_path / "failure_clusters.json").read_text())
    for dimension, groups in clusters["groups"].items():
        for group in groups:
            for member in group["members"]:
                assert group["label"] in EXPECTED[member["trace_id"]][dimension]
                valid_indices = {e["event_index"] for e in RECORDS[member["trace_id"]]["events"]}
                assert set(member["event_indices"]) <= valid_indices


def test_cli_partial_output_on_invalid_records(tmp_path):
    path = tmp_path / "mixed.jsonl"
    path.write_text('{bad json}\n' + json.dumps(RECORDS["plain-success"]) + '\n')
    output = tmp_path / "out"
    assert main(["analyze", "--input", str(path), "--output", str(output)]) == 1
    assert len((output / "trace_analysis.jsonl").read_text().splitlines()) == 1
    errors = [json.loads(line) for line in (output / "validation_errors.jsonl").read_text().splitlines()]
    assert errors[0]["line_number"] == 1
    assert json.loads((output / "failure_clusters.json").read_text())["invalid_record_count"] == 1


def test_cli_missing_file_and_input_overwrite(tmp_path):
    path = tmp_path / "trace_analysis.jsonl"
    assert main(["analyze", "--input", str(path), "--output", str(tmp_path / "out")]) == 2
    path.write_text("keep me")
    assert main(["analyze", "--input", str(path), "--output", str(tmp_path)]) == 2
    assert path.read_text() == "keep me"


def test_cli_preserves_escaped_unicode(tmp_path):
    record = copy.deepcopy(RECORDS["empty-session"])
    record["user_request"] = "Inspect café and escaped surrogate \ud800"
    path = tmp_path / "unicode.jsonl"
    path.write_text(json.dumps(record) + "\n")
    output = tmp_path / "out"
    assert main(["analyze", "--input", str(path), "--output", str(output)]) == 0
    saved = json.loads((output / "trace_analysis.jsonl").read_text())
    assert saved["user_request"] == record["user_request"]


def test_oversized_integer_is_a_record_error(tmp_path):
    path = tmp_path / "large.jsonl"
    path.write_text('{"n":' + '9' * 5000 + '}\n' + json.dumps(RECORDS["empty-session"]) + '\n')
    rows = list(read_jsonl(path))
    assert rows[0].error
    assert rows[1].trace.trace_id == "empty-session"


def test_recovery_does_not_erase_subsequent_failure():
    record = copy.deepcopy(RECORDS["denied-repeated-calls"])
    record["events"][3].update(status="success", output="Fetched", exit_code=0)
    observed = analyze_trace(parse_trace(record))
    assert observed["recoveries"][0]["failed_call_ids"] == ["a"]
    assert observed["unresolved_failed_call_ids"] == ["c"]
    assert classify(observed)["outcome"] == "reported_incomplete"
