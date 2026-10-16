"""Static design validation only; no simulated episode or grader execution."""

from copy import deepcopy
import hashlib
import json

import pytest

from src.eval_proposal import PROMPT_VERSION
from src.proposal_contract import CHECKLIST, complexity_summary
from src.proposal_schema import agent_task, validate_proposal, validate_proposal_set
from src.schema import ValidationError
from test_discovery import FakeClient
from test_proposals import ROOT, inputs, gate, proposal_response, request_for, run


@pytest.fixture
def candidate(inputs):
    context = gate(inputs)
    return context, proposal_response(request_for(context))


def model(p):
    return p["eval_private"]["environment_model"]


def grading(p):
    return p["eval_private"]["grading"]


@pytest.mark.parametrize("mutate,match", [
    (lambda p: model(p).update(external_dependencies=["cloud signing service"]), "external dependencies"),
    (lambda p: model(p).update(execution_mode="real_shell"), "status"),
    (lambda p: model(p).update(max_steps=1000), "budget"),
    (lambda p: model(p).update(max_steps=1), "feasibility"),
    (lambda p: model(p).update(initial_state="absent"), "initial state"),
    (lambda p: model(p).update(initial_state="done"), "continuation"),
    (lambda p: model(p)["states"].extend(deepcopy(model(p)["states"]) * 3), "states exceeds"),
    (lambda p: model(p)["transitions"].extend(deepcopy(model(p)["transitions"]) * 7), "transitions exceeds"),
    (lambda p: model(p)["fixtures"].extend(deepcopy(model(p)["fixtures"]) * 2), "fixtures exceeds"),
    (lambda p: model(p)["fixtures"][0].update(content="x" * 2049), "byte budget"),
    (lambda p: model(p)["fixtures"][0].update(content="界" * 700), "byte budget"),
    (lambda p: model(p)["tools"].extend(deepcopy(model(p)["tools"])), "tools exceeds"),
    (lambda p: model(p)["tools"][0]["operations"][0].update(name="execute_python"), "unsupported"),
    (lambda p: model(p)["tools"][0]["operations"][0]["arguments"][0].update(name="arbitrary_code"), "signature"),
    (lambda p: model(p)["tools"][2]["operations"][0]["arguments"][0].update(allowed_values=[True]), "typed finite"),
    (lambda p: model(p)["tools"][2]["operations"][0]["arguments"][0].update(allowed_values=[9]), "wait ticks"),
    (lambda p: model(p)["transitions"][0].update(to_state="absent"), "unknown state"),
    (lambda p: model(p)["transitions"].append(dict(deepcopy(model(p)["transitions"][0]), transition_id="duplicate_call")), "ambiguous"),
    (lambda p: model(p)["transitions"][0].update(from_state="done"), "terminal states"),
    (lambda p: model(p)["transitions"][0].update(to_state="ready"), "must not mutate"),
    (lambda p: model(p)["transitions"][1].update(event="observation"), "must not mutate"),
    (lambda p: model(p)["transitions"][1].update(to_state="blocked"), "state change"),
    (lambda p: model(p)["transitions"][0]["arguments"][0].update(value="other_path"), "typed finite domain"),
    (lambda p: model(p)["transitions"][0]["response"].update(fixture_ids=["result"]), "absent from resulting state"),
    (lambda p: model(p)["fixtures"][0].update(visibility="eval_private"), "private grader fixture"),
    (lambda p: model(p)["checkpoint"]["response"].update(feedback="different feedback"), "feedback differs"),
    (lambda p: model(p)["terminal"].update(failure_states=["done"]), "disjoint"),
    (lambda p: model(p)["states"][2].update(fixture_ids=["help"]), "goal evidence"),
    (lambda p: grading(p)["task_completion"][0].update(expected_value="true"), "native metric type"),
    (lambda p: grading(p)["task_completion"][0].update(expected_value=False), "actual goal"),
    (lambda p: grading(p)["recovery_decision_quality"][0].update(expected_value=True), "native metric type"),
    (lambda p: grading(p)["task_completion"][0].update(operator="at_least"), "integer metrics"),
    (lambda p: grading(p)["signals"][0].update(value_type="integer"), "collection and value type"),
    (lambda p: grading(p)["signals"][1]["collection"].update(event_tags=["unlogged_heuristic"]), "undefined event"),
    (lambda p: grading(p)["recovery_decision_quality"][0].update(operator="at_most", expected_value=1), "positive diagnostic"),
    (lambda p: grading(p)["safety_policy_compliance"][0].update(expected_value=1), "zero explicitly"),
    (lambda p: grading(p)["safety_policy_compliance"][0].update(expected_value=-1), "step budget"),
    (lambda p: p["eval_private"]["feasibility_review"].pop(), "six design dimensions"),
    (lambda p: p["eval_private"]["feasibility_review"][0].update(status="approved"), "status"),
])
def test_invalid_v2_designs_rejected(candidate, mutate, match):
    context, p = candidate
    mutate(p)
    with pytest.raises(ValidationError, match=match):
        validate_proposal(p, context)


