from dataclasses import replace
from io import BytesIO
from types import SimpleNamespace as NS
from urllib.error import HTTPError

import pytest

from harness.control_plane import FileRunStore
from harness.edit_transport_experiment import EditResponsibilityExperiment
from harness.errors import ExecutionHttpError, WorkerProtocolError
from harness.executor.models import ExecutionStage
from harness.integration.native_transport_recovery import NativeTransportCheckpoint, NativeRecoveryError
from harness.simple_worker_v2.edit_intent import TypedEditIntentV1
from test_edit_transport_experiment import native
from test_simple_native_mutation import setup
from test_simple_worker_v2 import decision
from harness.simple_worker_v2.interaction_state import fingerprint, encoded


def http_error():
    error=ExecutionHttpError('Ollama HTTP 500',status_code=500,response_error_kind='HTTP_SERVER_ERROR')
    error.__cause__=HTTPError('http://localhost/api/chat',500,'server error',{},BytesIO(b'{}'))
    error.failure_context=NS(stage=ExecutionStage.GENERATION)
    return error


def fixture(tmp_path,monkeypatch,values,observer=None):
    _,todo,old,_,_,session,_=setup(tmp_path,monkeypatch)
    store=FileRunStore(tmp_path/'ledger')
    run=old.get_run('r')
    store.create_goal(old.get_goal(run.goal_id))
    store.create_todo(old.get_todo(todo.todo_id))
    store.create_run(run)
    ledger=NativeTransportCheckpoint(store=store,session=session,contract=todo.work_contract,
        lease_id='lease',model={'logical_id':'qwen27_heavy_quality','num_gpu':39,'num_ctx':32768})
    versions=ledger.versions(('source.py','tests/test_source.py'))
    intent=TypedEditIntentV1(1,session.run_id,todo.todo_id,fingerprint(encoded(todo.work_contract.to_dict())),
        todo.work_contract.objective,(tuple(versions[0]),),(tuple(versions[1]),),(),())
    ledger.initialize(intent)
    pending={}
    def transport(*a,**kw):
        if callable(pending['value']): pending['value']=pending['value']()
        if isinstance(pending['value'],BaseException): raise pending['value']
        return {}
    client=NS(_request=transport)
    requests=[]
    def execute(request):
        requests.append(request)
        value=values.pop(0)
        pending['value']=value
        client._request('http://localhost','/api/chat',timeout_seconds=1,
            payload={'model':'qwen','messages':[m.to_dict() for m in request.messages],
                     'options':{'num_ctx':32768,'num_gpu':39,'temperature':0.0}})
        return pending['value']
    events=[]
    def observe(k,p):
        events.append((k,p))
        if observer: observer(k,p)
    engine=EditResponsibilityExperiment(NS(execute=execute),client,observe,checkpoint=ledger)
    engine.decision=decision()
    return engine,ledger,session,todo,requests,events


def test_stage(engine,session):
    return engine.stage(variant='native',responsibility='TEST',objective='Add a regression test.',
        session=session,paths=('source.py','tests/test_source.py'),targets=('tests/test_source.py',),
        verify=lambda stage:{'outcome':'PASSED','results':[]})
test_stage.__test__=False


def test_http_retry_one_mutation_existing_attempt_capacity(tmp_path,monkeypatch):
    change=native({'file':'tests/test_source.py','start_line':2,'end_line':2,
                  'replacement_lines':['    assert 2 == 2']})
    engine,ledger,session,_,requests,events=fixture(tmp_path,monkeypatch,[http_error(),change])
    before=(tmp_path/'source.py').read_bytes()
    result=test_stage(engine,session)
    assert result['status']=='PHASE7_PASS' and result['calls']==2
    assert [r.request_id for r in requests]==['r:TEST:0','r:TEST:1']
    assert requests[0].messages==requests[1].messages
    assert len([k for k,_ in events if k=='EDIT_APPLIED'])==1
    assert (tmp_path/'source.py').read_bytes()==before
    assert ledger.get('TEST:0:HTTP_SERVER_ERROR')['mutation_disposition']=='NONE'
    assert ledger.get('TEST:1:STARTED') and not ledger.get('TEST:2:STARTED')


