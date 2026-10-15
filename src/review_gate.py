"""Deterministic gate for the supplied human review v1 contract; no LLM decisions."""

from copy import deepcopy
import hashlib
from pathlib import Path

from .discovery import digest
from .discovery_schema import decode_json, validate_trace_discovery, validate_taxonomy
from .observations import extract_observations
from .schema import ValidationError, read_jsonl, _fields, _string


GATE_VERSION = "human-review-v1"
CLUSTER_DECISIONS = {"approve", "hold_for_policy_context", "reject_as_failure_cluster"}
MEMBER_DECISIONS = {"accepted", "rejected", "unclear"}


def require(condition, message):
    if not condition:
        raise ValidationError(message)


def load_object(path):
    value = decode_json(Path(path).read_text(encoding="utf-8"))
    require(isinstance(value, dict), f"{path}: expected JSON object")
    return value


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _strings(record, names, path):
    for name in names:
        _string(record[name], f"{path}.{name}")


def _list(value, path):
    require(isinstance(value, list), f"{path}: expected array")
    return value


def _member_review(member, observations, patterns, path, singleton=False):
    fields = {"trace_id", "pattern_id", "decision", "reason", "evidence_event_indices"}
    fields |= {"priority", "pattern_name"} if singleton else {"caveat"}
    _fields(member, fields, set(), path)
    _strings(member, fields - {"evidence_event_indices", "caveat"}, path)
    if not singleton:
        _string(member["caveat"], path + ".caveat", nonempty=False)
    require(member["decision"] in MEMBER_DECISIONS, f"{path}: unknown membership decision")
    key = (member["trace_id"], member["pattern_id"])
    require(key in patterns, f"{path}: invalid trace/pattern reference {key}")
    indices = _list(member["evidence_event_indices"], path + ".evidence_event_indices")
    valid = {e["event_index"] for e in observations[key[0]]["events"]}
    require(bool(indices) and all(type(i) is int and i in valid for i in indices),
            f"{path}: invalid reviewer evidence event index")
    require(len(indices) == len(set(indices)), f"{path}: duplicate reviewer evidence index")
    return key


