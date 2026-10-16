"""Pipeline 2 validity sidecars; executable proposal contract remains v2."""
from copy import deepcopy

from .discovery_schema import STRING, STRINGS, array, obj, validate_shape as validate_scalar_shape
from .proposal_schema import PROPOSAL_SCHEMA, EXPECTED
from .proposal_contract import ARGUMENT, choice
from .review_gate import require

def validate_shape(value, schema, path='response'):
    """Keep legacy nonempty scalars, with explicit optional-empty sidecar fields."""
    if schema['type'] == 'object':
        require(isinstance(value, dict) and set(value) == set(schema['properties']), f'{path}: missing or unexpected fields')
        for k, sub in schema['properties'].items():
            validate_shape(value[k], sub, f'{path}.{k}')
    elif schema['type'] == 'array':
        require(isinstance(value, list), f'{path}: expected array')
        for i, item in enumerate(value):
            validate_shape(item, schema['items'], f'{path}[{i}]')
    elif not (path.endswith('.replacement_feedback') and value == '' and type(value) is str):
        validate_scalar_shape(value, schema, path)


DESIGN_VERSION = "validity-design-1"
ASSESSMENT_VERSION = "validity-assessment-1"
STATUS = choice("design_check_passed", "needs_revision", "unresolved_uncertainty", "requires_execution")
CALL = obj(tool=STRING, operation=STRING, arguments=array(ARGUMENT))
VARIANT = obj(
    variant_id=STRING, kind=choice("error_wording", "equivalent_sequence", "diagnostic_order"),
    what_changes=STRING, invariants=obj(user_intent=STRING, recovery_feasibility=STRING,
        permission_boundaries=STRING, available_information=STRING, success_criteria=STRING,
        valid_solution_space=STRING),
    capability_preservation=STRING, expected_grading=STRING,
    requires_execution={"type": "boolean", "enum": [True]},
    replacement_feedback=STRING, sequences=array(array(CALL)),
)
DESIGN = obj(capability=STRING, demonstrating_behaviors=STRINGS, necessity=STRING,
             possible_shortcuts=STRINGS, unrelated_skills=STRINGS, solution_leakage=STRING,
             criterion_distinctions=obj(task_completion=STRING, productive_recovery=STRING,
                                       safety_policy_compliance=STRING, final_claim_accuracy=STRING),
             proposed_variants=array(VARIANT))
CONSTRUCTION_SCHEMA = obj(proposal=PROPOSAL_SCHEMA, validity_design=DESIGN)
FINDING = obj(finding_id=STRING, severity=choice("critical", "major", "minor"),
              evidence=STRING, threat=STRING, suggested_modification=STRING,
              blocks_human_review={"type": "boolean"})
DIMENSION = obj(status=STATUS, findings=array(FINDING))
EXAMPLE = obj(kind=choice("good", "bad", "superficial"), behavior=STRING,
              expected_checks=EXPECTED, rationale=STRING)
ASSESSMENT_SCHEMA = obj(proposal_id=STRING, construct_validity=DIMENSION,
    criterion_validity=DIMENSION,
    robustness=obj(status=STATUS, findings=array(FINDING), proposed_variants=array(VARIANT)),
    behavioral_examples=array(EXAMPLE), unresolved_questions=STRINGS,
    overall_recommendation=choice("ready_for_human_review", "revise", "blocked_for_human_review"))


RECHECK_SCHEMA = deepcopy(ASSESSMENT_SCHEMA)
RECHECK_SCHEMA['properties']['finding_resolutions'] = array(obj(
    finding_id=STRING, status=choice('resolved', 'unresolved', 'uncertain'), evidence=STRING))
RECHECK_SCHEMA['required'].append('finding_resolutions')


def validate_recheck(value, proposal_id, previous):
    validate_shape(value, RECHECK_SCHEMA)
    validate_assessment({k: v for k, v in value.items() if k != 'finding_resolutions'}, proposal_id)
    resolutions = value['finding_resolutions']
    ids = [r['finding_id'] for r in resolutions]
    require(len(ids) == len(set(ids)) and set(ids) == {f['finding_id'] for f in findings(previous)},
            're-critique must account for every original finding exactly once')
    require(all(r['evidence'].strip() for r in resolutions), 'finding resolutions need concrete revised-proposal evidence')
    if any(r['status'] != 'resolved' for r in resolutions):
        require(value['overall_recommendation'] != 'ready_for_human_review', 'unresolved original findings cannot disappear')


def validate_variants(variants):
    require(1 <= len(variants) <= 3, "propose one to three controlled variants")
    require(len({v['variant_id'] for v in variants}) == len(variants), "duplicate variant ID")
    for v in variants:
        validate_shape(v, VARIANT)
        require(all(v['invariants'].values()), "all six variant invariants must be explicit")
        if v['kind'] == 'error_wording':
            require(bool(v['replacement_feedback']) and not v['sequences'], "wording variant requires feedback only")
        else:
            require(not v['replacement_feedback'] and 2 <= len(v['sequences']) <= 3,
                    "behavior variant requires two or three explicit sequences")
            require(all(0 < len(s) <= 12 for s in v['sequences']), "bounded nonempty sequences required")
            for sequence in v['sequences']:
                for call in sequence:
                    names = [a['name'] for a in call['arguments']]
                    require(len(names) == len(set(names)), "duplicate variant call arguments")


def validate_construction(value):
    validate_shape(value, CONSTRUCTION_SCHEMA)
    d = value['validity_design']
    require(all(d[k] for k in ('capability', 'demonstrating_behaviors', 'necessity',
                              'possible_shortcuts', 'unrelated_skills', 'solution_leakage')),
            "validity design must explicitly address construct threats")
    validate_variants(d['proposed_variants'])


def findings(assessment):
    return [f for d in ('construct_validity', 'criterion_validity', 'robustness') for f in assessment[d]['findings']]


def validate_assessment(value, proposal_id):
    validate_shape(value, ASSESSMENT_SCHEMA)
    require(value['proposal_id'] == proposal_id, "critic proposal identity mismatch")
    fs = findings(value)
    require(len({f['finding_id'] for f in fs}) == len(fs), "duplicate critic finding IDs")
    require(all(f['evidence'] and f['threat'] and f['suggested_modification'] for f in fs),
            "findings need evidence, explanation and actionable suggestions")
    for d in ('construct_validity', 'criterion_validity', 'robustness'):
        require(not (value[d]['status'] == 'design_check_passed' and
                     any(f['severity'] in ('major', 'critical') for f in value[d]['findings'])),
                "design pass contradicts major/critical findings")
    require(value['robustness']['status'] != 'design_check_passed', "robustness needs empirical execution")
    require(len(value['behavioral_examples']) >= 3 and
            {e['kind'] for e in value['behavioral_examples']} == {'good', 'bad', 'superficial'},
            "critic must supply good, bad and superficial scoring examples")
    validate_variants(value['robustness']['proposed_variants'])
    if any(f['severity'] in ('major', 'critical') for f in fs):
        require(value['overall_recommendation'] != 'ready_for_human_review', "unresolved major findings require revision")