def test_repeated_http_failure_bounded_and_restart_cannot_attempt_two(tmp_path,monkeypatch):
    engine,ledger,session,_,requests,_=fixture(tmp_path,monkeypatch,[http_error(),http_error()])
    result=test_stage(engine,session)
    assert result['status']=='FAILED' and result['failure']['category']=='HTTP_SERVER_ERROR'
    assert len(requests)==2 and result['changed_paths']==[]
    with pytest.raises(NativeRecoveryError,match='AMBIGUOUS'):
        test_stage(engine,session)
    assert len(requests)==2


def test_stale_source_rejects_retry(tmp_path,monkeypatch):
    def fail():
        (tmp_path/'source.py').write_text('value = 9\n')
        return http_error()
    engine,_,session,_,requests,_=fixture(tmp_path,monkeypatch,[fail])
    with pytest.raises(NativeRecoveryError,match='STALE_NATIVE_TEST_REQUEST'):
        test_stage(engine,session)
    assert len(requests)==1


def test_late_bookkeeping_exception_after_mutation_never_replays(tmp_path,monkeypatch):
    change=native({'file':'tests/test_source.py','start_line':2,'end_line':2,
                  'replacement_lines':['    assert 2 == 2']})
    def observe(k,p):
        if k=='EDIT_APPLIED': raise http_error()
    engine,ledger,session,_,requests,_=fixture(tmp_path,monkeypatch,[change],observe)
    with pytest.raises(ExecutionHttpError): test_stage(engine,session)
    assert ledger.get('TEST:0:MUTATION_STARTED') and ledger.get('TEST:0:RESPONSE_ACCEPTED')
    assert ledger.get('TEST:0:HTTP_SERVER_ERROR') is None
    changed=(tmp_path/'tests/test_source.py').read_bytes()
    with pytest.raises(NativeRecoveryError,match='AMBIGUOUS'):
        test_stage(engine,session)
    assert len(requests)==1 and (tmp_path/'tests/test_source.py').read_bytes()==changed


def test_restart_between_attempts_restores_test_preserves_source(tmp_path,monkeypatch):
    def interrupt(k,p):
        if k=='EDIT_RETRY_ELIGIBLE': raise SystemExit('simulated process exit')
    change=native({'file':'tests/test_source.py','start_line':2,'end_line':2,
                  'replacement_lines':['    assert 2 == 2']})
    values=[http_error(),change]
    engine,ledger,session,todo,requests,_=fixture(tmp_path,monkeypatch,values,interrupt)
    source_result={'status':'PROVISIONAL_SOURCE_PASS','calls':1,'changed_paths':['source.py'],
                   'report':{'outcome':'PASSED','results':[]}}
    ledger.put('SOURCE:PASS',result=source_result,versions=ledger.versions(('source.py','tests/test_source.py')))
    with pytest.raises(SystemExit): test_stage(engine,session)
    reopened=NativeTransportCheckpoint(store=FileRunStore(tmp_path/'ledger'),session=session,
        contract=todo.work_contract,lease_id='lease',model=ledger.binding['model'])
    restored=reopened.resume_intent()
    restored.validate_current(session,todo.work_contract)
    assert restored.source_targets==('source.py',)
    engine.checkpoint=reopened
    engine.observe=lambda *_:None
    result=engine.execute(decision=decision(),variant='native',split=True,
        objective='Add a regression test.',session=session,source_paths=restored.source_targets,
        test_paths=restored.test_targets,verify=lambda stage:{'outcome':'PASSED','results':[]})
    assert result['outcome']=='VERIFIED' and result['stages'][0]['calls']==0
    assert [r.request_id for r in requests]==['r:TEST:0','r:TEST:1']
    assert (tmp_path/'source.py').read_text()=='value = 1\n'
    # Same completed responsibility is idempotent after another restart.
    engine.checkpoint=NativeTransportCheckpoint(store=FileRunStore(tmp_path/'ledger'),session=session,
        contract=todo.work_contract,lease_id='lease',model=ledger.binding['model'])
    again=engine.execute(decision=decision(),variant='native',split=True,
        objective='Add a regression test.',session=session,source_paths=restored.source_targets,
        test_paths=restored.test_targets,verify=lambda stage:pytest.fail('no repeated verification mutation stage'))
    assert again['model_calls']==0 and len(requests)==2


