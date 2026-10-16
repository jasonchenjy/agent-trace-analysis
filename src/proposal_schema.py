"""Proposal-only JSON contract and deterministic reference/boundary checks.

These checks cannot establish scenario quality, oracle feasibility or semantic
non-leakage of arbitrary prose. Nothing here executes a proposed condition.
"""

import re
from copy import deepcopy

from .discovery import canonical
from .discovery_schema import STRING, STRINGS, array, obj, validate_shape
from .review_gate import require
from .proposal_contract import (SCHEMA_VERSION, ENVIRONMENT_MODEL, COLLECTION, FEASIBILITY_REVIEW,
                                FINAL_RESPONSE_FORMAT, SCALAR, validate_contract)


VALIDATION_VERSION = "proposal-references-v2"
PROFILES = ("recoverable", "nonrecoverable", "retry_appropriate")
DIMENSIONS = ("task_completion", "recovery_decision_quality", "safety_policy_compliance", "final_response_accuracy")
CATEGORIES = ("tool_interface", "initial_state", "network", "filesystem", "permission",
              "tool_response", "state_transition", "fixture_dependency")


def enum(*values):
    return {"type": "string", "enum": list(values)}


SOURCE = obj(trace_id=STRING, pattern_id=STRING)
CONDITION = obj(condition_id=STRING, category=enum(*CATEGORIES),
                visibility=enum("agent_visible", "eval_private"), description=STRING, rationale=STRING)
SIGNAL = obj(signal_id=STRING, kind=enum("tool_event", "environment_state", "artifact", "test_result", "final_claim"),
             description=STRING, collection_proposal=STRING)
CRITERION = obj(criterion_id=STRING, requirement=STRING, signal_id=STRING,
                operator=enum("equals", "not_equals", "contains", "absent", "at_least", "at_most"),
                expected_value=STRING, condition_ids=STRINGS)
EXPECTED = obj(**{d: enum("pass", "fail", "not_assessed") for d in DIMENSIONS})
EXAMPLE = obj(behavior=STRING, expected_checks=EXPECTED, rationale=STRING)
PROPOSAL_SCHEMA = obj(
    proposal_id=STRING, title=STRING, status=enum("needs_review"),
    dataset_sha256=STRING, source_cluster_id=STRING, source_trace_ids=STRINGS,
    source_pattern_ids=array(SOURCE), contrast_references=array(SOURCE),
    scenario_profile=enum(*PROFILES), capability_under_test=STRING, rationale=STRING,
    agent_visible=obj(
        user_request=STRING,
        checkpoint=obj(kind=enum("continuation_after_tool_failure"), visible_context=STRING,
                       recent_tool_failure=STRING, available_tools=STRINGS),
        designed_condition_ids=STRINGS,
    ),
    environment=obj(
        historical_fidelity=enum("not_established"),
        observed_evidence_ids=STRINGS, reviewed_statement_ids=STRINGS,
        designed=array(CONDITION), unknown=STRINGS,
    ),
    eval_private=obj(
        valid_solution_space=obj(acceptable_strategies=STRINGS, disallowed_shortcuts=STRINGS,
                                 outcome_verification=STRING),
        grading=obj(
            contract_status=enum("proposal_not_executable"),
            completion_credit=enum("verified_user_goal_only"),
            expected_feasibility=enum("achievable", "not_achievable", "unknown"),
            signals=array(SIGNAL),
            **{d: array(CRITERION) for d in DIMENSIONS},
        ),
        validation=obj(status=enum("hypotheses_not_executed"), good=EXAMPLE, bad=EXAMPLE,
                       superficial=EXAMPLE, false_positives=STRINGS, false_negatives=STRINGS,
                       open_questions=STRINGS),
    ),
)

# Retain validation of archived v1 artifacts; current generation requires v2.
PROPOSAL_SCHEMA_V1 = deepcopy(PROPOSAL_SCHEMA)
PROPOSAL_SCHEMA["properties"]["schema_version"] = {"type": "integer", "enum": [SCHEMA_VERSION]}
PROPOSAL_SCHEMA["required"].append("schema_version")
public_schema = PROPOSAL_SCHEMA["properties"]["agent_visible"]
public_schema["properties"]["final_response_format"] = FINAL_RESPONSE_FORMAT
public_schema["required"].append("final_response_format")
private_schema = PROPOSAL_SCHEMA["properties"]["eval_private"]
for name, schema in (("environment_model", ENVIRONMENT_MODEL), ("feasibility_review", FEASIBILITY_REVIEW)):
    private_schema["properties"][name] = schema
    private_schema["required"].append(name)
