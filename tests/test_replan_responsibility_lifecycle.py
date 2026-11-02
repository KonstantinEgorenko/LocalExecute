"""Real Git/RunStore amendment lifecycle; no model endpoint."""
from dataclasses import replace
import json
import subprocess
from types import SimpleNamespace

import pytest

from harness.control_plane import Goal, GoalStatus, Todo, TodoStatus, RunStatus
from harness.control_plane.store import FileRunStore
from harness.integration.acceptance_bridge import load_frozen_acceptance
from harness.integration.execution_responsibility import (
    persisted_responsibility, evidence_payloads, StaleResponsibilityError,
)
from harness.integration.workflow_production_adapters import ProductionWorkspaceCoordinator
from harness.scheduler import FileSchedulerStore, SchedulerTodoState, SchedulerTodoStatus
from tests.test_production_workflow_verification_recovery_boundary import _setup_workspace, _workflow_todo


def setup(tmp_path):
    target, manager, _, artifacts, _, workspace = _setup_workspace(tmp_path)
    (target / 'base.py').write_text('def f(x):\n    """Return input plus one."""\n    return x - 1\n')
    (target / 'test_base.py').write_text('from base import f\ndef test_public():\n    assert f(0) == 1\n')
    (target / 'README.md').write_text('Public contract: f returns input plus one.\n')
    for arguments in (('add', 'base.py', 'test_base.py', 'README.md'), ('commit', '-m', 'public fixture')):
        subprocess.run(['git', '-C', str(target), *arguments], check=True, capture_output=True)
    store = FileRunStore(tmp_path / 'runs')
    workspace.run_store = store
    todo = _workflow_todo(run_id='run-01')
    contract = replace(todo.work_contract, objective='Update implementation and add regression tests.',
                       constraints={'REQUIRES_REGRESSION_CHANGE': True}, allowed_scope=())
    todo = replace(todo, objective=contract.objective, work_contract=contract)
    store.create_goal(Goal('goal', 'goal', GoalStatus.ACTIVE, 'test'))
    store.create_todo(Todo(todo.todo_id, 'goal', todo.objective, TodoStatus.READY))
    scheduler = FileSchedulerStore(tmp_path / 'scheduler')
    scheduler.register((SchedulerTodoState(todo.todo_id, 'goal', todo.run_id, todo.objective, (), 0,
                                         status=SchedulerTodoStatus.REPLAN_REQUIRED),))
    workspace.scheduler_store = scheduler
    return workspace, todo, scheduler


def amend(todo, scheduler, objective='Update implementation. Tests are immutable and must not be modified.'):
    scheduler.revise_replan_required(todo_id=todo.todo_id, objective=objective, amendment_id='amendment-1')
    return replace(todo, objective=objective, work_contract=replace(todo.work_contract, objective=objective))


def restart(workspace):
    restored = ProductionWorkspaceCoordinator(run_store=FileRunStore(workspace.run_store.root),
        manager=workspace.manager, source_repo_root=workspace.source_repo_root, spine=workspace.spine,
        artifact_store=workspace.artifact_store)
    restored.scheduler_store = FileSchedulerStore(workspace.scheduler_store.root)
    return restored


def test_same_responsibility_resumes_without_duplicate_run_or_lease(tmp_path):
    w, todo, _ = setup(tmp_path)
    pair = w.ensure(todo)
    root = load_frozen_acceptance(w.run_store, pair[0], todo.work_contract)
    assert w.ensure(todo) == pair
    assert restart(w).ensure(todo) == pair
    assert len(w.run_store.list_runs()) == 1
    assert len(w.manager.reconcile()) == 1
    assert root == load_frozen_acceptance(w.run_store, pair[0], todo.work_contract)