def build_review_context(taxonomy_path, discoveries_path, review_path, traces_path, cluster_id):
    """Validate all supplied references; return one approved cluster and labeled contrasts.

    Raw extension fields stay outside the model context. Source hashes identify exact
    input bytes; the human review itself only pins the dataset, not taxonomy prose.
    """
    paths = dict(taxonomy=taxonomy_path, discoveries=discoveries_path,
                 review=review_path, traces=traces_path)
    hashes = {name: file_hash(path) for name, path in paths.items()}
    taxonomy, review = load_object(taxonomy_path), load_object(review_path)
    require(taxonomy.get("schema_version") == 1 and taxonomy.get("status") == "completed",
            "taxonomy must be a completed discovery v1 artifact")
    require(taxonomy.get("input_complete") is True, "taxonomy input is incomplete")
    require(taxonomy.get("dataset_sha256") == hashes["traces"], "taxonomy dataset hash mismatch")
    observations, source_records, source_lines = {}, {}, {}
    for item in read_jsonl(Path(traces_path)):
        require(item.error is None, f"invalid source trace: {item.error}")
        trace = item.trace
        observations[trace.trace_id] = extract_observations(trace)
        source_records[trace.trace_id] = trace.source_record if trace.source_record is not None else {
            "trace_id": trace.trace_id, "user_request": trace.user_request, "events": list(trace.events)}
        source_lines[trace.trace_id] = item.line_number
    require(bool(observations), "source dataset is empty")
    analyses = {}
    for line, raw in enumerate(Path(discoveries_path).read_text(encoding="utf-8").splitlines(), 1):
        row = decode_json(raw)
        require(isinstance(row, dict), f"discovery line {line}: expected object")
        tid = row.get("trace_id")
        require(isinstance(tid, str) and tid in observations and tid not in analyses,
                f"discovery line {line}: unknown or duplicate trace ID")
        require(row.get("schema_version") == 1, "unsupported discovery schema version")
        require(row.get("source_record") == source_records[tid], f"discovery source record mismatch: {tid}")
        require(type(row.get("source_line")) is int and row["source_line"] == source_lines[tid],
                f"discovery source line mismatch: {tid}")
        require(row.get("observations") == observations[tid], f"discovery observations mismatch: {tid}")
        validate_trace_discovery(row.get("analysis"), observations[tid])
        analyses[tid] = row["analysis"]
    require(set(analyses) == set(observations), "discovery coverage does not match source dataset")
    require(taxonomy.get("analyzed_trace_ids") == list(analyses), "taxonomy discovery coverage mismatch")
    require("clusters" in taxonomy and "open_questions" in taxonomy, "missing taxonomy fields")
    validate_taxonomy({k: taxonomy[k] for k in ("clusters", "open_questions")}, observations, analyses)
    clusters = {c["cluster_id"]: c for c in taxonomy["clusters"]}
    patterns = {(tid, p["pattern_id"]): p for tid, a in analyses.items() for p in a["candidate_patterns"]}
    all_grouped = {(m["trace_id"], m["pattern_id"]) for c in clusters.values() for m in c["members"]}

    _fields(review, {"review_version", "review_scope", "source_taxonomy_url", "source_dataset_sha256",
                     "decision_policy", "cluster_reviews", "singleton_reviews", "summary",
                     "eval_construction_note"}, set(), "review")
    require(type(review["review_version"]) is int and review["review_version"] == 1,
            "unsupported human review version")
    _strings(review, {"review_scope", "source_taxonomy_url", "source_dataset_sha256",
                      "eval_construction_note"}, "review")
    require(review["source_dataset_sha256"] == hashes["traces"], "human review dataset hash mismatch (stale review)")
    _fields(review["decision_policy"], MEMBER_DECISIONS, set(), "review.decision_policy")
    _strings(review["decision_policy"], MEMBER_DECISIONS, "review.decision_policy")
    reviewed, membership_counts = {}, {k: 0 for k in MEMBER_DECISIONS}
    decisions = {}
    for index, item in enumerate(_list(review["cluster_reviews"], "cluster_reviews")):
        path = f"cluster_reviews[{index}]"
        _fields(item, {"cluster_id", "original_name", "review_decision", "recommended_name", "rationale",
                       "eval_priority", "members"}, set(), path)
        _strings(item, set(item) - {"members"}, path)
        cid = item["cluster_id"]
        require(cid in clusters and cid not in reviewed, f"{path}: unknown or duplicate cluster ID")
        require(item["original_name"] == clusters[cid]["name"], f"{cid}: stale review cluster name")
        require(item["review_decision"] in CLUSTER_DECISIONS, f"{cid}: invalid cluster review decision")
        expected = {(m["trace_id"], m["pattern_id"]) for m in clusters[cid]["members"]}
        seen = set()
        for pos, member in enumerate(_list(item["members"], path + ".members")):
            key = _member_review(member, observations, patterns, f"{path}.members[{pos}]")
            require(key in expected and key not in seen, f"{cid}: stale or duplicate membership reference")
            seen.add(key)
            membership_counts[member["decision"]] += 1
            decisions.setdefault(key, []).append({"cluster_id": cid, **deepcopy(member)})
        require(seen == expected, f"{cid}: review memberships do not match taxonomy (stale/incomplete review)")
        reviewed[cid] = item
    singleton_keys = set()
    for i, member in enumerate(_list(review["singleton_reviews"], "singleton_reviews")):
        key = _member_review(member, observations, patterns, f"singleton_reviews[{i}]", singleton=True)
        require(key not in all_grouped and key not in singleton_keys, "invalid or duplicate singleton review")
        singleton_keys.add(key)
    summary = review["summary"]
    _fields(summary, {"cluster_memberships_total", "membership_decisions", "singletone_candidates_reviewed",
                      "approved_failure_clusters", "conditional_cluster", "not_approved_failure_clusters",
                      "high_priority_singletons"}, set(), "review.summary")
    for name, expected in (("cluster_memberships_total", sum(membership_counts.values())),
                           ("singletone_candidates_reviewed", len(singleton_keys))):
        require(type(summary[name]) is int and summary[name] == expected, f"review summary mismatch: {name}")
    require(summary["membership_decisions"] == membership_counts, "review membership summary mismatch")
    for name, decision in (("approved_failure_clusters", "approve"), ("conditional_cluster", "hold_for_policy_context"),
                           ("not_approved_failure_clusters", "reject_as_failure_cluster")):
        ids = _list(summary[name], name)
        require(all(isinstance(cid, str) for cid in ids) and len(ids) == len(set(ids))
                and set(ids) == {cid for cid, r in reviewed.items() if r["review_decision"] == decision},
                f"review summary mismatch: {name}")
    high = _list(summary["high_priority_singletons"], "high_priority_singletons")
    require(all(isinstance(tid, str) and any(tid == k[0] for k in singleton_keys) for tid in high),
            "invalid high-priority singleton reference")
    require(cluster_id in reviewed, f"selected cluster {cluster_id} has no human review")
    selected = reviewed[cluster_id]
    require(selected["review_decision"] == "approve",
            f"selected cluster {cluster_id} is not approved: {selected['review_decision']}")
    accepted, excluded = [], []
    original_members = {(m["trace_id"], m["pattern_id"]): m for m in clusters[cluster_id]["members"]}
    for member in selected["members"]:
        key = (member["trace_id"], member["pattern_id"])
        entry = {"trace_id": key[0], "pattern_id": key[1], "pattern": deepcopy(patterns[key]),
                 "cluster_evidence": deepcopy(original_members[key]["evidence"]), "review": deepcopy(member)}
        (accepted if member["decision"] == "accepted" else excluded).append(entry)
    require(bool(accepted), f"selected cluster {cluster_id} has no accepted memberships")
    accepted_keys = {(m["trace_id"], m["pattern_id"]) for m in accepted}
    contrasts = []
    for key, pattern in patterns.items():
        reviews = decisions.get(key, [])
        if key not in accepted_keys and (pattern["assessment"] == "successful_behavior"
                                        or any(r["decision"] in {"rejected", "unclear"} for r in reviews)):
            contrasts.append({"trace_id": key[0], "pattern_id": key[1], "role": "contrast_only",
                              "approval": "not_an_approved_failure_example",
                              "pattern": deepcopy(pattern), "membership_reviews": deepcopy(reviews)})
    # Catalogs make observations/review quotations copyable without model-authored
    # historical environment assertions. They do not establish causal conclusions.
    evidence_catalog, review_catalog = {}, {}
    for entry in accepted + contrasts:
        for evidence in entry["pattern"]["evidence"]:
            value = {"trace_id": entry["trace_id"], "pattern_id": entry["pattern_id"], "evidence": evidence}
            evidence_catalog["e_" + digest(value)[:16]] = value
    for field in ("rationale",):
        value = {"pointer": f"/cluster_reviews/{list(reviewed).index(cluster_id)}/{field}", "text": selected[field]}
        review_catalog["r_" + digest(value)[:16]] = value
    for i, member in enumerate(selected["members"]):
        if member["decision"] != "accepted":
            continue
        for field in ("reason", "caveat"):
            if member[field]:
                value = {"pointer": f"/cluster_reviews/{list(reviewed).index(cluster_id)}/members/{i}/{field}",
                         "text": member[field]}
                review_catalog["r_" + digest(value)[:16]] = value
    relevant_ids = {m["trace_id"] for m in accepted + contrasts}
    return {
        "schema_version": 1, "gate_version": GATE_VERSION, "status": "approved_for_proposal_generation_only",
        "dataset_sha256": hashes["traces"], "input_source_hashes": hashes,
        "review_policy": deepcopy(review["decision_policy"]), "review_note": review["eval_construction_note"],
        "identity_limitations": ["Human review pins dataset bytes but not taxonomy/analysis content hashes; names, memberships and evidence are checked, not prose revision history."],
        "clusters": [{"cluster_id": cluster_id, "name": selected["recommended_name"],
                      "original_name": clusters[cluster_id]["name"], "review_decision": "approve",
                      "review_rationale": selected["rationale"], "eval_priority": selected["eval_priority"],
                      "discovery_hypothesis": {k: deepcopy(clusters[cluster_id][k]) for k in
                                               ("definition", "common_behavior", "candidate_capability_gap", "uncertainties")},
                      "accepted_members": accepted, "excluded_members": excluded}],
        "contrast_examples": contrasts, "evidence_catalog": evidence_catalog, "review_catalog": review_catalog,
        "source_context": [{"source_line": source_lines[tid], **observations[tid]} for tid in observations if tid in relevant_ids],
    }
