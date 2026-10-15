import copy
import json
from pathlib import Path

import pytest

from src.adapter import adapt_raw_trace, parse_input
from src.analysis import analyze_trace, error_families
from src.classification import classify
from src.cli import main
from src.clustering import group_analyses
from src.schema import ValidationError, parse_trace, read_jsonl


ROOT = Path(__file__).resolve().parents[1]
CANDIDATES = ROOT / "data/candidate_traces.jsonl"


def raw_trace(commands=("pytest",), outputs=((0, "5 passed", ""),), final="All tests passed."):
    events = []
    for i, (command, (code, stdout, stderr)) in enumerate(zip(commands, outputs)):
        events.extend([
            {"type": "tool_call", "call_id": f"c{i}", "tool": "shell", "command": command},
            {"type": "tool_result", "call_id": f"c{i}", "exit_code": code, "stdout": stdout, "stderr": stderr},
        ])
    events.append({"type": "assistant_final", "content": final})
    return {"trace_id": "raw", "user_request": "Run tests", "events": events}


def observe(record):
    return analyze_trace(adapt_raw_trace(record))


def test_lossless_adapter_and_inert_unknown_fields():
    record = raw_trace(outputs=((0, "original stdout", "original stderr\n"),))
    record["unknown"] = {"nested": [None, "é", 17]}
    record["events"][0].update(extra=[1, 2], operation="not_part_of_raw_contract")
    record["events"][-1].update(claimed_outcome="incomplete", evidence_call_ids=["fake"])
    original = copy.deepcopy(record)
    trace = adapt_raw_trace(record)
    assert trace.source_record == original == record
    assert [e["event_index"] for e in trace.events] == [0, 1, 2]
    assert trace.events[0]["arguments"] == {"command": "pytest"}
    assert trace.events[1]["output"] == "original stdout\noriginal stderr\n"
    assert trace.events[-1] == {"event_index": 2, "type": "assistant_final", "text": "All tests passed."}
    record["unknown"]["nested"].append("mutation")
    assert trace.source_record == original
    clean = raw_trace(outputs=((0, "original stdout", "original stderr\n"),))
    assert analyze_trace(trace) == observe(clean)


def test_original_indices_and_interleaving():
    record = raw_trace(("pytest", "pytest"), ((1, "1 failed", ""), (0, "5 passed", "")))
    events = record["events"]
    record["events"] = [events[0], events[2], events[3], events[1], events[4]]
    for i, event in enumerate(record["events"]):
        event["event_index"] = 10 + i * 3
    trace = adapt_raw_trace(record)
    assert [e["event_index"] for e in trace.events] == [10, 13, 16, 19, 22]
    observed = analyze_trace(trace)
    assert observed["attempts"][0]["event_indices"] == [10, 19]
    assert observed["recoveries"] == []
    assert observed["success_claim_assessment"]["status"] == "unverified"


@pytest.mark.parametrize("mutate", [
    lambda r: r["events"][1].update(exit_code=True),
    lambda r: r["events"][1].update(stdout=[]),
    lambda r: r["events"][0].pop("command"),
    lambda r: r["events"][0].update(type=[]),
    lambda r: r["events"][1].update(call_id="missing"),
])
def test_invalid_raw_records(mutate):
    record = raw_trace()
    mutate(record)
    with pytest.raises(ValidationError):
        adapt_raw_trace(record)


def test_raw_unknown_exit_and_explicit_format():
    record = raw_trace(outputs=((None, "5 passed", ""),))
    assert parse_input(record).events[1]["status"] == "unknown"
    assert observe(record)["success_claim_assessment"]["status"] == "unverified"
    with pytest.raises(ValidationError):
        parse_input(record, "normalized")


