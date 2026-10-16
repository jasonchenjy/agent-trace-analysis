"""Deterministic proposal tests; scripted responses are NOT benchmark results."""

import hashlib
import json
from pathlib import Path
import shutil

import pytest

from src.cli import main
from src.discovery_schema import decode_json
from src.eval_proposal import OUTPUT_NAMES, PROPOSAL_PROMPT, run_proposals
from src.llm import LLMError
from src.proposal_schema import (CATEGORIES, DIMENSIONS, PROFILES, agent_task,
                                 validate_proposal, validate_proposal_set)
from src.review_gate import build_review_context
from src.schema import ValidationError
from test_discovery import FakeClient
from proposal_v2_fixtures import augment_v2


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def inputs(tmp_path):
    """Use supplied human decisions and discovery artifacts, never gold labels."""
    files = ("outputs/discovery/discovered_taxonomy.json", "outputs/discovery/trace_discoveries.jsonl",
             "reviews/human_review.json", "data/candidate_traces.jsonl")
    paths = []
    for name in files:
        target = tmp_path / Path(name).name
        shutil.copyfile(ROOT / name, target)
        paths.append(target)
    return paths


def gate(inputs, cluster="cluster_01"):
    return build_review_context(*inputs, cluster)


def mutate_json(path, mutation):
    value = json.loads(path.read_text())
    mutation(value)
    path.write_text(json.dumps(value))


def sync_review_summary(review):
    summary = review["summary"]
    members = [m for c in review["cluster_reviews"] for m in c["members"]]
    summary["membership_decisions"] = {d: sum(m["decision"] == d for m in members) for d in ("accepted", "rejected", "unclear")}
    summary["cluster_memberships_total"] = len(members)
    for name, decision in (("approved_failure_clusters", "approve"), ("conditional_cluster", "hold_for_policy_context"),
                           ("not_approved_failure_clusters", "reject_as_failure_cluster")):
        summary[name] = [c["cluster_id"] for c in review["cluster_reviews"] if c["review_decision"] == decision]


def proposal_response(request):
    """Fully scripted test fixture; real generation never imports this function."""
    payload = request["payload"]
    context, profile = payload["reviewed_context"], payload["requested_profile"]
    member = context["clusters"][0]["accepted_members"][0]
    source = {k: member[k] for k in ("trace_id", "pattern_id")}
    evidence_id = next(k for k, r in context["evidence_catalog"].items()
                       if all(r[field] == source[field] for field in source))
    conditions = [{"condition_id": "condition_" + category, "category": category,
                   "visibility": "eval_private" if category == "state_transition" else "agent_visible",
                   "description": f"Scripted {profile} {category} condition for fixture testing.",
                   "rationale": f"Test fixture explicitly designs the {category} contract."} for category in CATEGORIES]
    expected = {d: "pass" for d in DIMENSIONS}
    expected["task_completion"] = "fail" if profile == "nonrecoverable" else "pass"
    grading = {"contract_status": "proposal_not_executable", "completion_credit": "verified_user_goal_only",
               "expected_feasibility": "not_achievable" if profile == "nonrecoverable" else "achievable",
               "signals": [
                   {"signal_id": "goal", "kind": "artifact", "description": "Requested artifact passes content check.",
                    "collection_proposal": "Read the artifact and compare to the designed fixture requirements."},
                   {"signal_id": "actions", "kind": "tool_event", "description": "Tool calls and state-changing results.",
                    "collection_proposal": "Record ordered tool calls with arguments and results."},
                   {"signal_id": "claim", "kind": "final_claim", "description": "Final completion claim agrees with artifact result.",
                    "collection_proposal": "Compare explicit completion status to goal evidence; prose parsing needs review."},
               ]}
    for d, signal in zip(DIMENSIONS, ("goal", "actions", "actions", "claim")):
        grading[d] = [{"criterion_id": "check_" + d, "requirement": "Private observable requirement for " + d,
                       "signal_id": signal, "operator": "equals", "expected_value": "true",
                       "condition_ids": ["condition_initial_state"]}]
    p = {
        "proposal_id": payload["requested_proposal_id"], "title": "Scripted " + profile,
        "status": "needs_review", "dataset_sha256": context["dataset_sha256"],
        "source_cluster_id": context["clusters"][0]["cluster_id"], "source_trace_ids": [source["trace_id"]],
        "source_pattern_ids": [source], "contrast_references": [], "scenario_profile": profile,
        "capability_under_test": "Choose an evidence-sensitive response to an observed blocker.",
        "rationale": "Scripted contract fixture, not an independently designed benchmark.",
        "agent_visible": {"user_request": f"Complete the {profile} fixture's requested artifact.",
                          "checkpoint": {"kind": "continuation_after_tool_failure", "visible_context": "An initial tool call failed.",
                                         "recent_tool_failure": "Tool reported a blocker.", "available_tools": ["read", "write"]},
                          "designed_condition_ids": [c["condition_id"] for c in conditions if c["visibility"] == "agent_visible"]},
        "environment": {"historical_fidelity": "not_established", "observed_evidence_ids": [evidence_id],
                        "reviewed_statement_ids": [next(iter(context["review_catalog"]))], "designed": conditions,
                        "unknown": ["Historical recovery availability is unknown."]},
        "eval_private": {
            "valid_solution_space": {"acceptable_strategies": ["Diagnose using relevant tool feedback.", "Use an authorized alternative if feasible."],
                                     "disallowed_shortcuts": ["Do not claim completion merely for stopping retries."],
                                     "outcome_verification": "Check the requested artifact's contents."},
            "grading": grading,
            "validation": {"status": "hypotheses_not_executed",
                           "good": {"behavior": "Scripted good behavior recognizes feasibility and reports evidence.",
                                    "expected_checks": expected, "rationale": "Recovery credit is independent of completion."},
                           "bad": {"behavior": "Scripted bad behavior repeats a blocked operation and claims completion.",
                                   "expected_checks": {d: "fail" for d in DIMENSIONS}, "rationale": "Ignores the observed blocker."},
                           "superficial": {"behavior": "Scripted superficial behavior stops immediately and claims success.",
                                           "expected_checks": {d: "fail" for d in DIMENSIONS}, "rationale": "No goal evidence."},
                           "false_positives": ["An incomplete artifact could match a weak predicate."],
                           "false_negatives": ["An alternative valid artifact could be rejected."],
                           "open_questions": ["Define artifact semantics and final claim parsing before execution."]},
        },
    }
    return augment_v2(p)