signal_schema = private_schema["properties"]["grading"]["properties"]["signals"]["items"]
signal_schema["properties"].update(value_type=enum("boolean", "integer"), collection=COLLECTION)
signal_schema["required"] += ["value_type", "collection"]
for dimension in DIMENSIONS:
    private_schema["properties"]["grading"]["properties"][dimension]["items"]["properties"]["expected_value"] = SCALAR


def _unique(values, name, nonempty=True):
    require(not nonempty or bool(values), f"{name}: must not be empty")
    require(len(values) == len(set(values)), f"{name}: duplicate values")


def agent_task(proposal):
    """Explicit public projection, for review/export only; this is not a runner.

    Never serialize the full proposal as an agent task: it contains private design
    and grading. Even this projection requires human review for semantic leakage.
    """
    public = {**proposal["agent_visible"], "environment_conditions": [
        {"condition_id": c["condition_id"], "category": c["category"], "description": c["description"]}
        for c in proposal["environment"]["designed"] if c["visibility"] == "agent_visible"]}
    if proposal.get("schema_version") == 2:
        # Only interface names/types/domains are public, never transition tables,
        # defaults, fixture bodies, metric recipes or goal states.
        public["tool_interfaces"] = [{"name": t["name"], "operations": [
            {k: op[k] for k in ("name", "description", "arguments")} for op in t["operations"]]}
            for t in proposal["eval_private"]["environment_model"]["tools"]]
    return public


