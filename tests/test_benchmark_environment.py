from copy import deepcopy
import json
from pathlib import Path
import pytest

from src.benchmark.approval import load_approved
from src.benchmark.environment import FiniteStateEnvironment, ToolGateway

SPECS = Path(__file__).resolve().parents[1] / 'outputs/approved/approved_eval_specs.json'

@pytest.fixture
def specs():
    return load_approved(SPECS)[0]


def call(tool, operation, **arguments):
    return dict(tool=tool, operation=operation, arguments=arguments)


def test_checkpoint_reset_and_last_step(specs):
    env = FiniteStateEnvironment(specs[0]); gate = ToolGateway(env, 2)
    assert gate.events == [] and env.state == 'blocked'
    assert env.public_context()['historical_tool_exchange']['result']['code']
    gate.dispatch(call('workspace', 'configure', key='catalog', value='english'))
    assert not env.goal
    gate.dispatch(call('check', 'run', target='links'))
    assert env.goal and gate.stop_reason == 'terminal_success'
    with pytest.raises(ValueError): gate.dispatch(call('check', 'run', target='links'))
    assert len(gate.events) == 2
    fresh = FiniteStateEnvironment(specs[0])
    assert fresh.state == 'blocked' and not fresh.goal


@pytest.mark.parametrize('tool_request',[
    call('clock','wait',ticks=True),call('clock','wait',ticks=1.0),call('clock','wait',ticks='1'),
    call('clock','wait',ticks=3),call('clock','wait'),call('clock','wait',ticks=1,event='progress'),
    call('shell','exec',command='touch /tmp/NEVER'),call('clock','unknown',ticks=1),
    dict(tool='clock',operation='wait',arguments={'ticks':1},event_tag='progress'),
    call([], 'wait', ticks=1),call('clock','wait',ticks=None),
])
def test_invalid_arguments_do_not_mutate(specs,tool_request):
    env=FiniteStateEnvironment(specs[2]); gate=ToolGateway(env)
    before=env.fixture_ids
    result=gate.dispatch(tool_request)
    assert result['code']=='INVALID_REQUEST'
    assert env.state=='pending_two' and env.fixture_ids==before
    assert gate.events[0]['event_tag']=='invalid_request'


@pytest.mark.parametrize('waits',[[2],[1,1]])
def test_wait_routes(specs,waits):
    env=FiniteStateEnvironment(specs[2]); gate=ToolGateway(env)
    for ticks in waits: gate.dispatch(call('clock','wait',ticks=ticks))
    assert env.state=='ready' and not env.goal
    gate.dispatch(call('check','run',target='theme-preview'))
    assert env.goal


def test_forbidden_and_defaults(specs):
    for p,request in zip(specs,[call('workspace','configure',key='catalog',value='unchecked'),
                              call('workspace','configure',key='baseline',value='regenerate'),
                              call('check','run',target='theme-preview-force')]):
        env=FiniteStateEnvironment(p); gate=ToolGateway(env); before=env.state
        gate.dispatch(request)
        assert env.state==before and gate.events[0]['event_tag']=='unauthorized_attempt'


def test_projection_and_event_copies(specs):
    p=deepcopy(specs[2]); p['eval_private']['environment_model']['rationale']='SECRET_SENTINEL'
    env=FiniteStateEnvironment(p); gate=ToolGateway(env,1)
    context=json.dumps(env.public_context())
    for secret in ['SECRET_SENTINEL','transitions','event_tags','eval_private','grading','f_report','pending_two']:
        assert secret not in context
    response=gate.dispatch(call('clock','wait',ticks=1))
    assert 'event_tag' not in response and 'state_after' not in response
    response['code']='FAKE'; copied=gate.events; copied[0]['event_tag']='progress'
    assert gate.events[0]['result']['code']!='FAKE'
    assert gate.stop_reason=='step_limit'
    with pytest.raises(ValueError): gate.dispatch(call('clock','wait',ticks=1))


def test_state_snapshots_replace(specs):
    env=FiniteStateEnvironment(specs[2]); gate=ToolGateway(env)
    old=set(env.fixture_ids);gate.dispatch(call('clock','wait',ticks=2))
    assert not old & set(env.fixture_ids)


def test_terminal_failure(specs):
    p=deepcopy(specs[0]); m=p['eval_private']['environment_model']
    m['states'].append(dict(state_id='failed',description='Failure',fixture_ids=[]))
    m['terminal']['failure_states']=['failed']
    t=deepcopy(next(t for t in m['transitions'] if t['event']=='progress'))
    t.update(transition_id='fail',to_state='failed',response=dict(code='FAILED',feedback='Failed',fixture_ids=[]))
    original=next(x for x in m['transitions'] if x['event']=='progress')
    m['transitions'].remove(original);m['transitions'].append(t)
    # Keep the reviewed profile graph feasible with an alternative route.
    t2=deepcopy(original);t2['transition_id']='alternative';t2['arguments'][1]['value']='unchecked'
    m['transitions']=[x for x in m['transitions'] if not(x['from_state']==t2['from_state'] and x['tool']==t2['tool'] and x['operation']==t2['operation'] and x['arguments']==t2['arguments'])]
    m['transitions'].append(t2)
    env=FiniteStateEnvironment(p);gate=ToolGateway(env)
    gate.dispatch(call('workspace','configure',key='catalog',value='english'))
    assert gate.stop_reason=='terminal_failure' and not env.goal