def test_profiles_and_typed_grading_do_not_conflate_completion(inputs):
    context = gate(inputs)
    ps = [proposal_response(request_for(context, profile, i)) for i, profile in enumerate(
        ("recoverable", "nonrecoverable", "retry_appropriate"), 1)]
    validate_proposal_set(ps, context, complete=True)
    for p in ps:
        assert grading(p)["task_completion"][0]["expected_value"] is True
        assert type(grading(p)["safety_policy_compliance"][0]["expected_value"]) is int
        assert all(c["status"] == "needs_review" for c in p["eval_private"]["feasibility_review"])
        assert {c["dimension"] for c in p["eval_private"]["feasibility_review"]} == set(CHECKLIST)
    assert ps[1]["eval_private"]["validation"]["good"]["expected_checks"]["task_completion"] == "fail"
    # Giving the nonrecoverable graph an actual solution contradicts its profile.
    model(ps[1])["transitions"] = deepcopy(model(ps[0])["transitions"])
    with pytest.raises(ValidationError, match="feasibility"):
        validate_proposal(ps[1], context)


def test_public_projection_contains_interfaces_but_not_private_state(candidate):
    context, p = candidate
    model(p)["states"][0]["description"] = "PRIVATE_STATE_SENTINEL"
    validate_proposal(p, context)
    public = agent_task(p)
    assert public["final_response_format"]["completion_field"] == "task_completed"
    assert len(public["tool_interfaces"]) == 3
    serialized = json.dumps(public)
    for secret in ("PRIVATE_STATE_SENTINEL", "transitions", "success_states", "expected_value", "default_event"):
        assert secret not in serialized


def test_v1_artifacts_remain_valid_and_unchanged(inputs):
    context = gate(inputs)
    path = ROOT / "outputs/proposals/eval_proposals.json"
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    bundle = json.loads(path.read_text())
    assert bundle["schema_version"] == 1
    validate_proposal_set(bundle["proposals"], context, complete=True)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_v1_output_cannot_be_accepted_by_current_generation(inputs, tmp_path):
    legacy = json.loads((ROOT / "outputs/proposals/eval_proposals.json").read_text())["proposals"]
    assert run(inputs, tmp_path / "out", FakeClient(legacy)) == 1
    errors = (tmp_path / "out/proposal_errors.jsonl").read_text()
    assert "current generation requires proposal schema v2" in errors


def test_v2_run_uses_new_cache_identity_and_records_complexity(inputs, tmp_path):
    out = tmp_path / "out"
    cache = tmp_path / "cache"
    cache.mkdir()
    for path in (ROOT / "outputs/proposals/cache").glob("*.json"):
        (cache / path.name).write_bytes(path.read_bytes())
    client = FakeClient(proposal_response)
    assert run(inputs, out, client, cache_dir=cache) == 0
    assert len(client.requests) == 3
    metadata = json.loads((out / "proposal_run_metadata.json").read_text())
    assert metadata["prompt_version"] == PROMPT_VERSION == "eval-proposal-v2"
    assert metadata["proposal_schema_version"] == 2 and metadata["cache_hits"] == 0
    assert len(metadata["complexity"]) == 3
    assert all(c["execution_status"] == "not_executed" for c in metadata["complexity"])
    assert "Human Review #2" in (out / "proposal_review.md").read_text()
    assert run(inputs, out, client, cache_dir=cache) == 0
    assert len(client.requests) == 3


def test_complexity_summary_is_explicit(candidate):
    _, p = candidate
    counts = complexity_summary(p)
    assert counts["tools"] == 3 and counts["states"] == 3 and counts["transitions"] == 3
    assert counts["fixture_bytes"] < 1024
    assert counts["review_status"] == "needs_review"
