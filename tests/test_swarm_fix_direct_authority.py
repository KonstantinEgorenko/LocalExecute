from dataclasses import replace
from types import SimpleNamespace as NS
import pytest
from test_production_direct_integration import configured
from harness.workflow.recovery_bridge import build_workflow_failure_evidence
from harness.workflow.models import VerificationObservationV1
from harness.verification.acceptance import fingerprint


def test_direct_result_supplies_production_replan_authority(tmp_path, monkeypatch):
    adapter,todo,store,executor,_=configured(tmp_path,monkeypatch)
    result=adapter.execute(todo,profile_id='p',attempt_index=1)
    workflow=NS(todo_id=todo.todo_id,run_id='r',work_contract=todo.work_contract,
                dependencies=(),routing_task_spec={})
    verification=VerificationObservationV1(True,'verify','f'*64,'pass',(),{})
    bundle=build_workflow_failure_evidence(todo=workflow,objective=todo.work_contract.objective,
        execution=result,verification=verification,failed_profile_ids=(),recovery_cycle=1)
    assert set(bundle.workspace_paths)=={'source.py','tests/test_source.py'}
    domain=result.raw['diagnosis_path_domain']
    assert set(domain['mutable_workspace_paths'])==set(bundle.workspace_paths)
    assert '.git' in domain['immutable_paths']
    assert result.raw['path_authority_provenance']['contract_fingerprint']==fingerprint(todo.work_contract.to_dict())


def test_direct_authority_metadata_rejects_wrong_parent_contract(tmp_path,monkeypatch):
    adapter,todo,*_=configured(tmp_path,monkeypatch)
    result=adapter.execute(todo,profile_id='p',attempt_index=1)
    wrong=replace(todo.work_contract,allowed_scope=('unrelated.py',))
    workflow=NS(todo_id=todo.todo_id,run_id='r',work_contract=wrong,dependencies=(),routing_task_spec={})
    with pytest.raises(ValueError,match='authority'):
        build_workflow_failure_evidence(todo=workflow,objective=wrong.objective,execution=result,
            verification=VerificationObservationV1(True,'verify','f'*64,'pass',(),{}),failed_profile_ids=(),recovery_cycle=1)


def test_missing_direct_authority_fails_before_recovery_dispatch(tmp_path,monkeypatch):
    adapter,todo,*_=configured(tmp_path,monkeypatch)
    execution=adapter.execute(todo,profile_id='p',attempt_index=1)
    raw={k:v for k,v in execution.raw.items() if k!='path_authority_provenance'}
    workflow=NS(todo_id=todo.todo_id,run_id='r',work_contract=todo.work_contract,dependencies=(),routing_task_spec={})
    with pytest.raises(ValueError,match='authority'):
        build_workflow_failure_evidence(todo=workflow,objective=todo.work_contract.objective,
            execution=replace(execution,raw=raw),verification=VerificationObservationV1(True,'verify','f'*64,'PASS',(),{}),
            failed_profile_ids=(),recovery_cycle=1)


def test_production_recovery_rejects_retired_direct_lease_before_replan(tmp_path):
    from test_replan_responsibility_lifecycle import setup
    from harness.integration.workflow_production_adapters import WorkflowRecoveryAdapter
    from harness.workflow.models import ExecutionObservationV1
    w,todo,_=setup(tmp_path);run,lid=w.ensure(todo)
    calls=[]
    raw={'run_id':run,'lease_id':'expired','execution_mode':'DIRECT',
         'path_authority_provenance':{'run_id':run,'lease_id':'expired','todo_id':todo.todo_id,
                                      'contract_fingerprint':fingerprint(todo.work_contract.to_dict())}}
    outcome=WorkflowRecoveryAdapter(bridge=NS(recover=lambda *a,**kw:calls.append('replan')),
        critic=NS(review=lambda **kw:calls.append('critic')),workspace=w).recover(todo,
        objective=todo.objective,execution=ExecutionObservationV1('p',True,(),2,2,'SUCCESS','done',raw),
        verification=VerificationObservationV1(True,'verify','f'*64,'pass',(),{}),failed_profile_ids=(),recovery_cycle=1)
    assert not outcome.continue_work and outcome.raw['cause_code']=='PATH_AUTHORITY_MISMATCH'
    assert calls==[]


def test_direct_to_real_recovery_replan_keeps_both_authorized_paths(tmp_path,monkeypatch):
    from pathlib import Path
    from harness.workflow.recovery_bridge import WorkflowRecoveryBridge
    from harness.recovery.models import FailureDiagnosisV2,FailureKind,ReplanContractV2
    from harness.evaluation.models import BenchmarkTaskClass,BenchmarkDifficulty
    from harness.router.policy_models import NavigationNeed, AmbiguityLevel
    adapter,item,*_=configured(tmp_path,monkeypatch)
    execution=adapter.execute(item,profile_id='p',attempt_index=1)
    workflow=NS(todo_id=item.todo_id,run_id='r',work_contract=item.work_contract,
                dependencies=(),routing_task_spec={},objective=item.work_contract.objective)
    bridge=WorkflowRecoveryBridge(Path(__file__).resolve().parents[1]);seen=[]
    diagnosis=FailureDiagnosisV2(FailureKind.CONTEXT_INSUFFICIENT,.9,'Review requires a revised deliverable.',
        ('e1',),('source.py',),(),False,True)
    plan=ReplanContractV2(item.todo_id,'Update implementation and regression coverage.',
        BenchmarkTaskClass.SINGLE_FILE_BUG,BenchmarkDifficulty.NORMAL,
        ('source.py','tests/test_source.py'),NavigationNeed.LOW,4096,0,
        AmbiguityLevel.LOW,AmbiguityLevel.LOW,('e1',),(),True,True,.9)
    def diagnose(bundle):
        seen.append(bundle)
        return NS(diagnosis=diagnosis,model_id='fake',model_tag='fake',model_calls=1,latency_ms=1)
    def replan(bundle,observed):
        assert observed==diagnosis and set(bundle.workspace_paths)=={'source.py','tests/test_source.py'}
        return NS(plan=plan,model_id='fake',model_tag='fake',model_calls=1,latency_ms=1)
    bridge.diagnosis_client=NS(diagnose=diagnose);bridge.replan_client=NS(replan=replan)
    result=bridge.recover(workflow,objective=workflow.objective,execution=execution,
        verification=VerificationObservationV1(True,'verify','f'*64,'pass',(),{}),
        failed_profile_ids=(),recovery_cycle=1,
        review_replan={'kind':'REVIEW_REPLAN','judge_report_id':'judge-current'})
    assert result.raw['controller_action']=='ORNITH_REPLAN'
    assert result.revised_objective==plan.revised_objective and len(seen)==1
    assert (tmp_path/'source.py').read_text()=='value = 2\n'