@pytest.mark.parametrize("message,families", [
    ("sandbox: network access to registry.npmjs.org denied by policy", ["network_permission_denied"]),
    ("curl: Failed to connect: sandbox network policy denied outbound connection", ["network_permission_denied"]),
    ("sandbox: outbound TCP connection blocked (network access denied)", ["network_permission_denied"]),
    ("connect EPERM registry.npmjs.org", ["network_permission_denied"]),
    ("socket EACCES", ["network_permission_denied"]),
    ("Registry request denied by policy", ["unknown"]),
    ("Permission request denied by user.", ["unknown"]),
    ("Temporary failure in name resolution", ["unknown"]),
    ("CERT_HAS_EXPIRED: certificate has expired", ["unknown"]),
    ("error: a value is required for --glob <GLOB> but none was supplied", ["invalid_tool_arguments"]),
    ("pytest: error: argument --maxfail: invalid int value: all", ["invalid_tool_arguments"]),
    ("error: unrecognized arguments: --bogus", ["invalid_tool_arguments"]),
    ("0 failed, 10 passed", ["unknown"]),
    ("FAILED tests/test_api.py::test_response - AssertionError", ["test_failure"]),
    ("No test files found, exiting with code 0", ["unknown"]),
])
def test_error_variants(message, families):
    assert error_families(message) == families


def test_pnpm_offline_is_only_alternative_recovery_candidate():
    record = raw_trace(("pnpm install", "pnpm install --offline"),
                       ((1, "", "network denied by policy"), (0, "Done", "")), "Dependencies installed.")
    observed = observe(record)
    assert observed["recoveries"] == []
    assert observed["unresolved_failed_call_ids"] == ["c0"]
    assert observed["goal_recovery"]["status"] == "unverified"
    assert observed["goal_recovery"]["candidates"][0]["event_indices"] == [0, 1, 2, 3]
    patterns = classify(observed)["failure_patterns"]
    assert patterns[0]["response_behavior"] == "alternative_recovery_candidate"


@pytest.mark.parametrize("command", ["pnpm install other-package --offline", "pnpm install --offline && echo done", "pwd"])
def test_unrelated_success_is_not_alternative_recovery(command):
    record = raw_trace(("pnpm install", command), ((1, "", "error"), (0, "Done", "")))
    assert observe(record)["goal_recovery"]["candidates"] == []


def test_tool_switch_keeps_execution_identity_and_goal_candidate_separate():
    trace = adapt_raw_trace(raw_trace(("read a", "read a"), ((1, "", "File not found"), (0, "contents", ""))))
    record = {"trace_id": trace.trace_id, "user_request": trace.user_request, "events": list(trace.events)}
    record["events"][0].update(operation="read_a")
    record["events"][2].update(operation="read_a", tool="read_file")
    observed = analyze_trace(parse_trace(record))
    assert observed["recoveries"] == []
    assert observed["goal_recovery"]["candidates"][0]["basis"] == "producer_operation_across_tools"
    for event in record["events"]:
        event.pop("operation", None)
    assert analyze_trace(parse_trace(record))["goal_recovery"]["candidates"] == []


@pytest.mark.parametrize("code,output,status", [
    (0, "", "unverified"),
    (0, "No test files found, exiting with code 0", "contradicted"),
    (0, "no tests ran in 0.00s", "contradicted"),
    (0, "0 passed", "unverified"),
    (0, "0 failed, 5 passed", "verified"),
    (0, "Tests  87 passed (87)", "verified"),
    (0, "Test Suites: 2 passed, 2 total", "verified"),
    (0, "1 failed, 5 passed", "contradicted"),
    (1, "5 passed", "contradicted"),
    (0, "Documentation says: 5 passed", "unverified"),
])
def test_test_success_requires_report_not_exit_zero(code, output, status):
    observed = observe(raw_trace(outputs=((code, output, ""),)))
    claim = observed["success_claim_assessment"]
    assert claim["status"] == status
    assert claim["goal_status"] == "unverified"
    assert classify(observed)["outcome"] == status + "_success_claim"
    if code == 0:
        assert observed["tool_errors"] == []


def test_no_tests_claim_uses_test_evidence_not_later_git_status():
    record = raw_trace(("npx vitest run --passWithNoTests", "git status --short"),
                       ((0, "No test files found, exiting with code 0", ""), (0, "", "")))
    observed = observe(record)
    assert observed["success_claim_assessment"]["event_indices"] == [0, 1, 4]
    assert classify(observed)["outcome"] == "contradicted_success_claim"
    assert classify(observed)["failure_patterns"] == []  # no fabricated tool error


