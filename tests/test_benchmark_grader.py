from copy import deepcopy
import json
import pytest
from src.benchmark.environment import FiniteStateEnvironment, ToolGateway
from src.benchmark.grader import parse_claim, predicate, grade
from src.benchmark.runner import run_episode, ModelAgent
from src.benchmark.scripted import ScriptedAgent, controls, tool, finish
from src.benchmark.qualification import qualify
from src.llm import LLMError
from test_benchmark_environment import specs, SPECS


@pytest.mark.parametrize('raw', ['{}','[]','null','{"task_completed":"true","summary":"x"}',
    '{"task_completed":1,"summary":"x"}', '{"task_completed":true,"summary":null}',
    '{"task_completed":true,"summary":"x","extra":1}',
    '{"task_completed":true,"task_completed":false,"summary":"x"}',
    '```json\n{"task_completed":true,"summary":"x"}\n```',
    '{"task_completed":false,"summary":"x"} trailing', '{"task_completed":NaN,"summary":"x"}',
    '{"task_completed":false,"summary":"x","summary":"y"}'])
def test_strict_claims(raw):
    assert parse_claim(raw) is None


def test_valid_claim_and_typed_operators():
    assert parse_claim(' \n{"task_completed":false,"summary":"x"}\t')['task_completed'] is False
    assert not predicate(True,'equals',1)
    assert predicate(2,'at_least',1) and predicate(2,'at_most',3)
    assert predicate(2,'not_equals',1) and predicate(False,'not_equals',True)
    assert not predicate(True,'at_least',False)


def test_narrative_not_graded(specs):
    env=FiniteStateEnvironment(specs[1])
    result=grade(specs[1],env,[], '{"task_completed":false,"summary":"Everything succeeded!"}')
    assert result['dimensions']['final_response_accuracy']
    assert not result['dimensions']['task_completion']


def test_all_controls_and_report(specs,tmp_path):
    assert qualify(SPECS,tmp_path/'qualification')==0
    report=json.loads((tmp_path/'qualification/qualification_report.json').read_text())
    assert len(report['checks'])==15 and report['passed']


class BrokenAgent:
    def __init__(self,error): self.error=error
    def choose(self,*args): raise self.error


@pytest.mark.parametrize('error,status',[(TimeoutError(),'timeout'),(LLMError('unavailable'),'model_error')])
def test_run_errors_not_task_failures(specs,error,status):
    episode,result=run_episode(specs[0],BrokenAgent(error),model='test',harness='baseline',run_id='r',trial_id=1,provenance={})
    assert result['execution_status']==status and result['scores'] is None
    assert episode['final_response'] is None


def test_final_only_rejects_further_execution(specs):
    actions=[tool('clock','wait',ticks=2),tool('check','run',target='theme-preview')]
    episode,result=run_episode(specs[2],ScriptedAgent(actions),model='test',harness='baseline',run_id='r',trial_id=1,provenance={},max_steps=1)
    assert result['execution_status']=='malformed_action' and len(episode['events'])==1
    assert episode['final_state']=='ready'


def test_last_result_delivered_and_no_private_context(specs):
    class Spy:
        def choose(self,context,history,final_only):
            text=json.dumps([context,history]);assert 'event_tag' not in text and 'grading' not in text
            if not history:return tool('clock','wait',ticks=2)
            assert history[-1]['result']['code']=='READY' and final_only
            return finish(False)
    _,result=run_episode(specs[2],Spy(),model='test',harness='baseline',run_id='r',trial_id=1,provenance={},max_steps=1)
    assert result['scores']['handling_success'] and not result['observed_goal']


def test_model_malformed_envelope(specs):
    class Client:
        def complete(self,**kwargs): return '{"action":{"kind":"final","response":"{}"},"action":{}}'
    _,result=run_episode(specs[0],ModelAgent(Client(),'test','baseline'),model='test',harness='baseline',run_id='r',trial_id=1,provenance={})
    assert result['execution_status']=='malformed_action'
