"""Bounded offline contract probes using Pipeline 3's unmodified interpreter/grader.

No agent runner, approvals, network or historical artifact writes. Exploration is
complete only for state/event-count equivalence classes within the reported bound;
semantic relevance of observations and natural language remains a human question.
"""
from collections import Counter, deque
from copy import deepcopy
from itertools import product
import json

from .benchmark.environment import FiniteStateEnvironment, arguments
from .benchmark.grader import grade, LIMITATION
from .proposal_contract import validate_contract, TAGS
from .review_gate import require

CHECK_VERSION = 'offline-validity-1'


def score(proposal, env, events):
    return grade(proposal, env, events, json.dumps({'task_completed': env.goal, 'summary': 'Offline contract probe.'}))


def requests(proposal):
    for tool in proposal['eval_private']['environment_model']['tools']:
        for op in tool['operations']:
            for values in product(*(a['allowed_values'] for a in op['arguments'])):
                yield {'tool': tool['name'], 'operation': op['name'],
                       'arguments': dict(zip((a['name'] for a in op['arguments']), values))}
    yield {'tool': '__invalid__', 'operation': '__invalid__', 'arguments': {}}


def offline_checks(proposal, max_nodes=1200):
    validate_contract(proposal)  # reachability, goal fixtures, types, safety, impossible tasks
    initial = FiniteStateEnvironment(proposal)
    queue = deque([(initial, [], [])])
    seen, states, goals, witnesses, counts = set(), set(), {}, {}, Counter()
    actions = list(requests(proposal))
    explored = 0
    enqueued = set()
    def witness(kind, path, result, **extra):
        counts[kind] += 1
        witnesses.setdefault(kind, {'kind': kind, 'path': path, 'dimensions': result['dimensions'], **extra})
    while queue and explored < max_nodes:
        env, events, path = queue.popleft()
        key = (env.state, tuple(sum(e['event_tag'] == tag for e in events) for tag in TAGS))
        if key in seen:
            continue
        seen.add(key)
        explored += 1
        states.add(env.state)
        result = score(proposal, env, events)
        safe = not any(e['event_tag'] in ('unauthorized_attempt', 'invalid_request') for e in events)
        if result['dimensions']['recovery_decision_quality'] and not env.goal:
            witness('recovery_credit_without_goal', path, result,
                    interpretation='Not automatically a defect: inspect partial progress or legitimate impossible-task diagnosis.')
        if result['dimensions']['recovery_decision_quality'] and not any(e['event_tag'] == 'progress' for e in events):
            witness('observation_only_recovery_credit', path, result,
                    interpretation='Inspect whether the credited observation is relevant; event tags do not prove diagnosis.')
        if env.goal and safe:
            signature = tuple(result['dimensions'].values())
            previous = goals.setdefault(env.state, {})
            if previous and signature not in previous:
                witness('equivalent_goal_scoring_difference', path, result, other_path=next(iter(previous.values())))
            previous.setdefault(signature, {'path': path, 'dimensions': result['dimensions']})
        if any(e['event_tag'] == 'unauthorized_attempt' for e in events) and result['task_success']:
            witness('unauthorized_overall_success', path, result)
        if not env.terminal and len(events) < env.max_steps:
            # Enqueue at most one representative per next state/count class.
            for request in actions:
                nxt = deepcopy(env)
                _, event = nxt.execute(request)
                new_events = events + [event]
                nk = (nxt.state, tuple(sum(e['event_tag'] == tag for e in new_events) for tag in TAGS))
                if nk not in seen and nk not in enqueued:
                    queue.append((nxt, new_events, path + [request]))
                    enqueued.add(nk)
    return {'version': CHECK_VERSION, 'contract_validation': 'passed',
            'explored_classes': explored, 'node_limit': max_nodes, 'truncated': bool(queue),
            'reachable_states_observed': sorted(states), 'successful_scoring_classes': sum(map(len, goals.values())),
            'witnesses': list(witnesses.values()), 'witness_counts': dict(counts),
            'nonrecoverable_scoring': 'completion remains false; handling scored independently',
            'limitations': [LIMITATION, 'State/count deduplication does not establish semantic equivalence or solution diversity.',
                           'Invalid requests are represented by one no-state-change invalid event.',
                           'Bounded probes are not empirical Agent or robustness evaluation.']}


def check_variants(proposal, variants):
    results = []
    for v in variants:
        row = {'variant_id': v['variant_id'], 'requires_pipeline3_execution': True,
               'semantic_invariants': 'declared_not_empirically_verified'}
        if v['kind'] == 'error_wording':
            changed = deepcopy(proposal)
            changed['agent_visible']['checkpoint']['recent_tool_failure'] = v['replacement_feedback']
            changed['eval_private']['environment_model']['checkpoint']['response']['feedback'] = v['replacement_feedback']
            validate_contract(changed)
            row.update(structural_invariants='environment dynamics, permissions, fixtures and grading unchanged',
                       grading_equivalent=True, semantic_review='Verify wording preserves all relevant information and does not leak the answer.')
        else:
            outcomes = []
            for sequence in v['sequences']:
                env, events = FiniteStateEnvironment(proposal), []
                for call in sequence:
                    require(not env.terminal and len(events) < env.max_steps, 'variant exceeds terminal/step boundary')
                    request = {'tool': call['tool'], 'operation': call['operation'], 'arguments': arguments(call)}
                    require(env.valid_request(request), 'variant contains an undeclared request')
                    _, event = env.execute(request)
                    events.append(event)
                outcomes.append({'state': env.state, 'goal': env.goal, 'dimensions': score(proposal, env, events)['dimensions']})
            row.update(outcomes=outcomes, grading_equivalent=all((o['goal'], o['dimensions']) == (outcomes[0]['goal'], outcomes[0]['dimensions']) for o in outcomes),
                       structural_invariants='identical environment and grading; only tool-call sequences vary')
        results.append(row)
    return results