def validate_proposal(proposal, context, *, profile=None, proposal_id=None):
    version = proposal.get("schema_version", 1) if isinstance(proposal, dict) else 1
    validate_shape(proposal, PROPOSAL_SCHEMA_V1 if version == 1 else PROPOSAL_SCHEMA)
    cluster = context["clusters"][0]
    require(proposal["dataset_sha256"] == context["dataset_sha256"], "proposal dataset hash mismatch")
    require(proposal["source_cluster_id"] == cluster["cluster_id"], "proposal references unselected cluster")
    if profile is not None:
        require(proposal["scenario_profile"] == profile, "proposal scenario profile mismatch")
    if proposal_id is not None:
        require(proposal["proposal_id"] == proposal_id, "proposal ID does not match requested slot")
    accepted = {(m["trace_id"], m["pattern_id"]) for m in cluster["accepted_members"]}
    contrasts = {(m["trace_id"], m["pattern_id"]) for m in context["contrast_examples"]}
    sources = [(r["trace_id"], r["pattern_id"]) for r in proposal["source_pattern_ids"]]
    contrast_refs = [(r["trace_id"], r["pattern_id"]) for r in proposal["contrast_references"]]
    _unique(sources, "source_pattern_ids")
    _unique(contrast_refs, "contrast_references", nonempty=False)
    _unique(proposal["source_trace_ids"], "source_trace_ids")
    require(set(sources) <= accepted, "source pattern is not an accepted member of the approved cluster")
    require(set(contrast_refs) <= contrasts, "unknown contrast reference")
    require(set(proposal["source_trace_ids"]) == {t for t, _ in sources}, "source_trace_ids do not match source_pattern_ids")
    environment = proposal["environment"]
    _unique(environment["observed_evidence_ids"], "observed_evidence_ids")
    _unique(environment["reviewed_statement_ids"], "reviewed_statement_ids")
    cited_sources = set()
    for evidence_id in environment["observed_evidence_ids"]:
        require(evidence_id in context["evidence_catalog"], "unsupported observed environment fact/reference")
        ref = context["evidence_catalog"][evidence_id]
        pair = (ref["trace_id"], ref["pattern_id"])
        require(pair in set(sources + contrast_refs), "observed evidence lacks declared source/contrast provenance")
        cited_sources.add(pair)
    require(set(sources) <= cited_sources, "each source pattern needs an observed evidence reference")
    require(set(environment["reviewed_statement_ids"]) <= set(context["review_catalog"]),
            "unsupported reviewed statement reference")
    require(bool(environment["unknown"]), "environment unknowns must be explicit")
    conditions = environment["designed"]
    _unique([c["condition_id"] for c in conditions], "designed condition IDs")
    require({c["category"] for c in conditions} == set(CATEGORIES),
            "designed environment must cover all categories (state explicitly when unrestricted/not needed)")
    public_ids = {c["condition_id"] for c in conditions if c["visibility"] == "agent_visible"}
    private_ids = {c["condition_id"] for c in conditions if c["visibility"] == "eval_private"}
    visible = proposal["agent_visible"]
    _unique(visible["designed_condition_ids"], "agent-visible condition IDs")
    require(set(visible["designed_condition_ids"]) == public_ids,
            "agent-visible conditions must reference exactly public conditions, never hidden conditions")
    require(bool(visible["checkpoint"]["available_tools"]), "checkpoint must expose tool interfaces")
    private = proposal["eval_private"]
    solution = private["valid_solution_space"]
    require(len(solution["acceptable_strategies"]) >= 2, "describe multiple acceptable strategies, not one fixed sequence")
    require(bool(solution["disallowed_shortcuts"]), "missing disallowed shortcuts")
    grading = private["grading"]
    signals = {s["signal_id"]: s for s in grading["signals"]}
    _unique([s["signal_id"] for s in grading["signals"]], "signal IDs")
    criterion_ids = []
    for dimension in DIMENSIONS:
        require(bool(grading[dimension]), f"missing observable grading conditions: {dimension}")
        for criterion in grading[dimension]:
            criterion_ids.append(criterion["criterion_id"])
            require(criterion["signal_id"] in signals, "grading references missing observable signal")
            _unique(criterion["condition_ids"], "grading condition references")
            require(set(criterion["condition_ids"]) <= public_ids | private_ids,
                    "grading references unknown designed condition")
            kind = signals[criterion["signal_id"]]["kind"]
            if dimension == "task_completion":
                require(kind in {"artifact", "environment_state", "test_result"},
                        "task completion needs goal evidence, not retry stopping or final claims")
            if dimension in {"recovery_decision_quality", "safety_policy_compliance"}:
                require(kind in {"tool_event", "environment_state"}, f"{dimension} needs execution evidence")
            if dimension == "final_response_accuracy":
                require(kind == "final_claim", "final response accuracy requires observable final claims")
    _unique(criterion_ids, "criterion IDs")
    if proposal["scenario_profile"] == "nonrecoverable":
        require(grading["expected_feasibility"] == "not_achievable", "nonrecoverable scenario must not claim task feasibility")
    elif proposal["scenario_profile"] in {"recoverable", "retry_appropriate"}:
        require(grading["expected_feasibility"] == "achievable", "recoverable/retry scenario must design a feasible task")
    validation = private["validation"]
    expected_completion = {"achievable": "pass", "not_achievable": "fail", "unknown": "not_assessed"}
    require(validation["good"]["expected_checks"]["task_completion"] == expected_completion[grading["expected_feasibility"]],
            "good behavior must not conflate honest failure handling with task completion")
    require(all(validation["good"]["expected_checks"][d] == "pass" for d in DIMENSIONS[1:]),
            "good behavior must pass recovery, safety and accuracy checks")
    for name in ("bad", "superficial"):
        require("fail" in validation[name]["expected_checks"].values(), f"{name} example must fail at least one check")
    for name in ("false_positives", "false_negatives", "open_questions"):
        require(bool(validation[name]), f"validation.{name}: must identify review uncertainties")
    # Mechanical leakage checks: exact private prose/IDs cannot appear in the
    # public projection. Paraphrases and answer hints still require human review.
    public_text = " ".join(" ".join(_strings(agent_task(proposal))).casefold().split())
    private_texts = [c["description"] for c in conditions if c["visibility"] == "eval_private"]
    private_texts += [c["requirement"] for d in DIMENSIONS for c in grading[d]]
    private_texts += [validation[n]["behavior"] for n in ("good", "bad", "superficial")]
    require(not any(" ".join(t.casefold().split()) in public_text for t in private_texts),
            "private grading/hidden condition text leaked into agent-visible context")
    require(not any(re.search(r"(?<!\w)" + re.escape(i.casefold()) + r"(?!\w)", public_text)
                    for i in private_ids | set(criterion_ids)),
            "private grading/condition ID leaked into agent-visible context")
    if version == 2:
        validate_contract(proposal)
    return proposal


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)


def validate_proposal_set(proposals, context, *, complete=False):
    for proposal in proposals:
        validate_proposal(proposal, context)
    _unique([p["proposal_id"] for p in proposals], "proposal IDs", nonempty=complete)
    _unique([p["scenario_profile"] for p in proposals], "scenario profiles", nonempty=complete)
    _unique([p["title"].strip().casefold() for p in proposals], "scenario titles", nonempty=complete)
    _unique([p["agent_visible"]["user_request"].strip().casefold() for p in proposals], "scenario user requests", nonempty=complete)
    _unique([canonical(p["environment"]["designed"]) for p in proposals], "designed scenarios", nonempty=complete)
    if complete:
        require(len(proposals) == 3 and {p["scenario_profile"] for p in proposals} == set(PROFILES),
                "complete run requires three proposals covering all scenario profiles")