def test_revision_invalidates_precreated_run_and_inherits_immutable_root(tmp_path):
    w, todo, scheduler = setup(tmp_path)
    old, old_lease = w.ensure(todo)
    original = load_frozen_acceptance(w.run_store, old, todo.work_contract)
    assert not original.unresolved_semantics
    assert any(e.provenance.value == 'PUBLIC_CONTRACT_EVIDENCE' for e in original.evidence)
    assert any(e.provenance.value == 'PREEXISTING_TEST_EVIDENCE' for e in original.evidence)
    old_payload = json.dumps(evidence_payloads(w.run_store, old, 'ACCEPTANCE_OBLIGATIONS_V1'), sort_keys=True)
    precreated, precreated_lease = w.rotate(todo)
    revised = amend(todo, scheduler)
    new, new_lease = w.ensure(revised)
    assert new not in {old, precreated}
    assert new_lease not in {old_lease, precreated_lease}
    assert w.run_store.get_run(old).status is RunStatus.CANCELLED
    assert w.run_store.get_run(precreated).status is RunStatus.CANCELLED
    assert [l.lease_id for l in w.manager.reconcile()] == [new_lease]
    assert old_payload == json.dumps(evidence_payloads(w.run_store, old, 'ACCEPTANCE_OBLIGATIONS_V1'), sort_keys=True)
    assert load_frozen_acceptance(w.run_store, new, revised.work_contract) == original
    assert not evidence_payloads(w.run_store, new, 'ACCEPTANCE_OBLIGATIONS_V1')
    bound = evidence_payloads(w.run_store, new, 'EXECUTION_ACCEPTANCE_BINDING_V1')[0]
    assert bound['execution_guidance_provenance'] == 'MODEL_GENERATED'
    assert bound['root_run_id'] == old
    assert bound['contradictions'] == ['REPLAN_GUIDANCE_CONFLICTS_WITH_REQUIRES_REGRESSION_CHANGE']
    assert persisted_responsibility(w.run_store, new).source_amendment_id == 'amendment-1'
    assert persisted_responsibility(w.run_store, new).amendment_version == 1
    with pytest.raises(StaleResponsibilityError, match='STALE_RESPONSIBILITY'):
        w.resolve(run_id=precreated, contract=revised.work_contract)
    assert w.run_store.get_run(new).quota_usage.worker_calls == 0


def test_duplicate_amendment_and_restart_do_not_duplicate_binding(tmp_path):
    w, todo, scheduler = setup(tmp_path)
    w.ensure(todo)
    revised = amend(todo, scheduler)
    pair = w.ensure(revised)
    fresh = restart(w)
    before = fresh.scheduler_store.get_todo(todo.todo_id)
    fresh.scheduler_store.revise_replan_required(todo_id=todo.todo_id, objective=revised.objective,
                                                amendment_id='amendment-1')
    assert fresh.scheduler_store.get_todo(todo.todo_id) == before
    assert fresh.ensure(revised) == pair
    assert len(fresh.run_store.list_runs()) == 2
    assert len(evidence_payloads(fresh.run_store, pair[0], 'EXECUTION_ACCEPTANCE_BINDING_V1')) == 1


def test_claim_versions_do_not_create_another_responsibility(tmp_path):
    w, todo, scheduler = setup(tmp_path)
    w.ensure(todo)
    revised = amend(todo, scheduler)
    pair = w.ensure(revised)
    # Claim lifecycle versions advance independently of the applied amendment.
    with scheduler._locked() as raw:
        state = scheduler.get_todo(todo.todo_id)
        raw['todos'][todo.todo_id] = replace(state, version=state.version + 2).to_dict()
    assert restart(w).ensure(revised) == pair
    assert persisted_responsibility(w.run_store, pair[0]).amendment_version == 1


def test_stale_resume_before_new_admission_is_rejected(tmp_path):
    w, todo, scheduler = setup(tmp_path)
    old, _ = w.ensure(todo)
    revised = amend(todo, scheduler)
    with pytest.raises(StaleResponsibilityError):
        w.current(todo.todo_id)
    for contract in (todo.work_contract, revised.work_contract):
        with pytest.raises(StaleResponsibilityError, match='STALE_RESPONSIBILITY'):
            w.resolve(run_id=old, contract=contract)
    assert w.run_store.get_run(old).quota_usage.worker_calls == 0