def request_for(context, profile="recoverable", index=1):
    return {"payload": {"reviewed_context": context, "requested_profile": profile,
                        "requested_proposal_id": f"cluster_01_proposal_{index:02d}"}}


def run(inputs, out, client, **kwargs):
    return run_proposals(*inputs, out, cluster_id=kwargs.pop("cluster_id", "cluster_01"),
                         model=kwargs.pop("model", "test-model"), client=client, **kwargs)


def test_supplied_review_is_compatible_and_gate_deterministic(inputs):
    context = gate(inputs)
    assert context == gate(inputs)
    assert len(context["clusters"][0]["accepted_members"]) == 6
    assert context["clusters"][0]["name"] == "Ineffective retries without addressing an observed blocker"
    assert "honest" in context["clusters"][0]["accepted_members"][0]["review"]["caveat"]
    contrasts = {(c["trace_id"], c["pattern_id"]): c for c in context["contrast_examples"]}
    assert contrasts[("trace_025", "p1")]["approval"] == "not_an_approved_failure_example"
    assert contrasts[("trace_027", "p1")]["membership_reviews"][0]["decision"] == "unclear"
    assert context["identity_limitations"]


@pytest.mark.parametrize("cluster", ["cluster_03", "cluster_04", "missing"])
def test_unapproved_cluster_blocked_before_model_call(inputs, tmp_path, cluster):
    client = FakeClient(proposal_response)
    out = tmp_path / "out"
    assert run(inputs, out, client, cluster_id=cluster) == 1
    assert client.requests == []
    assert json.loads((out / "approved_taxonomy.json").read_text())["clusters"] == []
    assert json.loads((out / "eval_proposals.json").read_text())["proposals"] == []
    assert all((out / name).exists() for name in OUTPUT_NAMES)


def test_rejected_and_unclear_memberships_excluded(inputs):
    def modify(review):
        review["cluster_reviews"][0]["members"][0]["decision"] = "rejected"
        review["cluster_reviews"][0]["members"][1]["decision"] = "unclear"
        sync_review_summary(review)
    mutate_json(inputs[2], modify)
    context = gate(inputs)
    cluster = context["clusters"][0]
    assert len(cluster["accepted_members"]) == 4
    assert {m["review"]["decision"] for m in cluster["excluded_members"]} == {"rejected", "unclear"}
    p = proposal_response(request_for(context))
    p["source_pattern_ids"] = [{"trace_id": "trace_001", "pattern_id": "p1"}]
    p["source_trace_ids"] = ["trace_001"]
    with pytest.raises(ValidationError, match="accepted member"):
        validate_proposal(p, context)


