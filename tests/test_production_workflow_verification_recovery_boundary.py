"""Tests for Phase 16 verification-recovery boundary handling of zero-delta candidates."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
import pytest

from harness.context_manager import InMemoryArtifactStore
from harness.control_plane import Goal, GoalStatus, Quota, QuotaUsage, Run, RunStatus, Todo, TodoStatus, WorkContract
from harness.control_plane.store import InMemoryRunStore
from harness.integration.failure_policy import classify_failure, FailureKindV1
from harness.integration.workflow_production_adapters import (
    WorkflowVerificationAdapter,
    ProductionWorkspaceCoordinator,
)
from harness.workflow.models import ExecutionObservationV1, WorkflowTodoV1
from harness.workflow.recovery_bridge import build_workflow_failure_evidence
from harness.verification.coordinator import VerificationCoordinator
from harness.verification.errors import VerificationStateError
from harness.verification.models import VerificationCoordinatorResult, VerificationOutcome, VerificationReport
from harness.verification.policy import VerificationPolicy
from harness.worktree.manager import GitWorktreeManager
from harness.worktree.models import WorktreeLease, WorktreeSnapshot


def _init_repo(path: Path) -> Path:
    import subprocess
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(path), "init"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    (path / "base.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-m", "base"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return path


def _setup_workspace(tmp_path: Path):
    control = _init_repo(tmp_path / "control")
    target = _init_repo(tmp_path / "target")
    manager = GitWorktreeManager(
        protected_repo_roots=(control,),
        managed_root=tmp_path / "managed",
        state_root=tmp_path / "worktree-state",
    )
    runs = InMemoryRunStore()
    artifacts = InMemoryArtifactStore()
    spine = SimpleNamespace(latest_project_snapshot=lambda goal_id: (_fake_snapshot(), "evidence:1"))
    coordinator = VerificationCoordinator(
        run_store=runs,
        artifact_store=artifacts,
        manager=manager,
        policy=VerificationPolicy(require_candidate_dirty=True),
    )
    workspace = ProductionWorkspaceCoordinator(
        manager=manager,
        run_store=runs,
        source_repo_root=target,
        spine=spine,
        artifact_store=artifacts,
    )
    return target, manager, runs, artifacts, coordinator, workspace


def _fake_snapshot():
    return SimpleNamespace(head_commit="HEAD")


def _contract(todo_id: str = "todo:01") -> WorkContract:
    return WorkContract("goal", todo_id, "obj", {}, ("*",), (), (), Quota(6, 2, 2, 2, 3))


def _workflow_todo(todo_id: str = "todo:01", run_id: str = "run:01") -> WorkflowTodoV1:
    return WorkflowTodoV1(
        todo_id=todo_id,
        run_id=run_id,
        objective="test objective",
        dependencies=(),
        work_contract=_contract(todo_id),
        routing_task_spec=None,
    )


# TEST A: Exact v23 zero delta case with clean candidate
def test_v23_exact_zero_delta_case(tmp_path: Path):
    target, manager, runs, artifacts, coordinator, workspace = _setup_workspace(tmp_path)
    todo = _workflow_todo("todo:01", "run:01")
    runs.create_goal(Goal("goal", "goal", GoalStatus.ACTIVE, "test"))
    runs.create_todo(Todo("todo:01", "goal", "obj", TodoStatus.READY, required_gates=("python_syntax",)))
    run_id, lease_id = workspace.ensure(todo)

    adapter = WorkflowVerificationAdapter(coordinator=coordinator, workspace=workspace)
    execution = ExecutionObservationV1(
        profile_id="qwen36_heavy_quality",
        finalized=True,
        changed_paths=(),
        model_calls=4,
        tool_calls=3,
        outcome="SUCCESS",
        summary="TaskStore extended with priority/due_date fields...",
        raw={"agent_result": {"outcome": "SUCCESS", "tool_calls": 3}},
    )

    observation = adapter.verify(todo, execution)
    assert observation.passed is False
    assert observation.raw["kind"] == "CANDIDATE_NOT_DIRTY"
    assert observation.raw["candidate_dirty"] is False
    assert observation.raw["execution_outcome"] == "SUCCESS"
    assert "candidate worktree contains no modifications" in observation.summary


# TEST B: Clean candidate + WorkContract identity mismatch MUST still raise VerificationStateError
def test_clean_candidate_with_contract_mismatch_fails_closed(tmp_path: Path):
    target, manager, runs, artifacts, coordinator, workspace = _setup_workspace(tmp_path)
    todo = _workflow_todo("todo:01", "run:01")
    runs.create_goal(Goal("goal", "goal", GoalStatus.ACTIVE, "test"))
    runs.create_todo(Todo("todo:01", "goal", "obj", TodoStatus.READY, required_gates=("python_syntax",)))
    run_id, lease_id = workspace.ensure(todo)

    # Candidate remains completely clean!
    snapshot = manager.snapshot(lease_id)
    assert not snapshot.dirty

    # Mismatched contract.todo_id != run.active_todo_id
    mismatched_todo = WorkflowTodoV1(
        todo_id="todo:01",
        run_id="run:01",
        objective="test objective",
        dependencies=(),
        work_contract=_contract("todo:02"),
        routing_task_spec=None,
    )
    adapter = WorkflowVerificationAdapter(coordinator=coordinator, workspace=workspace)
    execution = ExecutionObservationV1(
        profile_id="qwen36_heavy_quality",
        finalized=True,
        changed_paths=(),
        model_calls=1,
        tool_calls=0,
        outcome="SUCCESS",
        summary="clean but contract mismatch",
        raw={},
    )

    with pytest.raises(VerificationStateError, match="Run and WorkContract identity mismatch"):
        adapter.verify(mismatched_todo, execution)


# TEST C: Clean candidate + lease identity mismatch MUST still raise VerificationStateError
def test_clean_candidate_with_lease_mismatch_fails_closed(tmp_path: Path):
    target, manager, runs, artifacts, coordinator, workspace = _setup_workspace(tmp_path)
    todo = _workflow_todo("todo:01", "run:01")
    runs.create_goal(Goal("goal", "goal", GoalStatus.ACTIVE, "test"))
    runs.create_todo(Todo("todo:01", "goal", "obj", TodoStatus.READY, required_gates=("python_syntax",)))
    run_id, lease_id = workspace.ensure(todo)

    # Create another todo and lease
    todo_other = _workflow_todo("todo:02", "run:02")
    runs.create_todo(Todo("todo:02", "goal", "obj", TodoStatus.READY, required_gates=("python_syntax",)))
    run_id_2, lease_id_2 = workspace.ensure(todo_other)

    # Put lease_id_2 into todo:01 current mapping
    workspace._current["todo:01"] = (run_id, lease_id_2)

    adapter = WorkflowVerificationAdapter(coordinator=coordinator, workspace=workspace)
    execution = ExecutionObservationV1(
        profile_id="qwen36_heavy_quality",
        finalized=True,
        changed_paths=(),
        model_calls=1,
        tool_calls=0,
        outcome="SUCCESS",
        summary="clean but lease mismatch",
        raw={},
    )

    with pytest.raises(VerificationStateError, match="worktree lease does not belong to Run/Todo"):
        adapter.verify(todo, execution)


# TEST D: Dirty candidate executes normal coordinator verification path
def test_candidate_dirty_runs_normal_verification(tmp_path: Path):
    target, manager, runs, artifacts, coordinator, workspace = _setup_workspace(tmp_path)
    todo = _workflow_todo("todo:01", "run:01")
    runs.create_goal(Goal("goal", "goal", GoalStatus.ACTIVE, "test"))
    runs.create_todo(Todo("todo:01", "goal", "obj", TodoStatus.READY, required_gates=("python_syntax",)))
    run_id, lease_id = workspace.ensure(todo)

    # Mutate candidate physically
    lease_obj = manager.get_active(lease_id)
    (Path(lease_obj.worktree_root) / "new_file.py").write_text("y = 2\n", encoding="utf-8")

    adapter = WorkflowVerificationAdapter(coordinator=coordinator, workspace=workspace)
    execution = ExecutionObservationV1(
        profile_id="qwen36_heavy_quality",
        finalized=True,
        changed_paths=("new_file.py",),
        model_calls=1,
        tool_calls=1,
        outcome="SUCCESS",
        summary="normal dirty execution",
        raw={},
    )

    observation = adapter.verify(todo, execution)
    assert observation.raw.get("kind") != "CANDIDATE_NOT_DIRTY"
    assert "report_id" in observation.raw or observation.report_id.startswith("vr-")


# TEST E: Metadata claims change but actual candidate is clean
def test_metadata_claims_change_but_candidate_clean(tmp_path: Path):
    target, manager, runs, artifacts, coordinator, workspace = _setup_workspace(tmp_path)
    todo = _workflow_todo("todo:01", "run:01")
    runs.create_goal(Goal("goal", "goal", GoalStatus.ACTIVE, "test"))
    runs.create_todo(Todo("todo:01", "goal", "obj", TodoStatus.READY, required_gates=("python_syntax",)))
    run_id, lease_id = workspace.ensure(todo)

    adapter = WorkflowVerificationAdapter(coordinator=coordinator, workspace=workspace)
    execution = ExecutionObservationV1(
        profile_id="qwen36_heavy_quality",
        finalized=True,
        changed_paths=("taskboard/models.py",),
        model_calls=1,
        tool_calls=1,
        outcome="SUCCESS",
        summary="modified models.py",
        raw={},
    )

    observation = adapter.verify(todo, execution)
    assert observation.passed is False
    assert observation.raw["kind"] == "CANDIDATE_NOT_DIRTY"
    assert observation.raw["candidate_dirty"] is False


# TEST F: Execution metadata empty but actual candidate is dirty
def test_candidate_dirty_even_if_execution_metadata_empty(tmp_path: Path):
    target, manager, runs, artifacts, coordinator, workspace = _setup_workspace(tmp_path)
    todo = _workflow_todo("todo:01", "run:01")
    runs.create_goal(Goal("goal", "goal", GoalStatus.ACTIVE, "test"))
    runs.create_todo(Todo("todo:01", "goal", "obj", TodoStatus.READY, required_gates=("python_syntax",)))
    run_id, lease_id = workspace.ensure(todo)

    # Mutate the candidate worktree physically
    lease_obj = manager.get_active(lease_id)
    (Path(lease_obj.worktree_root) / "new_file.py").write_text("y = 2\n", encoding="utf-8")

    adapter = WorkflowVerificationAdapter(coordinator=coordinator, workspace=workspace)
    execution = ExecutionObservationV1(
        profile_id="qwen36_heavy_quality",
        finalized=True,
        changed_paths=(),
        model_calls=1,
        tool_calls=0,
        outcome="SUCCESS",
        summary="empty metadata",
        raw={},
    )

    observation = adapter.verify(todo, execution)
    assert observation.raw.get("kind") != "CANDIDATE_NOT_DIRTY"
    assert "report_id" in observation.raw or observation.report_id.startswith("vr-")


# TEST G: Exact error message matching - arbitrary VerificationStateError is re-raised
def test_arbitrary_verification_state_error_reraised(tmp_path: Path):
    target, manager, runs, artifacts, coordinator, workspace = _setup_workspace(tmp_path)
    todo = _workflow_todo("todo:01", "run:01")
    runs.create_goal(Goal("goal", "goal", GoalStatus.ACTIVE, "test"))
    runs.create_todo(Todo("todo:01", "goal", "obj", TodoStatus.READY, required_gates=("python_syntax",)))
    run_id, lease_id = workspace.ensure(todo)

    mock_coordinator = MagicMock()
    mock_coordinator.verify.side_effect = VerificationStateError("some other verification state failure")

    adapter = WorkflowVerificationAdapter(coordinator=mock_coordinator, workspace=workspace)
    execution = ExecutionObservationV1(
        profile_id="qwen36_heavy_quality",
        finalized=True,
        changed_paths=(),
        model_calls=1,
        tool_calls=0,
        outcome="SUCCESS",
        summary="clean",
        raw={},
    )

    with pytest.raises(VerificationStateError, match="some other verification state failure"):
        adapter.verify(todo, execution)


# TEST H: No false verification report & determinism
def test_no_false_verification_report(tmp_path: Path):
    target, manager, runs, artifacts, coordinator, workspace = _setup_workspace(tmp_path)
    todo = _workflow_todo("todo:01", "run:01")
    runs.create_goal(Goal("goal", "goal", GoalStatus.ACTIVE, "test"))
    runs.create_todo(Todo("todo:01", "goal", "obj", TodoStatus.READY, required_gates=("python_syntax",)))
    run_id, lease_id = workspace.ensure(todo)

    adapter = WorkflowVerificationAdapter(coordinator=coordinator, workspace=workspace)
    execution = ExecutionObservationV1(
        profile_id="qwen36_heavy_quality",
        finalized=True,
        changed_paths=(),
        model_calls=1,
        tool_calls=0,
        outcome="SUCCESS",
        summary="clean final",
        raw={},
    )

    observation = adapter.verify(todo, execution)
    assert observation.passed is False
    assert observation.evidence_ids == ()
    for ref in artifacts.list_refs():
        art = artifacts.get_artifact(ref.artifact_id)
        assert art.metadata.get("verification_report_id") is None


def test_determinism(tmp_path: Path):
    target, manager, runs, artifacts, coordinator, workspace = _setup_workspace(tmp_path)
    todo = _workflow_todo("todo:01", "run:01")
    runs.create_goal(Goal("goal", "goal", GoalStatus.ACTIVE, "test"))
    runs.create_todo(Todo("todo:01", "goal", "obj", TodoStatus.READY, required_gates=("python_syntax",)))
    run_id, lease_id = workspace.ensure(todo)

    adapter = WorkflowVerificationAdapter(coordinator=coordinator, workspace=workspace)
    execution = ExecutionObservationV1(
        profile_id="qwen36_heavy_quality",
        finalized=True,
        changed_paths=(),
        model_calls=1,
        tool_calls=0,
        outcome="SUCCESS",
        summary="clean final",
        raw={},
    )

    obs_1 = adapter.verify(todo, execution)
    obs_2 = adapter.verify(todo, execution)

    assert obs_1.report_id == obs_2.report_id
    assert obs_1.fingerprint == obs_2.fingerprint
    assert obs_1.summary == obs_2.summary


def test_existing_needs_attention_semantics(tmp_path: Path):
    target, manager, runs, artifacts, coordinator, workspace = _setup_workspace(tmp_path)
    todo = _workflow_todo("todo:01", "run:01")
    runs.create_goal(Goal("goal", "goal", GoalStatus.ACTIVE, "test"))
    runs.create_todo(Todo("todo:01", "goal", "obj", TodoStatus.READY, required_gates=("python_syntax",)))
    run_id, lease_id = workspace.ensure(todo)

    # Set run status to NEEDS_ATTENTION
    run = runs.get_run(run_id)
    runs.update_run(Run(run.run_id, run.goal_id, run.active_todo_id, RunStatus.NEEDS_ATTENTION, run.quota_usage))

    adapter = WorkflowVerificationAdapter(coordinator=coordinator, workspace=workspace)
    execution = ExecutionObservationV1(
        profile_id="qwen36_heavy_quality",
        finalized=False,
        changed_paths=(),
        model_calls=3,
        tool_calls=5,
        outcome="QUOTA_EXHAUSTED",
        summary="tool call quota exhausted",
        raw={"agent_result": {"outcome": "QUOTA_EXHAUSTED"}},
    )

    observation = adapter.verify(todo, execution)
    assert observation.passed is False
    assert observation.raw["kind"] == "EXECUTION_NOT_VERIFIABLE"
    assert "execution requires attention" in observation.summary


def test_recovery_flow_receives_clean_candidate_failure(tmp_path: Path):
    target, manager, runs, artifacts, coordinator, workspace = _setup_workspace(tmp_path)
    todo = _workflow_todo("todo:01", "run:01")
    runs.create_goal(Goal("goal", "goal", GoalStatus.ACTIVE, "test"))
    runs.create_todo(Todo("todo:01", "goal", "obj", TodoStatus.READY, required_gates=("python_syntax",)))
    run_id, lease_id = workspace.ensure(todo)

    adapter = WorkflowVerificationAdapter(coordinator=coordinator, workspace=workspace)
    execution = ExecutionObservationV1(
        profile_id="qwen36_heavy_quality",
        finalized=True,
        changed_paths=(),
        model_calls=4,
        tool_calls=3,
        outcome="SUCCESS",
        summary="clean final",
        raw={"agent_result": {"last_model_id": "qwen35_heavy_fast"}},
    )

    observation = adapter.verify(todo, execution)
    evidence = build_workflow_failure_evidence(
        todo=todo,
        objective=todo.objective,
        execution=execution,
        verification=observation,
        failed_profile_ids=("qwen36_heavy_quality",),
        recovery_cycle=1,
    )
    assert evidence.failed_profile_ids == ("qwen36_heavy_quality",)
    assert evidence.workspace_paths == ("*",)