def test_cleanup_failure_remains_owned_and_restart_retries(tmp_path, monkeypatch):
    w, todo, scheduler = setup(tmp_path)
    old, lease = w.ensure(todo)
    revised = amend(todo, scheduler)
    original_release = w.manager.release
    with monkeypatch.context() as m:
        m.setattr(w.manager, 'release', lambda *a, **kw: (_ for _ in ()).throw(PermissionError('held')))
        with pytest.raises(PermissionError):
            w.ensure(revised)
        assert len(w.run_store.list_runs()) == 1
        assert w.manager.get_active(lease)
        with pytest.raises(StaleResponsibilityError):
            w.resolve(run_id=old, contract=revised.work_contract)
    assert original_release
    fresh = restart(w)
    assert fresh.ensure(revised)[0] != old
    assert len(fresh.manager.reconcile()) == 1


def test_acquire_failure_reuses_pending_revised_run(tmp_path, monkeypatch):
    w, todo, scheduler = setup(tmp_path)
    w.ensure(todo)
    revised = amend(todo, scheduler)
    with monkeypatch.context() as m:
        m.setattr(w.manager, 'acquire', lambda **kw: (_ for _ in ()).throw(OSError('acquire failed')))
        with pytest.raises(OSError):
            w.ensure(revised)
    pending = [r for r in w.run_store.list_runs() if r.status is RunStatus.RUNNING]
    assert len(pending) == 1
    fresh = restart(w)
    assert fresh.ensure(revised)[0] == pending[0].run_id
    assert len(fresh.run_store.list_runs()) == 2


def test_root_constraints_cannot_be_erased(tmp_path):
    w, todo, scheduler = setup(tmp_path)
    w.ensure(todo)
    revised = amend(todo, scheduler)
    revised = replace(revised, work_contract=replace(revised.work_contract, constraints={}))
    with pytest.raises(ValueError, match='structured root constraints'):
        w.ensure(revised)


def test_wrong_current_contract_does_not_bypass_frozen_guard(tmp_path):
    w, todo, scheduler = setup(tmp_path)
    old, _ = w.ensure(todo)
    revised = amend(todo, scheduler)
    with pytest.raises(ValueError, match='frozen acceptance responsibility mismatch'):
        load_frozen_acceptance(w.run_store, old, revised.work_contract)
    new, _ = w.ensure(revised)
    with pytest.raises(ValueError, match='lineage responsibility mismatch'):
        load_frozen_acceptance(w.run_store, new, todo.work_contract)


def test_amendment_identity_changes_generation_even_for_same_objective(tmp_path):
    w, todo, scheduler = setup(tmp_path)
    old, _ = w.ensure(todo)
    revised = amend(todo, scheduler, objective=todo.objective)
    new, _ = w.ensure(revised)
    assert new != old
    assert w.run_store.get_run(old).status is RunStatus.CANCELLED
    assert load_frozen_acceptance(w.run_store, new, todo.work_contract).run_id == old


def test_binding_root_digest_tamper_is_rejected(tmp_path):
    w, todo, scheduler = setup(tmp_path)
    w.ensure(todo)
    revised = amend(todo, scheduler)
    new, _ = w.ensure(revised)
    # Inject inconsistent durable evidence through the same ledger; never silently
    # choose one of two competing lineage records.
    from harness.control_plane import Evidence, EvidenceStatus
    payload = evidence_payloads(w.run_store, new, 'EXECUTION_ACCEPTANCE_BINDING_V1')[0]
    payload['root_acceptance_fingerprint'] = '0' * 64
    w.run_store.record_evidence(new, Evidence('tamper', todo.todo_id,
        'EXECUTION_ACCEPTANCE_BINDING_V1', 'test', EvidenceStatus.PASSED, payload))
    with pytest.raises(ValueError, match='lineage responsibility mismatch'):
        load_frozen_acceptance(w.run_store, new, revised.work_contract)