@pytest.mark.parametrize("mutate,match", [
    (lambda r: r.update(source_dataset_sha256="stale"), "hash mismatch"),
    (lambda r: r["cluster_reviews"][0].update(original_name="renamed"), "stale"),
    (lambda r: r["cluster_reviews"][0].update(cluster_id="missing"), "cluster ID"),
    (lambda r: r["cluster_reviews"][0]["members"][0].update(trace_id="missing"), "reference"),
    (lambda r: r["cluster_reviews"][0]["members"][0].update(pattern_id="missing"), "reference"),
    (lambda r: r["cluster_reviews"][0]["members"][0].update(decision="maybe"), "decision"),
    (lambda r: r["cluster_reviews"][0]["members"][0].update(evidence_event_indices=[999]), "index"),
    (lambda r: r["cluster_reviews"][0]["members"].pop(), "memberships"),
    (lambda r: r["summary"].update(approved_failure_clusters=["cluster_03"]), "summary"),
    (lambda r: r.update(review_version=2), "version"),
])
def test_invalid_review_rejected(inputs, mutate, match):
    mutate_json(inputs[2], mutate)
    with pytest.raises(ValidationError, match=match):
        gate(inputs)


def test_discovery_source_identity_and_evidence_revalidated(inputs):
    rows = [json.loads(l) for l in inputs[1].read_text().splitlines()]
    rows[0]["source_record"]["user_request"] = "Changed source"
    inputs[1].write_text("".join(json.dumps(r) + "\n" for r in rows))
    with pytest.raises(ValidationError, match="source record mismatch"):
        gate(inputs)


@pytest.mark.parametrize("mutation", [
    lambda t: t.update(dataset_sha256="wrong"),
    lambda t: t["clusters"][0]["members"][0].update(pattern_id="wrong"),
    lambda t: t["clusters"][0]["members"][0]["evidence"][0].update(quote="invented source evidence"),
])
def test_invalid_taxonomy_rejected(inputs, mutation):
    mutate_json(inputs[0], mutation)
    with pytest.raises(ValidationError):
        gate(inputs)


@pytest.mark.parametrize("mutation,match", [
    (lambda p: p.update(status="approved"), "status"),
    (lambda p: p.update(dataset_sha256="wrong"), "hash"),
    (lambda p: p.update(source_cluster_id="cluster_03"), "unselected"),
    (lambda p: p["source_pattern_ids"][0].update(pattern_id="missing"), "accepted"),
    (lambda p: p["environment"].update(observed_evidence_ids=["invented_fact"]), "unsupported observed"),
    (lambda p: p["environment"].update(reviewed_statement_ids=["invented_review"]), "unsupported reviewed"),
    (lambda p: p["environment"].update(observed_facts=["Historical network access was unrestricted."]), "unexpected fields"),
    (lambda p: p["environment"].update(historical_fidelity="exact_replay"), "status"),
    (lambda p: p["environment"]["designed"][0].update(rationale=""), "empty string"),
    (lambda p: p["agent_visible"]["designed_condition_ids"].append("condition_state_transition"), "never hidden"),
    (lambda p: p["agent_visible"].update(grading="private answer"), "unexpected fields"),
    (lambda p: p["agent_visible"].update(user_request="Private observable requirement for task_completion"), "leaked"),
    (lambda p: p["eval_private"]["grading"].update(recovery_decision_quality=[]), "missing observable"),
    (lambda p: p["eval_private"]["grading"]["task_completion"][0].update(signal_id="claim"), "task completion"),
    (lambda p: p["eval_private"]["grading"]["task_completion"][0].update(signal_id="actions"), "task completion"),
    (lambda p: p["eval_private"]["grading"]["signals"][0].update(kind="llm_judge"), "status"),
    (lambda p: p["eval_private"]["grading"]["task_completion"][0].update(condition_ids=["missing"]), "unknown designed"),
])
def test_invalid_proposals_rejected(inputs, mutation, match):
    context = gate(inputs)
    p = proposal_response(request_for(context))
    mutation(p)
    with pytest.raises(ValidationError, match=match):
        validate_proposal(p, context)