def test_generic_success_and_multiple_test_scopes_remain_unverified():
    record = raw_trace(final="The installation completed.")
    assert observe(record)["success_claim_assessment"]["status"] == "unverified"
    record = raw_trace(("pytest tests/a", "pytest tests/b"), ((1, "1 failed", ""), (0, "5 passed", "")))
    assert observe(record)["success_claim_assessment"]["status"] == "unverified"


def test_normalized_zero_test_report_contradicts_explicit_test_claim():
    trace = adapt_raw_trace(raw_trace(outputs=((0, "No test files found, exiting with code 0", ""),)))
    record = {"trace_id": trace.trace_id, "user_request": trace.user_request, "events": list(trace.events)}
    record["events"][-1].update(claimed_outcome="success", evidence_call_ids=["c0"])
    observed = analyze_trace(parse_trace(record))
    assert observed["success_claim_assessment"]["evidence_basis"] == "producer_evidence_call_ids"
    assert classify(observed)["outcome"] == "contradicted_success_claim"


def test_raw_validation_errors_do_not_abort_batch(tmp_path):
    bad = raw_trace()
    bad["events"][0]["type"] = []
    path = tmp_path / "mixed.jsonl"
    path.write_text(json.dumps(bad) + "\n" + json.dumps(raw_trace()) + "\n")
    rows = list(read_jsonl(path, "raw"))
    assert rows[0].error
    assert rows[1].trace.trace_id == "raw"


def test_failure_patterns_do_not_cross_unrelated_events():
    record = raw_trace(("npm ci", "cat a", "cat a", "cat a"), (
        (1, "", "network denied by policy"), (1, "", "File not found"),
        (1, "", "File not found"), (0, "contents", "")), "Done")
    classification = classify(observe(record))
    pairs = {(p["error_family"], p["response_behavior"]) for p in classification["failure_patterns"]}
    assert pairs == {("network_permission_denied", "unknown"),
                     ("missing_file", "repeated_failed_operation"), ("missing_file", "successful_recovery")}
    clusters = group_analyses([{"trace_id": "raw", "source_line": 1, "classification": classification}])
    for group in clusters["failure_patterns"]:
        assert group["trace_count"] == 1
        member = group["members"][0]
        if group["error_family"] == "missing_file":
            assert member["failed_call_ids"] == ["c1", "c2"]
            assert not {0, 1} & set(member["event_indices"])


def test_all_candidates_and_deterministic_lossless_outputs(tmp_path):
    source = [json.loads(line) for line in CANDIDATES.read_text().splitlines()]
    assert len(source) == 30
    assert len(list(read_jsonl(CANDIDATES))) == 30
    args = ["analyze", "--input", str(CANDIDATES), "--output", str(tmp_path)]
    assert main(args) == 0
    first = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    assert main(args) == 0
    assert first == {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    saved = [json.loads(line) for line in (tmp_path / "trace_analysis.jsonl").read_text().splitlines()]
    assert [a["source_record"] for a in saved] == source
    assert all(a["observations"]["final_outcome_evidence"]["claimed_outcome"] == "unknown" for a in saved)
    assert all(a["observations"]["final_outcome_evidence"]["referenced_results"] == [] for a in saved)
    by_id = {a["trace_id"]: a for a in saved}
    assert by_id["trace_001"]["classification"]["error_families"] == ["network_permission_denied"]
    assert by_id["trace_022"]["classification"]["outcome"] == "contradicted_success_claim"
    assert by_id["trace_030"]["classification"]["error_families"] == []
    for analysis in saved:
        for pattern in analysis["classification"]["failure_patterns"]:
            events = analysis["source_record"]["events"]
            assert all(0 <= i < len(events) for i in pattern["event_indices"])
            assert any(e.get("call_id") == pattern["failed_call_id"] and e["type"] == "tool_result"
                       and e["exit_code"] != 0 for e in events)
