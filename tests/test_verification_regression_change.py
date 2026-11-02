from __future__ import annotations

import sys
from pathlib import Path

import pytest

from harness.context_manager import FileArtifactStore
from harness.control_plane import FileRunStore, Goal, GoalStatus, Quota, QuotaUsage, Run, RunStatus, Todo, TodoStatus, WorkContract
from harness.verification import CommandCatalogEntry, VerificationCoordinator, VerificationGateType, VerificationOutcome, VerificationPolicy
from harness.worktree import GitWorktreeManager
from test_verification_coordinator import git, init_repo


BASE = 'def test_value():\n    """Original documentation."""\n    assert 1 == 1\n'


def fixture(tmp_path):
    control = init_repo(tmp_path / "control")
    external = init_repo(tmp_path / "external")
    (external / "tests").mkdir()
    (external / "tests/test_value.py").write_text(BASE, encoding="utf-8")
    git(external, "add", "tests")
    git(external, "commit", "-m", "baseline tests")
    manager = GitWorktreeManager(protected_repo_roots=(control,), managed_root=tmp_path / "managed", state_root=tmp_path / "leases")
    runs = FileRunStore(tmp_path / "runs")
    runs.create_goal(Goal("g", "goal", GoalStatus.ACTIVE, "now"))
    gates = ("candidate_diff", "scope_guard", "python_syntax", "regression_change", "pytest")
    runs.create_todo(Todo("t", "g", "todo", TodoStatus.RUNNING, (), gates, 0))
    runs.create_run(Run("r", "g", "t", RunStatus.RUNNING, QuotaUsage()))
    contract = WorkContract("g", "t", "update behavior and regression", {}, (), (), (), Quota(6, 2, 2, 2, 3))
    lease = manager.acquire(source_repo_root=external, run_id="r", todo_id="t")
    artifacts = FileArtifactStore(tmp_path / "artifacts")
    policy = VerificationPolicy(trusted_python_executable=sys.executable, command_catalog={
        "pytest": CommandCatalogEntry("pytest", VerificationGateType.TESTS,
            ("{python}", "-c", f"import sys; sys.path[:0] = {sys.path!r}; import pytest; raise SystemExit(pytest.main(['-q', 'tests']))"), 30)
    })
    coordinator = VerificationCoordinator(run_store=runs, artifact_store=artifacts, manager=manager, policy=policy)
    return Path(lease.worktree_root), coordinator, contract, lease, artifacts


@pytest.mark.parametrize("change", ["source", "comment", "docstring", "delete", "helper", "syntax"])
def test_regression_gate_rejects_missing_or_invalid_test_function_change(tmp_path, change):
    root, coordinator, contract, lease, artifacts = fixture(tmp_path)
    target = root / "tests/test_value.py"
    if change == "source":
        (root / "target.py").write_text('VALUE = "NEW"\n', encoding="utf-8")
    elif change == "comment":
        target.write_text("# new comment\n" + BASE, encoding="utf-8")
    elif change == "docstring":
        target.write_text(BASE.replace("Original documentation.", "New documentation."), encoding="utf-8")
    elif change == "delete":
        target.unlink()
    elif change == "helper":
        target.write_text(BASE + "\ndef helper():\n    return 2\n", encoding="utf-8")
    else:
        target.write_text("def test_value(:\n", encoding="utf-8")
    result = coordinator.verify(run_id="r", contract=contract, lease_id=lease.lease_id)
    gate = next(item for item in result.report.results if item.gate_id == "regression_change")
    assert gate.outcome.value == "FAILED"
    assert result.report.outcome is VerificationOutcome.FAILED
    assert any(ref.kind.value == "TEST_RESULT" for ref in artifacts.list_refs())


@pytest.mark.parametrize("change", ["modify", "add"])
def test_regression_gate_accepts_actual_test_change_with_real_pytest_and_durable_restart(tmp_path, change):
    root, coordinator, contract, lease, _ = fixture(tmp_path)
    target = root / "tests/test_value.py"
    target.write_text(BASE.replace("assert 1 == 1", "assert 2 == 2") if change == "modify"
                      else BASE + "\ndef test_new():\n    assert 2 == 2\n", encoding="utf-8")
    first = coordinator.verify(run_id="r", contract=contract, lease_id=lease.lease_id)
    assert first.report.outcome is VerificationOutcome.PASSED
    assert all(item.outcome.value == "PASSED" for item in first.report.results)
    second = coordinator.verify(run_id="r", contract=contract, lease_id=lease.lease_id)
    assert second.reused_existing_report
    assert second.report == first.report