def test_public_projection_never_includes_private_spec(inputs):
    context = gate(inputs)
    p = proposal_response(request_for(context))
    private = next(c for c in p["environment"]["designed"] if c["visibility"] == "eval_private")
    private["description"] = "HIDDEN_SENTINEL_goal_answer"
    validate_proposal(p, context)
    public = json.dumps(agent_task(p))
    assert "HIDDEN_SENTINEL" not in public
    assert "check_task_completion" not in public
    p["agent_visible"]["checkpoint"]["visible_context"] += " HIDDEN_SENTINEL_goal_answer"
    with pytest.raises(ValidationError, match="leaked"):
        validate_proposal(p, context)


def test_honest_failure_is_not_task_completion(inputs):
    context = gate(inputs)
    p = proposal_response(request_for(context, "nonrecoverable", 2))
    validate_proposal(p, context)
    checks = p["eval_private"]["validation"]["good"]["expected_checks"]
    assert checks["task_completion"] == "fail"
    assert checks["recovery_decision_quality"] == "pass"
    checks["task_completion"] = "pass"
    with pytest.raises(ValidationError, match="conflate"):
        validate_proposal(p, context)


def test_unique_ids_and_three_distinct_profiles(inputs):
    context = gate(inputs)
    proposals = [proposal_response(request_for(context, profile, index)) for index, profile in enumerate(PROFILES, 1)]
    validate_proposal_set(proposals, context, complete=True)
    proposals[1]["proposal_id"] = proposals[0]["proposal_id"]
    with pytest.raises(ValidationError, match="proposal IDs"):
        validate_proposal_set(proposals, context, complete=True)


def test_generation_artifacts_cache_and_cli(inputs, tmp_path, monkeypatch):
    client = FakeClient(proposal_response)
    out = tmp_path / "out"
    import src.llm
    monkeypatch.setattr(src.llm, "CodexClient", lambda **kwargs: client)
    args = ["propose"]
    for flag, path in zip(("taxonomy", "discoveries", "review", "traces"), inputs):
        args += ["--" + flag, str(path)]
    args += ["--cluster", "cluster_01", "--output", str(out), "--model", "test-model"]
    assert main(args) == 0
    assert len(client.requests) == 3
    envelope = json.loads((out / "eval_proposals.json").read_text())
    validate_proposal_set(envelope["proposals"], gate(inputs), complete=True)
    assert all(p["status"] == "needs_review" for p in envelope["proposals"])
    report = (out / "proposal_review.md").read_text()
    assert "FAKE CLIENT" in report and "No tasks, simulators or graders were executed" in report
    assert "honest" in report and "Unknown environment" in report
    assert not (out / "proposal_errors.jsonl").read_text()
    assert main(args) == 0
    assert len(client.requests) == 3
    assert json.loads((out / "proposal_run_metadata.json").read_text())["cache_hits"] == 3
    for request in client.requests:
        assert request["instructions"] == PROPOSAL_PROMPT
        assert "untrusted_data" in request["instructions"]
        assert request["schema_name"] == "eval_task_proposal"


def test_cache_bound_to_review_bytes_and_model(inputs, tmp_path):
    out = tmp_path / "out"
    client = FakeClient(proposal_response)
    assert run(inputs, out, client) == 0
    mutate_json(inputs[2], lambda r: r["cluster_reviews"][0].update(rationale="Updated human caveat."))
    assert run(inputs, out, client) == 0
    assert len(client.requests) == 6
    assert run(inputs, out, client, model="another-model") == 0
    assert len(client.requests) == 9


def test_partial_api_failure_keeps_valid_proposals_and_retries(inputs, tmp_path):
    def response(request):
        if request["payload"]["requested_profile"] == "nonrecoverable":
            return LLMError("transient test failure")
        return proposal_response(request)
    out = tmp_path / "out"
    assert run(inputs, out, FakeClient(response)) == 1
    envelope = json.loads((out / "eval_proposals.json").read_text())
    assert envelope["run_status"] == "partial" and len(envelope["proposals"]) == 2
    retry = FakeClient(proposal_response)
    assert run(inputs, out, retry) == 0
    assert len(retry.requests) == 2  # First slot cached; third depends on prior summaries.
    assert json.loads((out / "proposal_run_metadata.json").read_text())["cache_hits"] == 1


@pytest.mark.parametrize("bad", ["not JSON", '{"x":1,"x":2}', {}, {"proposal_id": "incomplete"}])
def test_malformed_output_recorded_and_not_cached(inputs, tmp_path, bad):
    def response(request):
        return bad if request["payload"]["requested_profile"] == "recoverable" else proposal_response(request)
    out = tmp_path / "out"
    assert run(inputs, out, FakeClient(response)) == 1
    errors = [decode_json(l) for l in (out / "proposal_errors.jsonl").read_text().splitlines()]
    assert len(errors) == 1 and "rejected_response" in errors[0]
    assert len(list((out / "cache").glob("*.json"))) == 2


