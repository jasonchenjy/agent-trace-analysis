"""Review artifact: escaped model text, concrete source quotes, no grading."""

import html
import re


def safe(value):
    # JSON can contain escaped lone surrogates. Preserve their visible escape
    # rather than failing to write an otherwise valid UTF-8 review artifact.
    text = str(value).encode("utf-8", errors="backslashreplace").decode("utf-8")
    return re.sub(r'([\\`*_{}\[\]()#+.!|>~-])', r'\\\1', html.escape(text)).replace('\n', ' ')


def evidence_lines(evidence):
    return [f"  - Event {ref['event_index']}, call {safe(ref['call_id'])}, field {safe(ref['field'])}: "
            f"“{safe(ref['quote'])}”" for ref in evidence]


def render_review(metadata, records, taxonomy, errors):
    lines = ["# Discovery review", "", f"Provider: {safe(metadata['provider'])}; model: {safe(metadata['model'])}.",
             f"Dataset SHA-256: `{metadata['dataset_sha256']}`.",
             f"Run status: {safe(metadata['status'])}; analyzed: {len(records)}; errors: {len(errors)}.", "",
             "All clusters need human review. Valid references and exact excerpts do not prove semantic conclusions.",
             "Uncached model calls may produce different analyses. No baseline taxonomy is supplied.", ""]
    if metadata["provider"] not in {"openai_codex", "openai_responses"}:
        lines += ["**FAKE CLIENT / TEST OUTPUT — not actual model discoveries.**", ""]
    lines += ["## Candidate taxonomy", "", f"Taxonomy status: {safe(taxonomy['status'])}.", ""]
    grouped = set()
    for cluster in taxonomy["clusters"]:
        lines += [f"### {safe(cluster['cluster_id'])}: {safe(cluster['name'])}", "",
                  f"Definition: {safe(cluster['definition'])}",
                  f"Common behavior: {safe(cluster['common_behavior'])}",
                  f"Candidate capability gap (hypothesis): {safe(cluster['candidate_capability_gap'])}",
                  "Status: needs_review.", ""]
        example = cluster["representative_example"]
        lines += [f"Representative: {safe(example['trace_id'])} / {safe(example['pattern_id'])}.", ""]
        for member in cluster["members"]:
            grouped.add((member["trace_id"], member["pattern_id"]))
            lines.append(f"- Trace {safe(member['trace_id'])}, pattern {safe(member['pattern_id'])}:")
            lines += evidence_lines(member["evidence"])
        lines += ["", "Uncertainties / alternatives:", ""]
        lines += [f"- {safe(u)}" for u in cluster["uncertainties"]] or ["- None supplied; human review still required."]
        lines.append("")
    lines += ["## All sessions and findings", "",
              "Successful, ambiguous, and ungrouped findings remain available here and in trace_discoveries.jsonl.", ""]
    for record in records:
        result = record["analysis"]
        lines += [f"### {safe(result['trace_id'])} (source line {record['source_line']})", "",
                  f"User request: {safe(record['observations']['user_request'])}",
                  f"Goal interpretation: {safe(result['user_goal'])}", ""]
        for name in ("execution_summary", "obstacle", "agent_response", "outcome"):
            finding = result[name]
            lines += [f"{name}: {safe(finding.get('statement', finding.get('reasoning')))}"]
            if name == "outcome":
                lines.append(f"Outcome hypothesis: {safe(finding['status'])}")
            lines += evidence_lines(finding["evidence"])
            lines += [f"  - Unknown: {safe(u)}" for u in finding["unknowns"]]
            lines.append("")
        for fact in result["observed_facts"]:
            lines.append(f"Observed fact proposed by model: {safe(fact['statement'])}")
            lines += evidence_lines(fact["evidence"])
        lines.append("")
        for pattern in result["candidate_patterns"]:
            membership = "grouped" if (result["trace_id"], pattern["pattern_id"]) in grouped else "ungrouped"
            lines += [f"- {safe(pattern['name'])}: {safe(pattern['assessment'])}, {membership}.",
                      f"  Description: {safe(pattern['description'])}",
                      f"  Interpretation: {safe(pattern['reasoning'])}"]
            lines += evidence_lines(pattern["evidence"])
            lines += [f"  - Unknown: {safe(u)}" for u in pattern["unknowns"]]
        if not result["candidate_patterns"]:
            lines.append("No candidate pattern asserted.")
        lines += ["", "Missing information:", ""]
        lines += [f"- {safe(u)}" for u in result["missing_information"]] or ["- None specified."]
        lines.append("")
    lines += ["## Open review questions", ""]
    lines += [f"- {safe(q)}" for q in taxonomy["open_questions"]]
    lines += ["- Do the cited events support each interpretation and cluster definition?",
              "- Are alternative explanations, successful recoveries, and missing evidence represented fairly?",
              "- Which clusters should be merged, split, renamed, or rejected?", "", "## Errors", ""]
    lines += [f"- {safe(e['stage'])}, trace {safe(e.get('trace_id'))}: {safe(e['message'])}" for e in errors]
    if not errors:
        lines.append("None.")
    return "\n".join(lines) + "\n"
