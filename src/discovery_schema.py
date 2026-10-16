"""Small JSON contracts and source-reference validation, not a semantic grader."""

import json

from .schema import ValidationError, _invalid_constant, _unique_object


VALIDATION_VERSION = "references-v1"


def obj(**properties):
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


def array(items):
    return {"type": "array", "items": items}


STRING = {"type": "string"}
STRINGS = array(STRING)
EVIDENCE = obj(event_index={"type": "integer"}, call_id={"type": ["string", "null"]},
               field=STRING, quote=STRING)
FINDING = obj(statement=STRING, evidence=array(EVIDENCE), unknowns=STRINGS)
PATTERN = obj(pattern_id=STRING, name=STRING, description=STRING,
              assessment={"type": "string", "enum": ["potential_failure", "successful_behavior", "unclear"]},
              evidence=array(EVIDENCE), reasoning=STRING, unknowns=STRINGS)
TRACE_SCHEMA = obj(
    trace_id=STRING, user_goal=STRING, execution_summary=FINDING,
    observed_facts=array(obj(statement=STRING, evidence=array(EVIDENCE))),
    obstacle=FINDING, agent_response=FINDING,
    outcome=obj(status={"type": "string", "enum": ["achieved", "not_achieved", "unknown"]},
                reasoning=STRING, evidence=array(EVIDENCE), unknowns=STRINGS),
    candidate_patterns=array(PATTERN), missing_information=STRINGS,
)
MEMBER = obj(trace_id=STRING, pattern_id=STRING, evidence=array(EVIDENCE))
CLUSTER = obj(cluster_id=STRING, name=STRING, definition=STRING, members=array(MEMBER),
              representative_example=obj(trace_id=STRING, pattern_id=STRING),
              common_behavior=STRING, candidate_capability_gap=STRING,
              uncertainties=STRINGS, status={"type": "string", "enum": ["needs_review"]})
TAXONOMY_SCHEMA = obj(clusters=array(CLUSTER), open_questions=STRINGS)


def decode_json(text):
    try:
        return json.loads(text, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
    except (ValueError, TypeError, RecursionError) as exc:
        raise ValidationError("response is not a valid unique-key JSON object") from exc


def validate_shape(value, schema, path="response"):
    """Validate the small schema subset above; shared with the API contract."""
    kind = schema["type"]
    kinds = kind if isinstance(kind, list) else [kind]
    checks = {"object": isinstance(value, dict), "array": isinstance(value, list),
              "string": isinstance(value, str), "integer": type(value) is int,
              "boolean": type(value) is bool, "null": value is None}
    if not any(checks[k] for k in kinds):
        raise ValidationError(f"{path}: expected {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValidationError(f"{path}: invalid assessment/status")
    if kind == "object":
        if set(value) != set(schema["properties"]):
            raise ValidationError(f"{path}: missing or unexpected fields")
        for key, sub in schema["properties"].items():
            validate_shape(value[key], sub, f"{path}.{key}")
    elif kind == "array":
        for i, item in enumerate(value):
            validate_shape(item, schema["items"], f"{path}[{i}]")
    elif isinstance(value, str) and not value.strip():
        raise ValidationError(f"{path}: empty string")


def evidence_value(event, pointer):
    """Resolve an RFC 6901 JSON pointer into the neutral source event."""
    if not pointer.startswith("/"):
        raise ValidationError("evidence field must be a JSON pointer starting with /")
    value = event
    try:
        for part in pointer[1:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if isinstance(value, list):
                if not part.isdecimal():
                    raise KeyError(part)
                value = value[int(part)]
            else:
                value = value[part]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise ValidationError("evidence field is absent in source event") from exc
    if isinstance(value, (list, dict)):
        raise ValidationError("evidence must quote a scalar source field")
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=True)


def validate_evidence(refs, observations, required=True):
    if required and not refs:
        raise ValidationError("missing evidence for finding")
    events = {e["event_index"]: e for e in observations["events"]}
    seen = set()
    for ref in refs:
        event = events.get(ref["event_index"])
        if event is None:
            raise ValidationError(f"unknown event_index {ref['event_index']} in trace {observations['trace_id']}")
        if ref["call_id"] != event.get("call_id"):
            raise ValidationError("evidence call_id does not belong to the referenced event")
        quote = ref["quote"]
        if len(quote) > 300 or quote not in evidence_value(event, ref["field"]):
            raise ValidationError("evidence quote must be an exact source excerpt of at most 300 characters")
        key = json.dumps(ref, sort_keys=True)
        if key in seen:
            raise ValidationError("duplicate evidence reference")
        seen.add(key)


def validate_trace_discovery(value, observations):
    validate_shape(value, TRACE_SCHEMA)
    if value["trace_id"] != observations["trace_id"]:
        raise ValidationError("response trace_id does not match input session")
    for name in ("execution_summary", "obstacle", "agent_response", "outcome"):
        finding = value[name]
        validate_evidence(finding["evidence"], observations, required=False)
        if not finding["evidence"] and not finding["unknowns"]:
            raise ValidationError(f"{name}: missing evidence must be reported in unknowns")
    if value["outcome"]["status"] != "unknown":
        validate_evidence(value["outcome"]["evidence"], observations)
    for fact in value["observed_facts"]:
        validate_evidence(fact["evidence"], observations)
    seen = set()
    for pattern in value["candidate_patterns"]:
        if pattern["pattern_id"] in seen:
            raise ValidationError("duplicate pattern_id within session")
        seen.add(pattern["pattern_id"])
        validate_evidence(pattern["evidence"], observations)
    return value


def validate_taxonomy(value, observations, discoveries):
    validate_shape(value, TAXONOMY_SCHEMA)
    seen = set()
    for cluster in value["clusters"]:
        if cluster["cluster_id"] in seen:
            raise ValidationError("duplicate cluster_id")
        seen.add(cluster["cluster_id"])
        members = set()
        for member in cluster["members"]:
            trace_id = member["trace_id"]
            if trace_id not in observations or trace_id not in discoveries:
                raise ValidationError(f"cluster references unknown or unanalyzed trace {trace_id}")
            patterns = {p["pattern_id"]: p for p in discoveries[trace_id]["candidate_patterns"]}
            pattern = patterns.get(member["pattern_id"])
            if pattern is None or pattern["assessment"] != "potential_failure":
                raise ValidationError("failure cluster member must reference a potential_failure pattern")
            key = (trace_id, member["pattern_id"])
            if key in members:
                raise ValidationError("duplicate cluster member")
            members.add(key)
            validate_evidence(member["evidence"], observations[trace_id])
            if any(ref not in pattern["evidence"] for ref in member["evidence"]):
                raise ValidationError("cluster evidence must come from the referenced pattern")
        if len({trace for trace, _ in members}) < 2:
            raise ValidationError("recurring cluster requires at least two distinct traces")
        example = cluster["representative_example"]
        if (example["trace_id"], example["pattern_id"]) not in members:
            raise ValidationError("representative example is not a cluster member")
    return value
