from copy import deepcopy
import json
from pathlib import Path
import shutil
import subprocess
import pytest

from src.benchmark.approval import load_approved, write_json
from src.benchmark.benchmark import benchmark
from src.benchmark.comparison import compare, load_run
from src.benchmark.environment import FiniteStateEnvironment, ToolGateway
from src.benchmark.model_client import BoundedCodexClient
from src.benchmark.runner import ModelAgent, run_episode
from src.benchmark.scripted import finish, tool
from src.benchmark.reports import aggregate
from src.cli import main
from src.llm import LLMError
from src.review_gate import file_hash
from test_benchmark_environment import SPECS, specs, call


def relocated_approval(tmp_path):
    root=SPECS.parents[2]
    specs_path=tmp_path/'approved_eval_specs.json'
    shutil.copyfile(SPECS,specs_path)
    manifest=json.loads(SPECS.with_name('approval_manifest.json').read_text())
    manifest['repository_root']=str(root)
    write_json(tmp_path/'approval_manifest.json',manifest)
    return specs_path,manifest


@pytest.mark.parametrize('mutation',['spec','source','pending','decision','edit','commit','dataset','new_proposal'])
def test_approval_tampering(tmp_path,mutation):
    path,manifest=relocated_approval(tmp_path)
    if mutation=='spec':
        path.write_text(path.read_text()+' ')
    elif mutation=='source': manifest['source_files'][0]['sha256']='0'*64
    elif mutation=='pending':manifest['status']='pending'
    elif mutation=='decision':manifest['decisions'][0]['status']='pending'
    elif mutation=='edit':manifest['decisions'][2]['edits'][0]['after']=2
    elif mutation=='commit':manifest['source']['commit']='0'*40
    elif mutation=='dataset':manifest['source']['dataset_sha256']='0'*64
    elif mutation=='new_proposal':
        value=json.loads(path.read_text());value['proposals'].append(deepcopy(value['proposals'][0]));write_json(path,value)
        manifest['approved_specs_sha256']=file_hash(path)
    write_json(tmp_path/'approval_manifest.json',manifest)
    with pytest.raises((ValueError,subprocess.CalledProcessError)):load_approved(path)


def test_review_preserved_and_original_unchanged():
    root=SPECS.parents[2]
    assert (root/'eval_proposal_review_v2.json').read_bytes()==(root/'reviews/eval_proposal_review_v2.json').read_bytes()
    source=json.loads((root/'outputs/proposals_v2/eval_proposals.json').read_text())
    assert source['proposals'][2]['eval_private']['grading']['recovery_decision_quality'][0]['expected_value']==2
    approved,_=load_approved(SPECS)
    assert approved[2]['eval_private']['grading']['recovery_decision_quality'][0]['expected_value']==1
    assert source['proposals'][:2]==approved[:2]


class FakeClient:
    cache_identity={'provider':'test-double-not-real-model'}
    def complete(self,**kwargs):
        payload=kwargs['payload'];context=payload['task'];history=payload['history']
        text=json.dumps(payload)
        for key in ('eval_private','event_tag','state_before','state_after','source_cluster_id','expected_checks','transition_id'):
            assert key not in text
        request=context['historical_tool_exchange']['request'];target=request['arguments']['target']
        if payload['final_only']:action=finish(history[-1]['result']['code'] in ('OK','PASS','SUCCESS','CHECK_PASSED','AUDIT_COMPLETE'))
        elif target=='links':
            action=[tool('workspace','configure',key='catalog',value='english'),tool('check','run',target='links'),finish(True)][len(history)]
        elif target=='help-output':
            action=[tool('check','status',target=target),finish(False)][len(history)]
        else:
            action=[tool('clock','wait',ticks=2),tool('check','run',target=target),finish(True)][len(history)]
        # Use the verified completed code from actual tool response for test transport.
        if payload['final_only'] and len(history)==2:action=finish(True)
        return json.dumps({'action':action})


def run_pair(tmp_path,client=None,trials=2):
    for harness,out in [('baseline','a'),('recovery_guidance','b')]:
        benchmark(SPECS,tmp_path/out,model='test-model',harness=harness,trials=trials,client=client or FakeClient(),progress=lambda _:None)
    return tmp_path/'a',tmp_path/'b'


def test_repeated_comparison_and_provenance(tmp_path):
    a,b=run_pair(tmp_path)
    assert compare(a,b,tmp_path/'comparison')==0
    data=json.loads((tmp_path/'comparison/comparison.json').read_text())
    assert data['scored_pairs']==6 and data['regression_count']==0
    assert data['all_attempts']['baseline']['aggregate']['feasible_completion']['passed']==4
    assert data['all_attempts']['candidate']['aggregate']['nonrecoverable_handling_success']['passed']==2
    assert 'source_files' in data['provenance']


@pytest.mark.parametrize('field,value',[('model','other'),('trials',3),('implementation_sha256','other'),('budgets',{}),('run_kind','scripted_qualification')])
def test_unfair_comparison_rejected(tmp_path,field,value):
    a,b=run_pair(tmp_path,trials=1)
    m=json.loads((b/'run_metadata.json').read_text());m[field]=value;write_json(b/'run_metadata.json',m)
    with pytest.raises(ValueError):compare(a,b,tmp_path/'comparison')


def test_artifact_tampering_rejected(tmp_path):
    a,b=run_pair(tmp_path,trials=1)
    with (b/'episodes.jsonl').open('a') as f:f.write('\n')
    with pytest.raises(ValueError):compare(a,b,tmp_path/'comparison')


def test_model_unavailable_comparison_is_inconclusive(tmp_path):
    class Unavailable(FakeClient):
        def complete(self,**kwargs):raise LLMError('Unavailable')
    a,b=run_pair(tmp_path,Unavailable(),trials=1)
    assert compare(a,b,tmp_path/'comparison')==1
    report=json.loads((tmp_path/'comparison/comparison.json').read_text())
    assert report['status']=='inconclusive_no_scored_pairs' and report['scored_pairs']==0
    assert report['all_attempts']['baseline']['aggregate']['execution_errors']==3


def test_worker_timeout_kills_process_group(monkeypatch):
    killed=[]
    class Process:
        pid=12345
        def communicate(self,*args,**kwargs):
            if kwargs:raise subprocess.TimeoutExpired('test',.01)
            return '', ''
    monkeypatch.setattr('src.benchmark.model_client.subprocess.Popen',lambda *a,**k:Process())
    monkeypatch.setattr('src.benchmark.model_client.os.killpg',lambda *a:killed.append(a))
    with pytest.raises(TimeoutError):BoundedCodexClient(.01).complete(model='test')
    assert killed[0][0]==12345


def test_cli_and_no_overwrite(tmp_path):
    assert main(['qualify','--specs',str(SPECS),'--output',str(tmp_path/'q')])==0
    assert main(['qualify','--specs',str(SPECS),'--output',str(tmp_path/'q')])==2


def test_deterministic_events(specs):
    traces=[]
    for _ in range(2):
        env=FiniteStateEnvironment(specs[2]);gate=ToolGateway(env)
        for ticks in (1,1):gate.dispatch(call('clock','wait',ticks=ticks))
        gate.dispatch(call('check','run',target='theme-preview'));traces.append(gate.events)
    assert traces[0]==traces[1]


def test_review_side_manifest_is_portable():
    root = SPECS.parents[2]
    default, _ = load_approved(SPECS)
    reviewed, _ = load_approved(SPECS, root / 'reviews/approval_manifest_v2.json')
    assert default == reviewed