def test_revised_trace_is_correlated_after_cold_restart(tmp_path):
    from harness.integration.mission_trace_observer import MissionTraceRegistry
    w, todo, scheduler = setup(tmp_path)
    w.trace_registry = MissionTraceRegistry(tmp_path / 'trace-state')
    w.trace_registry.get('mission', 'goal')
    old, _ = w.ensure(todo)
    revised = amend(todo, scheduler)
    fresh = restart(w)
    fresh.trace_registry = MissionTraceRegistry(tmp_path / 'trace-state')
    fresh.trace_registry.get('mission', 'goal')
    new, _ = fresh.ensure(revised)
    assert new != old
    assert fresh.trace_registry.correlation_for_run(new).todo_id == todo.todo_id
    trace = next((tmp_path / 'trace-state').rglob('mission_trace.jsonl'))
    events = [json.loads(line) for line in trace.read_text().splitlines()]
    assert any(e['event_type'] == 'STALE_RUN_SUPERSEDED' and e['run_id'] == old for e in events)
    assert any(e['event_type'] == 'ACCEPTANCE_LINEAGE_BOUND' and e['run_id'] == new for e in events)
    resumed = restart(fresh)
    resumed.trace_registry = MissionTraceRegistry(tmp_path / 'trace-state')
    resumed.trace_registry.get('mission', 'goal')
    assert resumed.ensure(revised)[0] == new
    assert resumed.trace_registry.correlation_for_run(new).todo_id == todo.todo_id


def test_historical_single_amendment_uses_identity_without_guessing_claim_version(tmp_path):
    w, todo, scheduler = setup(tmp_path)
    w.ensure(todo)
    revised = amend(todo, scheduler)
    with scheduler._locked() as raw:
        raw.pop('responsibility_revisions')
    new, _ = w.ensure(revised)
    assert persisted_responsibility(w.run_store, new).amendment_version is None
    assert persisted_responsibility(w.run_store, new).source_amendment_id == 'amendment-1'
    assert restart(w).ensure(revised)[0] == new


@pytest.mark.parametrize('version', [True, None, 0])
def test_malformed_amendment_revision_fails_before_candidate_admission(tmp_path, version):
    w, todo, scheduler = setup(tmp_path)
    old, _ = w.ensure(todo)
    revised = amend(todo, scheduler)
    with scheduler._locked() as raw:
        raw['responsibility_revisions'][todo.todo_id]['version'] = version
    from harness.scheduler.errors import SchedulerStateError
    with pytest.raises(SchedulerStateError, match='malformed responsibility revision'):
        w.ensure(revised)
    assert len(w.run_store.list_runs()) == 1
    assert w.run_store.get_run(old).quota_usage.worker_calls == 0


def test_worker_context_and_slice_quota_do_not_replace_canonical_authorization(tmp_path):
    from harness.agent_loop.coordinator import ControlPlaneCodingAgent
    from harness.context_manager import ContextBudget
    w, todo, scheduler = setup(tmp_path)
    w.ensure(todo)
    revised = amend(todo, scheduler)
    new, lease = w.ensure(revised)
    decorated = replace(revised.work_contract, objective=revised.objective + '\nRecovery context',
                        quota=replace(revised.work_contract.quota, max_worker_calls=3))
    observed = []
    agent = ControlPlaneCodingAgent(context_resolver=SimpleNamespace(assemble=lambda *a, **kw: object()),
        workspace_resolver=w, loop=SimpleNamespace(execute=lambda **kw: observed.append(kw)))
    agent.execute(run_id=new, contract=decorated, workspace_contract=revised.work_contract,
                  routing_spec=object(), budget=ContextBudget(4096, 20, 100000))
    assert observed[0]['contract'] == decorated
    assert observed[0]['workspace_session'].root == w.manager.get_active(lease).worktree_root
    assert load_frozen_acceptance(w.run_store, new, revised.work_contract).run_id != new
    # The same explicit seam cannot use original responsibility A after amendment B.
    with pytest.raises(StaleResponsibilityError):
        agent.execute(run_id=new, contract=decorated, workspace_contract=todo.work_contract,
                      routing_spec=object(), budget=ContextBudget(4096, 20, 100000))
    assert len(observed) == 1
    with pytest.raises(ValueError, match='cannot change workspace authorization'):
        agent.execute(run_id=new, contract=replace(decorated, constraints={}),
                      workspace_contract=revised.work_contract, routing_spec=object(),
                      budget=ContextBudget(4096, 20, 100000))
    assert len(observed) == 1