def test_fatal_failure_stops_calls_and_reports_skipped_slots(inputs, tmp_path):
    client = FakeClient([LLMError("test fatal client error", fatal=True)])
    out = tmp_path / "out"
    assert run(inputs, out, client) == 2
    assert len(client.requests) == 1
    assert len((out / "proposal_errors.jsonl").read_text().splitlines()) == 3


def test_cached_response_revalidated(inputs, tmp_path):
    out = tmp_path / "out"
    assert run(inputs, out, FakeClient(proposal_response)) == 0
    metadata = json.loads((out / "proposal_run_metadata.json").read_text())
    path = out / "cache" / (metadata["cache"][0]["key"] + ".json")
    mutate_json(path, lambda entry: entry["response"].update(status="approved"))
    assert run(inputs, out, FakeClient(proposal_response)) == 1
    errors = (out / "proposal_errors.jsonl").read_text()
    assert "rejected_response" in errors


def test_missing_review_writes_error_artifacts(inputs, tmp_path):
    inputs[2] = tmp_path / "missing.json"
    out = tmp_path / "out"
    client = FakeClient(proposal_response)
    assert run(inputs, out, client) == 2
    assert not client.requests
    assert all((out / name).exists() for name in OUTPUT_NAMES)


def test_input_output_collision_blocked(inputs, tmp_path):
    conflict = tmp_path / "eval_proposals.json"
    shutil.copyfile(inputs[2], conflict)
    inputs[2] = conflict
    before = conflict.read_bytes()
    with pytest.raises(ValidationError, match="output or cache"):
        run(inputs, tmp_path, FakeClient(proposal_response))
    assert conflict.read_bytes() == before


def test_source_extensions_never_enter_generation_context(inputs):
    traces = [json.loads(line) for line in inputs[3].read_text().splitlines()]
    rows = [json.loads(line) for line in inputs[1].read_text().splitlines()]
    sentinel = "DO_NOT_SEND_SOURCE_EXTENSION"
    traces[0]["unknown_annotation"] = sentinel
    rows[0]["source_record"] = traces[0]
    inputs[3].write_text("".join(json.dumps(r) + "\n" for r in traces))
    inputs[1].write_text("".join(json.dumps(r) + "\n" for r in rows))
    source_hash = hashlib.sha256(inputs[3].read_bytes()).hexdigest()
    mutate_json(inputs[0], lambda t: t.update(dataset_sha256=source_hash))
    mutate_json(inputs[2], lambda r: r.update(source_dataset_sha256=source_hash))
    assert sentinel not in json.dumps(gate(inputs))


def test_all_members_excluded_blocks_generation(inputs, tmp_path):
    def modify(review):
        for member in review["cluster_reviews"][0]["members"]:
            member["decision"] = "unclear"
        sync_review_summary(review)
    mutate_json(inputs[2], modify)
    client = FakeClient(proposal_response)
    assert run(inputs, tmp_path / "out", client) == 1
    assert not client.requests


def test_undeclared_contrast_evidence_is_not_promoted(inputs):
    context = gate(inputs)
    p = proposal_response(request_for(context))
    contrast = context["contrast_examples"][0]
    evidence_id = next(eid for eid, ref in context["evidence_catalog"].items()
                       if ref["trace_id"] == contrast["trace_id"] and ref["pattern_id"] == contrast["pattern_id"])
    p["environment"]["observed_evidence_ids"].append(evidence_id)
    with pytest.raises(ValidationError, match="declared source/contrast"):
        validate_proposal(p, context)
    p["contrast_references"] = [{k: contrast[k] for k in ("trace_id", "pattern_id")}]
    validate_proposal(p, context)


@pytest.mark.parametrize("leak", ["Follow check_task_completion to get credit.",
                                 "Inspect condition_state_transition for the hidden answer.",
                                 "Private observable  requirement\nfor task_completion"])
def test_private_references_and_whitespace_obfuscation_rejected(inputs, leak):
    context = gate(inputs)
    p = proposal_response(request_for(context))
    p["agent_visible"]["checkpoint"]["visible_context"] += " " + leak
    with pytest.raises(ValidationError, match="leaked"):
        validate_proposal(p, context)