@pytest.mark.parametrize('error',[WorkerProtocolError('bad JSON'),TimeoutError('timeout'),
    ExecutionHttpError('500 without actual generation HTTPError cause')])
def test_other_failures_not_transport_retried(tmp_path,monkeypatch,error):
    engine,ledger,session,_,requests,_=fixture(tmp_path,monkeypatch,[error])
    assert test_stage(engine,session)['status']=='FAILED'
    assert len(requests)==1 and ledger.get('TEST:0:HTTP_SERVER_ERROR') is None


def test_changed_profile_is_stale_not_new_retry_namespace(tmp_path,monkeypatch):
    _,ledger,session,todo,_,_=fixture(tmp_path,monkeypatch,[])
    with pytest.raises(NativeRecoveryError,match='STALE_NATIVE_TEST_REQUEST'):
        NativeTransportCheckpoint(store=ledger.store,session=session,contract=todo.work_contract,
            lease_id='lease',model={**ledger.binding['model'],'num_gpu':0})


def test_simple_adapter_charges_retry_existing_quota_and_does_not_rediscover(tmp_path,monkeypatch):
    from test_simple_native_mutation import action, resolve
    owner,todo,store,executor,_,_,_=setup(tmp_path,monkeypatch)
    edits=executor.values.copy()
    executor.values=[action('VIEW',{'path':''}),action('VIEW',{'path':'source.py'}),
        action('VIEW',{'path':'tests'}),action('VIEW',{'path':'tests/test_source.py'}),resolve(),*edits]
    original=executor.execute
    def transport(*a,**kw):
        if current['request'].request_id.endswith(':TEST:0'): raise http_error()
        return {}
    current={}
    executor.client._request=transport
    requests=[]
    def execute(request):
        requests.append(request)
        current['request']=request
        if request.request_id.endswith(':TEST:0'):
            executor.calls+=1
        if ':SOURCE:' in request.request_id or ':TEST:' in request.request_id:
            executor.client._request('http://localhost','/api/chat',timeout_seconds=1,
                payload={'model':'qwen','messages':[m.to_dict() for m in request.messages],
                         'options':{'num_ctx':32768,'num_gpu':39,'temperature':0.0}})
        return original(request)
    executor.execute=execute
    result=owner.execute(todo,profile_id='p',objective_with_context=todo.work_contract.objective,
                         attempt_index=1,fresh_workspace=True)
    assert result.finalized
    assert store.get_run('r').quota_usage.worker_calls==8
    assert [r.request_id for r in requests if ':SOURCE:' in r.request_id]==['r:SOURCE:0']
    assert [r.request_id for r in requests if ':TEST:' in r.request_id]==['r:TEST:0','r:TEST:1']
    assert (tmp_path/'source.py').read_text()=='value = 2\n'


def test_known_semantic_failure_keeps_existing_outer_recovery_distinct(tmp_path,monkeypatch):
    engine,ledger,session,todo,_,_=fixture(tmp_path,monkeypatch,[native(count=0),native(count=0)])
    assert test_stage(engine,session)['status']=='FAILED'
    later=NativeTransportCheckpoint(store=FileRunStore(tmp_path/'ledger'),session=session,
        contract=todo.work_contract,lease_id='lease',model=ledger.binding['model'])
    assert later.generation==1 and later.resume_intent() is None
    assert later.get('TEST:0:STARTED') is None
    # Constructor does not write or charge an inference/recovery budget.
    assert len(later.records())==0


