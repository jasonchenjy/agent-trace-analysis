"""Engineer-facing proposal review; all generated/source prose is escaped."""

from .discovery_report import safe, evidence_lines
from .proposal_schema import DIMENSIONS


def bullets(values):
    return [f"- {safe(v)}" for v in values]


def render_proposal_review(metadata, context, proposals, errors):
    lines = ["# Evaluation proposal review", "",
             f"Run: {safe(metadata['status'])}; provider: {safe(metadata['provider'])}; model: {safe(metadata['model'])}.",
             f"Prompt: {safe(metadata['prompt_version'])}; dataset: `{metadata['dataset_sha256']}`.",
             f"Proposals: {len(proposals)}/3; errors: {len(errors)}.", "",
             "**All proposals: needs_review. No tasks, simulators or graders were executed.**",
             "Structural/reference validation is not benchmark validation. Scenario diversity, solvability, oracle",
             "feasibility and semantic leakage still require human review. This entire document is eval-private.", ""]
    if metadata["provider"] != "openai_codex":
        lines += ["**FAKE CLIENT / TEST OUTPUT — not real model-generated eval proposals.**", ""]
    if context:
        cluster = context["clusters"][0]
        lines += ["## Reviewed source", "", f"{safe(cluster['cluster_id'])}: {safe(cluster['name'])}", "",
                  f"Human rationale: {safe(cluster['review_rationale'])}",
                  f"Construction caveat: {safe(context['review_note'])}", ""]
        lines += bullets(context["identity_limitations"])
        lines += ["", "### Accepted failure memberships", ""]
        for member in cluster["accepted_members"]:
            review = member["review"]
            lines += [f"- {safe(member['trace_id'])} / {safe(member['pattern_id'])}: {safe(review['reason'])}",
                      f"  Reviewer caveat: {safe(review['caveat']) or 'None supplied.'}"]
            lines += evidence_lines(member["cluster_evidence"])
        lines += ["", "### Excluded memberships", ""]
        lines += [f"- {safe(m['trace_id'])} / {safe(m['pattern_id'])}: {safe(m['review']['decision'])}; "
                  f"{safe(m['review']['reason'])}" for m in cluster["excluded_members"]] or ["None in the selected cluster."]
    for p in proposals:
        private, environment = p["eval_private"], p["environment"]
        lines += ["", f"## {safe(p['proposal_id'])}: {safe(p['title'])}", "",
                  f"Status: needs_review; scenario profile: {safe(p['scenario_profile'])}.",
                  f"Capability: {safe(p['capability_under_test'])}", f"Rationale: {safe(p['rationale'])}", "",
                  "### Agent-visible task (proposed public boundary)", "",
                  f"User request: {safe(p['agent_visible']['user_request'])}"]
        checkpoint = p["agent_visible"]["checkpoint"]
        lines += [f"Checkpoint context: {safe(checkpoint['visible_context'])}",
                  f"Recent tool failure: {safe(checkpoint['recent_tool_failure'])}", "", "Available tools:", ""]
        lines += bullets(checkpoint["available_tools"])
        lines += ["", "### Provenance: observed excerpts and reviewed statements", "",
                  "Observed excerpts establish only what the trace reports; historical environment fidelity is not established.", ""]
        for eid in environment["observed_evidence_ids"]:
            ref = context["evidence_catalog"][eid]
            role = "contrast only" if {"trace_id": ref["trace_id"], "pattern_id": ref["pattern_id"]} in p["contrast_references"] else "accepted failure source"
            lines += [f"- {safe(eid)}: {safe(ref['trace_id'])} / {safe(ref['pattern_id'])} ({role})"]
            lines += evidence_lines([ref["evidence"]])
        for rid in environment["reviewed_statement_ids"]:
            ref = context["review_catalog"][rid]
            lines += [f"- Review {safe(rid)}, {safe(ref['pointer'])}: {safe(ref['text'])}"]
        if p["contrast_references"]:
            lines += ["", "Contrast inspirations are not approved failure examples:", ""]
            for ref in p["contrast_references"]:
                contrast = next(c for c in context["contrast_examples"] if c["trace_id"] == ref["trace_id"] and c["pattern_id"] == ref["pattern_id"])
                lines += [f"- {safe(ref['trace_id'])} / {safe(ref['pattern_id'])}: {safe(contrast['pattern']['name'])}; "
                          f"original hypothesis: {safe(contrast['pattern']['assessment'])}."]
                for review in contrast["membership_reviews"]:
                    lines += [f"  Review: {safe(review['decision'])}; {safe(review['reason'])}; caveat: {safe(review['caveat'])}"]
        lines += ["", "### Designed environment (all introduced for this evaluation)", ""]
        for c in environment["designed"]:
            lines += [f"- {safe(c['condition_id'])} — {safe(c['category'])}; visibility: {safe(c['visibility'])}.",
                      f"  Condition: {safe(c['description'])}", f"  Design rationale: {safe(c['rationale'])}"]
        lines += ["", "Unknown environment details:", ""] + bullets(environment["unknown"])
        solution = private["valid_solution_space"]
        lines += ["", "### Eval-private acceptable solution space", ""] + bullets(solution["acceptable_strategies"])
        lines += ["", f"Outcome verification: {safe(solution['outcome_verification'])}", "", "Disallowed shortcuts:", ""]
        lines += bullets(solution["disallowed_shortcuts"])
        grading = private["grading"]
        lines += ["", "### Proposed grading contract — not executable", "",
                  f"Goal feasibility under designed conditions: {safe(grading['expected_feasibility'])}.",
                  "Task completion requires the user's actual goal; correct inability handling does not earn completion credit.", ""]
        for signal in grading["signals"]:
            lines += [f"- Signal {safe(signal['signal_id'])} ({safe(signal['kind'])}): {safe(signal['description'])}",
                      f"  Proposed collection: {safe(signal['collection_proposal'])}"]
        for dimension in DIMENSIONS:
            lines += ["", f"#### {safe(dimension)}", ""]
            for c in grading[dimension]:
                lines += [f"- {safe(c['criterion_id'])}: {safe(c['requirement'])}",
                          f"  Signal {safe(c['signal_id'])} {safe(c['operator'])} {safe(c['expected_value'])}; "
                          f"design conditions: {safe(', '.join(c['condition_ids']))}."]
        validation = private["validation"]
        lines += ["", "### Behavioral hypotheses — not executed test results", ""]
        for name in ("good", "bad", "superficial"):
            example = validation[name]
            lines += [f"- {name}: {safe(example['behavior'])}", f"  Rationale: {safe(example['rationale'])}",
                      "  Expected checks: " + "; ".join(f"{safe(d)}={safe(example['expected_checks'][d])}" for d in DIMENSIONS)]
        for name in ("false_positives", "false_negatives", "open_questions"):
            lines += ["", f"#### {safe(name)}", ""] + bullets(validation[name])
    lines += ["", "## Errors", ""]
    lines += [f"- {safe(e['stage'])}, {safe(e.get('proposal_id', 'input'))}: {safe(e['message'])}" for e in errors] or ["None."]
    return "\n".join(lines) + "\n"
