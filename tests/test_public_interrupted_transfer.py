"""Fault injection at the actual production write/receipt boundary; no LLM."""
from pathlib import Path
import base64
import pytest
from tests.test_replan_responsibility_lifecycle import setup, amend, restart
from harness.integration.execution_responsibility import evidence_payloads


def test_partial_draft_is_blocked_without_replay_and_sealed_bytes_survive(tmp_path, monkeypatch):
    w,todo,scheduler=setup(tmp_path)
    parent,lid=w.ensure(todo)
    source=Path(w.manager.get_active(lid).worktree_root)
    expected={'base.py':b'def f(x):\n    return x + 1\n',
              'test_new.py':b'from base import f\ndef test_new():\n    assert f(3)==4\n'}
    for name,data in expected.items():(source/name).write_bytes(data)
    revised=amend(todo,scheduler,'Update implementation and preserve regression coverage.')
    class Crash(BaseException):pass
    original=Path.write_bytes;writes=[]
    def interrupted(path,data):
        result=original(path,data)
        if path.name=='base.py' and path!=source/'base.py':
            writes.append(str(path));raise Crash('AFTER_FIRST_WRITE_BEFORE_FINAL_RECEIPT')
        return result
    with monkeypatch.context() as m:
        m.setattr(Path,'write_bytes',interrupted)
        with pytest.raises(Crash):w.ensure(revised)
    assert len(writes)==1
    child_run,child_lid=w._current[todo.todo_id]
    child=Path(w.manager.get_active(child_lid).worktree_root)
    before=(child/'base.py').read_bytes()
    assert before==expected['base.py'] and not (child/'test_new.py').exists()
    assert evidence_payloads(w.run_store,child_run,'REPLAN_DRAFT_STARTED_V1')
    assert not evidence_payloads(w.run_store,child_run,'REPLAN_DRAFT_APPLIED_V1')
    for _ in range(2):
        with pytest.raises(ValueError,match='destination conflict'):
            restart(w).ensure(revised)
    assert (child/'base.py').read_bytes()==before and not (child/'test_new.py').exists()
    assert not evidence_payloads(w.run_store,child_run,'VERIFICATION_REPORT')
    assert not evidence_payloads(w.run_store,child_run,'ACCEPTED_EXPORT_V1')
    # Independent Python invocation reconstructs the production stores/manager.
    import json, subprocess, sys
    from dataclasses import asdict
    meta={'source':str(w.source_repo_root),'runs':str(w.run_store.root),
          'managed':str(w.manager.managed_root),'leases':str(w.manager.store.root),
          'scheduler':str(w.scheduler_store.root),'policy':asdict(w.manager.policy),
          'todo':dict(todo_id=revised.todo_id,run_id=revised.run_id,objective=revised.objective,
                      dependencies=list(revised.dependencies),work_contract=revised.work_contract.to_dict(),
                      routing_task_spec=revised.routing_task_spec),
          'protected':[str(path) for path in w.manager.guard.protected_roots]}
    script='''
import json,sys
from harness.control_plane import WorkContract,Quota,FileRunStore
from harness.worktree import GitWorktreeManager,WorktreePolicy
from harness.scheduler import FileSchedulerStore
from harness.workflow.models import WorkflowTodoV1
from harness.integration.workflow_production_adapters import ProductionWorkspaceCoordinator
m=json.load(sys.stdin);t=m['todo'];c=t['work_contract'];c['quota']=Quota(**c['quota']);t['work_contract']=WorkContract(**c)
w=ProductionWorkspaceCoordinator(run_store=FileRunStore(m['runs']),manager=GitWorktreeManager(protected_repo_roots=tuple(m['protected']),managed_root=m['managed'],state_root=m['leases'],policy=WorktreePolicy(**m['policy'])),source_repo_root=m['source'],spine=None)
w.scheduler_store=FileSchedulerStore(m['scheduler'])
try:w.ensure(WorkflowTodoV1(**t))
except ValueError as e:
 assert 'destination conflict' in str(e),str(e)
 print('COLD_PROCESS_BLOCKED_WITHOUT_REPLAY')
else:raise AssertionError('PARTIAL_TRANSFER_ACCEPTED')
'''
    cold=subprocess.run([sys.executable,'-c',script],input=json.dumps(meta),capture_output=True,text=True,timeout=45)
    assert cold.returncode==0,(cold.stdout,cold.stderr)
    assert 'COLD_PROCESS_BLOCKED_WITHOUT_REPLAY' in cold.stdout
    assert (child/'base.py').read_bytes()==before and not (child/'test_new.py').exists()
    rows=[e.to_dict()['payload']['payload'] for e in w.run_store.list_events(parent)
          if e.to_dict()['payload'].get('evidence_type','').startswith('REPLAN_DRAFT:')]
    assert len(rows)==1
    assert {f['path']:base64.b64decode(f['content_base64']) for f in rows[0]['files']}==expected

