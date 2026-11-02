from dataclasses import replace
from types import SimpleNamespace as NS
import pytest
from test_replan_responsibility_lifecycle import setup
from harness.integration.workflow_production_adapters import WorkflowReviewAdapter,WorkflowRecoveryAdapter
from harness.workflow.models import ExecutionObservationV1,VerificationObservationV1

@pytest.mark.parametrize('task',['authority_navigation','atomic_csv_import'])
def test_missing_public_topology_blocks_without_futile_model_replan(tmp_path,task):
    w,todo,_=setup(tmp_path)
    w.acceptance_interpreter=lambda f:replace(f,unresolved_semantics=('PUBLIC_INTERPRETER_TOPOLOGY_UNSUPPORTED',))
    run,lid=w.ensure(todo)
    calls=[]
    from harness.judge import JudgeDecision
    def critic_call(**kw):
        calls.append('critic'); return NS(report=NS(report_id='critic',to_dict=lambda:{}))
    def judge_call(**kw):
        calls.append('judge'); return NS(report=NS(report_id='judge',to_dict=lambda:{},analysis=NS(decision=JudgeDecision.REPLAN,summary='missing verification capability',evidence=())))
    critic=NS(review=critic_call)
    judge=NS(review=judge_call)
    execution=ExecutionObservationV1('p',True,('base.py',),2,2,'SUCCESS',task,{'run_id':run,'lease_id':lid})
    verify=VerificationObservationV1(True,'verification','f'*64,'PASS',(),{})
    review=WorkflowReviewAdapter(critic=critic,judge=judge,workspace=w).review(todo,execution,verify)
    assert review.decision=='UNSUPPORTED_REQUIRED_OBLIGATION' and not review.accept
    recovery=WorkflowRecoveryAdapter(bridge=NS(),critic=critic,workspace=w).recover(todo,
        objective=todo.objective,execution=execution,verification=verify,failed_profile_ids=(),recovery_cycle=1)
    assert not recovery.continue_work
    assert recovery.raw['failure_kind']=='UNSUPPORTED_REQUIRED_OBLIGATION'
    assert recovery.raw['model_incorrectness_proven'] is False
    assert calls==[]

