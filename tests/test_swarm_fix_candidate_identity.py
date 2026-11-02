from pathlib import Path
from types import SimpleNamespace as NS
from test_worktree_manager import manager,git
from harness.verification.runner import VerificationRunner


def snapshots(tmp_path):
    m,_,source=manager(tmp_path)
    git(source,'config','core.autocrlf','true')
    lease=m.acquire(source_repo_root=source,run_id='r',todo_id='t')
    f=Path(lease.worktree_root)/'target.txt'
    f.write_bytes(b'CHANGED\n'); before=m.snapshot(lease.lease_id)
    f.write_bytes(b'CHANGED\r\n'); after=m.snapshot(lease.lease_id)
    assert before.diff_sha256==after.diff_sha256
    return before,after


def test_exact_candidate_bytes_are_distinct_even_when_git_diff_normalizes_eol(tmp_path):
    before,after=snapshots(tmp_path)
    assert getattr(before,'content_sha256',None)!=getattr(after,'content_sha256',None)


def test_verification_report_identity_invalidates_on_eol_only_mutation(tmp_path):
    before,after=snapshots(tmp_path)
    plan=NS(run_id='r',todo_id='t',lease_id=before.lease_id,fingerprint='a'*64)
    assert VerificationRunner.report_id(plan,before)!=VerificationRunner.report_id(plan,after)

from dataclasses import replace
import pytest
from test_replan_responsibility_lifecycle import setup as workspace_setup
from harness.integration.workflow_production_adapters import WorkflowReviewAdapter
from harness.workflow.models import ExecutionObservationV1,VerificationObservationV1
from harness.judge import JudgeDecision
from harness.verification.errors import VerificationStateError
from harness.integration.execution_responsibility import evidence_payloads


def accepted_workspace(tmp_path):
    w,todo,_=workspace_setup(tmp_path);run,lid=w.ensure(todo)
    root=Path(w.manager.get_active(lid).worktree_root)
    (root/'base.py').write_bytes(b'def f(x):\n    return x + 1\n')
    critic=NS(review=lambda **kw:NS(report=NS(report_id='critic',to_dict=lambda:{})))
    judge=NS(review=lambda **kw:NS(report=NS(report_id='judge',to_dict=lambda:{},
        analysis=NS(decision=JudgeDecision.ACCEPT,summary='offline acceptance',evidence=()))))
    result=WorkflowReviewAdapter(critic=critic,judge=judge,workspace=w).review(todo,
        ExecutionObservationV1('p',True,('base.py',),2,2,'SUCCESS','done',{}),
        VerificationObservationV1(True,'verification','a'*64,'PASS',(),{}))
    assert result.accept
    return w,todo,run,root


def test_accepted_export_matches_judged_raw_manifest_and_replays_without_mutation(tmp_path):
    w,todo,run,root=accepted_workspace(tmp_path)
    snap=w.commit_current(goal_id=todo.work_contract.goal_id,epoch=1)
    exported=evidence_payloads(w.run_store,run,'ACCEPTED_EXPORT_V1')[0]
    bound=evidence_payloads(w.run_store,run,'ACCEPTED_CANDIDATE_AUTHORITY_V1')[0]
    assert exported['content_sha256']==bound['content_sha256']
    assert exported['accepted_head']==snap.head_commit
    assert w.commit_current(goal_id=todo.work_contract.goal_id,epoch=1)==snap


def test_mutation_after_judge_cannot_export_under_old_acceptance(tmp_path):
    w,todo,run,root=accepted_workspace(tmp_path)
    (root/'base.py').write_bytes(b'def f(x):\r\n    return x + 1\r\n')
    with pytest.raises(VerificationStateError,match='judged raw candidate'):
        w.commit_current(goal_id=todo.work_contract.goal_id,epoch=1)
    assert not evidence_payloads(w.run_store,run,'ACCEPTED_EXPORT_V1')
