from dataclasses import replace
from types import SimpleNamespace as NS
import pytest
from test_research_diagnostics_production_integration import setup_adapter
from test_edit_transport_experiment import native
from harness.integration.direct_execution_adapter import DirectExecutionAdapter
from harness.execution_policy import ExecutionModeV1


def configured(tmp_path,monkeypatch):
    owner,item,store,executor=setup_adapter(tmp_path,monkeypatch)
    (tmp_path/'source.py').write_text('value = 1\n')
    (tmp_path/'tests/test_source.py').write_text('def test_value():\n    assert True\n')
    constraints={**item.work_contract.constraints,'SCENARIO_SCOPE':'SINGLE_FILE',
                 'SCENARIO_RISK':'LOW','SCENARIO_AMBIGUITY':'LOW'}
    todo=NS(todo_id=item.todo.todo_id,work_contract=replace(item.work_contract,constraints=constraints))
    executor.client=NS(_request=lambda *a,**kw:None)
    executor.values=[native({'file':'source.py','start_line':1,'end_line':1,'replacement_lines':['value = 2']}),
        native({'file':'tests/test_source.py','start_line':1,'end_line':2,
                'replacement_lines':['from source import value','def test_value():','    assert value == 2']})]
    router=NS(_branch_for=lambda t:'coding',_eligibility=lambda *a:NS(rejection_reasons=(),feasibility=None))
    owner.common.router=router
    monkeypatch.setattr('harness.integration.direct_execution_adapter.TaskProfileFactory.build',
                        lambda *a,**kw:NS(task_type=NS(value='BUGFIX')))
    gates=[NS(gate_id=g,required=True) for g in ('candidate_diff','scope_guard','python_syntax','pytest','regression_change')]
    calls=[]
    def report(*a,**kw):
        calls.append(kw)
        return NS(to_dict=lambda:{'outcome':'PASSED','results':[]})
    coordinator=NS(compiler=NS(compile=lambda **kw:NS(gates=gates)),
        run_store=store,runner=NS(run=report),verify=lambda **kw:NS(report=report(**kw)))
    # A dataclass plan is needed for the production provisional subset projection.
    from dataclasses import make_dataclass
    Plan=make_dataclass('Plan',[('gates',tuple)])
    coordinator.compiler.compile=lambda **kw:Plan(tuple(gates))
    owner.verification=coordinator
    owner.legacy.workspace.manager.snapshot=lambda lease:NS(dirty_paths=('source.py','tests/test_source.py'),diff_text='diff')
    owner.legacy.workspace.current=lambda t:('r',NS(lease_id='lease'))
    return DirectExecutionAdapter(owner),todo,store,executor,calls


def events(store):
    return [e.to_dict()['payload'] for e in store.list_events('r')]


def test_production_native_split_durable_result_and_idempotent_replay(tmp_path,monkeypatch):
    adapter,todo,store,executor,reports=configured(tmp_path,monkeypatch)
    result=adapter.execute(todo,profile_id='p',attempt_index=1)
    assert result.finalized and result.model_calls==2
    assert result.raw['execution_mode']=='DIRECT'
    assert (tmp_path/'source.py').read_text()=='value = 2\n'
    assert len(reports)==2
    assert any(e.get('evidence_type')=='WORKER_RESULT' for e in events(store))
    assert adapter.execute(todo,profile_id='p',attempt_index=1)==result
    assert executor.calls==2
    assert adapter.execute(todo,profile_id='p',attempt_index=2) is None
    assert executor.calls==2


def test_bounded_failure_escalates_once_without_restarting_direct(tmp_path,monkeypatch):
    adapter,todo,store,executor,_=configured(tmp_path,monkeypatch)
    executor.values=[native(count=0),native(count=0)]
    assert adapter.execute(todo,profile_id='p',attempt_index=0) is None
    escalation=[e['payload'] for e in events(store) if e.get('payload',{}).get('kind')=='ESCALATION']
    assert len(escalation)==1 and escalation[0]['next_mode']=='SIMPLE'
    assert escalation[0]['source_stage_result']['calls']==2
    assert executor.calls==2
    assert adapter.execute(todo,profile_id='p',attempt_index=1) is None
    assert executor.calls==2


def test_interrupted_responsibility_retains_evidence_and_falls_back(tmp_path,monkeypatch):
    adapter,todo,store,executor,_=configured(tmp_path,monkeypatch)
    adapter._event(store,'r',todo,'r:direct:'+todo.todo_id,'EXECUTION_POLICY',{
        'policy':{'mode':'DIRECT'},'assessment':{'source_targets':['source.py'],'test_targets':['tests/test_source.py']}})
    adapter._event(store,'r',todo,'r:direct:'+todo.todo_id,'EDIT_MODEL_STARTED',{'stage':'TEST'})
    assert adapter.execute(todo,profile_id='p',attempt_index=0) is None
    assert executor.calls==0
    assert any(e['payload'].get('typed_failure',{}).get('category')=='DIRECT_INTERRUPTED'
               for e in events(store) if e.get('evidence_type')=='DIRECT_EVENT')