def test_valid_transport_response_then_http_like_bookkeeping_error_not_retryable(tmp_path,monkeypatch):
    engine,ledger,session,todo,requests,_=fixture(tmp_path,monkeypatch,[native()])
    original=engine.executor.execute
    def late(request):
        original(request)
        raise http_error()
    engine.executor.execute=late
    result=test_stage(engine,session)
    assert result['failure']['category']=='AMBIGUOUS_NATIVE_INVOCATION'
    assert ledger.get('TEST:0:RESPONSE_RECEIVED') and ledger.get('TEST:0:HTTP_SERVER_ERROR') is None
    reopened=NativeTransportCheckpoint(store=FileRunStore(tmp_path/'ledger'),session=session,
        contract=todo.work_contract,lease_id='lease',model=ledger.binding['model'])
    assert reopened.generation==0
    engine.checkpoint=reopened
    with pytest.raises(NativeRecoveryError,match='AMBIGUOUS'): test_stage(engine,session)
    assert len(requests)==1


def protocol_error():
    error=http_error()
    error.response_error_kind='TOOL_CALL_PROTOCOL_ERROR'
    error.protocol_error_reason='JSON_EOF'
    return error


def test_provider_tool_rejection_uses_existing_fresh_repair_not_transport_retry(tmp_path,monkeypatch):
    change=native({'file':'tests/test_source.py','start_line':2,'end_line':2,
                  'replacement_lines':['    assert 2 == 2']})
    engine,ledger,session,_,requests,events=fixture(tmp_path,monkeypatch,[protocol_error(),change])
    result=test_stage(engine,session)
    assert result['status']=='PHASE7_PASS' and len(requests)==2
    assert requests[0].messages!=requests[1].messages
    assert 'FRESH_REPAIR' in requests[1].messages[1].content
    assert 'TOOL_CALL_PROTOCOL_ERROR' in requests[1].messages[1].content
    assert not any(k.startswith('EDIT_RETRY_') for k,_ in events)
    assert ledger.get('TEST:0:HTTP_SERVER_ERROR') is None
    assert ledger.get('TEST:0:PROTOCOL_ERROR')['response_disposition']=='REJECTED'
    assert len([k for k,_ in events if k=='EDIT_APPLIED'])==1


def test_repeated_provider_protocol_error_does_not_open_another_generation(tmp_path,monkeypatch):
    engine,ledger,session,todo,requests,_=fixture(tmp_path,monkeypatch,[protocol_error(),protocol_error()])
    result=test_stage(engine,session)
    assert result['failure']['category']=='TOOL_CALL_PROTOCOL_ERROR' and len(requests)==2
    reopened=NativeTransportCheckpoint(store=FileRunStore(tmp_path/'ledger'),session=session,
        contract=todo.work_contract,lease_id='lease',model=ledger.binding['model'])
    assert reopened.generation==0
    engine.checkpoint=reopened
    with pytest.raises(NativeRecoveryError,match='AMBIGUOUS'): test_stage(engine,session)
    assert len(requests)==2


def test_protocol_repair_resume_preserves_source_and_repair_feedback(tmp_path,monkeypatch):
    def interrupt(k,p):
        if k=='EDIT_REJECTED': raise SystemExit('after provider rejection')
    change=native({'file':'tests/test_source.py','start_line':2,'end_line':2,
                  'replacement_lines':['    assert 2 == 2']})
    engine,ledger,session,todo,requests,_=fixture(tmp_path,monkeypatch,[protocol_error(),change],interrupt)
    ledger.put('SOURCE:PASS',result={'status':'PROVISIONAL_SOURCE_PASS','calls':1,'changed_paths':['source.py'],
        'report':{'outcome':'PASSED','results':[]}},versions=ledger.versions(('source.py','tests/test_source.py')))
    with pytest.raises(SystemExit): test_stage(engine,session)
    reopened=NativeTransportCheckpoint(store=FileRunStore(tmp_path/'ledger'),session=session,
        contract=todo.work_contract,lease_id='lease',model=ledger.binding['model'])
    reopened.resume_intent().validate_current(session,todo.work_contract)
    engine.checkpoint=reopened;engine.observe=lambda *_:None
    assert test_stage(engine,session)['status']=='PHASE7_PASS'
    assert 'FRESH_REPAIR' in requests[-1].messages[1].content
    assert [r.request_id for r in requests]==['r:TEST:0','r:TEST:1']
    assert (tmp_path/'source.py').read_text()=='value = 1\n'