def test_policy_unknown_multiple_source_files_never_calls_model(tmp_path,monkeypatch):
    adapter,todo,store,executor,_=configured(tmp_path,monkeypatch)
    (tmp_path/'another.py').write_text('value=1\n')
    assert adapter.execute(todo,profile_id='p',attempt_index=0) is None
    assert executor.calls==0
    policies=[e['payload']['policy'] for e in events(store) if e.get('payload',{}).get('kind')=='EXECUTION_POLICY']
    assert policies[-1]['mode']!=ExecutionModeV1.DIRECT.value


def test_explicit_high_risk_blocks_even_isolated_computation(tmp_path,monkeypatch):
    adapter,todo,store,executor,_=configured(tmp_path,monkeypatch)
    (tmp_path/'source.py').write_text('def f(x): return x + 1\n')
    todo.work_contract=replace(todo.work_contract,constraints={**todo.work_contract.constraints,'SCENARIO_RISK':'HIGH'})
    assert adapter.execute(todo,profile_id='p',attempt_index=1) is None
    assert executor.calls==0
    assessments=[e['payload']['assessment'] for e in events(store) if e.get('payload',{}).get('kind')=='EXECUTION_POLICY']
    assert assessments[-1]['risk']=='HIGH' and assessments[-1]['declared_risk']=='HIGH'


def test_repair_counts_rejected_aci_attempt_without_relaxing_preflight(tmp_path,monkeypatch):
    adapter,todo,store,executor,_=configured(tmp_path,monkeypatch)
    bad=native({'file':'source.py','start_line':1,'end_line':1,'replacement_lines':['value = )']})
    executor.values.insert(0,bad)
    result=adapter.execute(todo,profile_id='p',attempt_index=1)
    assert result.finalized and result.model_calls==3 and result.tool_calls==3
    applied=[e['payload'] for e in events(store) if e.get('payload',{}).get('kind')=='EDIT_APPLIED']
    assert len(applied)==3 and applied[0]['error_code']=='SYNTAX_PREFLIGHT_FAILED'
    assert applied[0]['changed_paths']==[]


def test_actual_simple_fallback_receives_summary_and_keeps_acceptance_external(tmp_path,monkeypatch):
    from test_simple_worker_v2 import action
    from test_simple_worker_v2_compact_edit import edit
    from harness.control_plane import RunStatus
    adapter,todo,store,executor,_=configured(tmp_path,monkeypatch)
    executor.values=[native(count=0),native(count=0),
        action('VIEW',{'path':'source.py'}),
        edit('value = 2',1,1,'source.py'),
        action('VIEW',{'path':'tests/test_source.py'}),
        edit('from source import value\ndef test_value():\n    assert value == 2',1,2,'tests/test_source.py'),
        action('FINISH',{},'source and regression coverage changed')]
    captured=[];original=executor.execute
    def execute(request):
        captured.append(request)
        return original(request)
    executor.execute=execute
    result=adapter.owner.execute(todo,profile_id='p',objective_with_context=todo.work_contract.objective,
                                 attempt_index=1,fresh_workspace=True)
    assert result.finalized and executor.calls==7
    assert store.get_run('r').quota_usage.worker_calls==7
    assert store.get_run('r').status is RunStatus.RUNNING
    context='\n'.join(message.content for message in captured[2].messages)
    assert 'DIRECT_ESCALATION_SUMMARY_V1' in context
    assert 'TRANSPORT_PARSE_FAIL' in context
    assert 'provisional source result is not acceptance' in context
    assert (tmp_path/'source.py').read_text()=='value = 2\n'


def test_escalation_projection_is_bounded_and_does_not_copy_chat(tmp_path,monkeypatch):
    from harness.integration.simple_worker_adapter import direct_escalation_context
    adapter,todo,store,_,_=configured(tmp_path,monkeypatch)
    adapter._event(store,'r',todo,'request','ESCALATION',{
        'objective':'objective','current_diff':'x'*50000,'raw_response_content':'PRIVATE_CHAT',
        'typed_failure':{'category':'DIRECT_FAILED','detail':'detail'},'next_mode':'SIMPLE'})
    text=direct_escalation_context(store,'r',todo.todo_id)
    assert len(text.encode('utf-8'))<16384 and 'PRIVATE_CHAT' not in text
    assert 'current_diff_truncated": true' in text
