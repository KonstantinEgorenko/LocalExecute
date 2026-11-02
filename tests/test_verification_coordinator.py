from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from harness.context_manager import FileArtifactStore
from harness.control_plane import FileRunStore, Goal, GoalStatus, Quota, QuotaUsage, Run, RunStatus, Todo, TodoStatus, WorkContract
from harness.verification import CommandCatalogEntry, VerificationCoordinator, VerificationGateType, VerificationIntegrityError, VerificationOutcome, VerificationPolicy
from harness.worktree import GitWorktreeManager


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], text=True, encoding="utf-8", stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout.strip()


def init_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    git(path, "init")
    git(path, "config", "user.email", "phase7@example.invalid")
    git(path, "config", "user.name", "Phase7")
    (path / "target.py").write_text('VALUE = "OLD"\n', encoding="utf-8")
    git(path, "add", ".")
    git(path, "commit", "-m", "base")
    return path


def setup(tmp_path: Path, *, expected='VALUE = "NEW"\n'):
    control = init_repo(tmp_path / "control")
    external = init_repo(tmp_path / "external")
    manager = GitWorktreeManager(
        protected_repo_roots=(control,), managed_root=tmp_path / "managed", state_root=tmp_path / "worktree-state"
    )
    run_store = FileRunStore(tmp_path / "cp")
    run_store.create_goal(Goal("g", "goal", GoalStatus.ACTIVE, "now"))
    gates = ("candidate_diff", "scope_guard", "python_syntax", "exact_target", "check")
    run_store.create_todo(Todo("t", "g", "todo", TodoStatus.RUNNING, (), gates, 0))
    run_store.create_run(Run("r", "g", "t", RunStatus.RUNNING, QuotaUsage()))
    contract = WorkContract(
        "g", "t", "todo",
        {"verification_file_assertions": {"exact_target": {"mode": "EXACT_TEXT", "path": "target.py", "expected": expected}}},
        ("target.py",), (), (), Quota(6, 2, 2, 2, 3),
    )
    lease = manager.acquire(source_repo_root=external, run_id="r", todo_id="t")
    artifacts = FileArtifactStore(tmp_path / "artifacts")
    policy = VerificationPolicy(trusted_python_executable=sys.executable, command_catalog={
        "check": CommandCatalogEntry(
            "check", VerificationGateType.TESTS,
            ("{python}", "-c", "from pathlib import Path; assert 'NEW' in Path('target.py').read_text()"), 10,
        )
    })
    return control, external, manager, run_store, artifacts, policy, contract, lease


def test_coordinator_passes_gates_publishes_report_and_completes_run(tmp_path):
    _, external, manager, run_store, artifacts, policy, contract, lease = setup(tmp_path)
    Path(lease.worktree_root, "target.py").write_text('VALUE = "NEW"\n', encoding="utf-8")
    result = VerificationCoordinator(run_store=run_store, artifact_store=artifacts, manager=manager, policy=policy).verify(
        run_id="r", contract=contract, lease_id=lease.lease_id
    )
    assert result.report.outcome is VerificationOutcome.PASSED
    assert result.run_status == "COMPLETED"
    assert result.reused_existing_report is False
    assert (external / "target.py").read_text(encoding="utf-8") == 'VALUE = "OLD"\n'
    assert any(ref.kind.value == "TEST_RESULT" for ref in artifacts.list_refs())


def test_restart_reuses_existing_report_without_duplicate_events(tmp_path):
    _, _, manager, run_store, artifacts, policy, contract, lease = setup(tmp_path)
    Path(lease.worktree_root, "target.py").write_text('VALUE = "NEW"\n', encoding="utf-8")
    first = VerificationCoordinator(run_store=run_store, artifact_store=artifacts, manager=manager, policy=policy).verify(
        run_id="r", contract=contract, lease_id=lease.lease_id
    )
    event_count = len(run_store.list_events("r"))
    restarted_manager = GitWorktreeManager(
        protected_repo_roots=(tmp_path / "control",), managed_root=tmp_path / "managed", state_root=tmp_path / "worktree-state"
    )
    restarted_runs = FileRunStore(tmp_path / "cp")
    restarted_artifacts = FileArtifactStore(tmp_path / "artifacts")
    second = VerificationCoordinator(
        run_store=restarted_runs, artifact_store=restarted_artifacts, manager=restarted_manager, policy=policy
    ).verify(run_id="r", contract=contract, lease_id=lease.lease_id)
    assert second.reused_existing_report is True
    assert second.report == first.report
    assert len(restarted_runs.list_events("r")) == event_count


def test_failed_required_gate_marks_run_failed(tmp_path):
    _, _, manager, run_store, artifacts, policy, contract, lease = setup(tmp_path, expected='VALUE = "EXPECTED"\n')
    Path(lease.worktree_root, "target.py").write_text('VALUE = "NEW"\n', encoding="utf-8")
    result = VerificationCoordinator(run_store=run_store, artifact_store=artifacts, manager=manager, policy=policy).verify(
        run_id="r", contract=contract, lease_id=lease.lease_id
    )
    assert result.report.outcome is VerificationOutcome.FAILED
    assert result.run_status == "FAILED"


def test_candidate_change_after_completed_verification_cannot_reuse_old_pass(tmp_path):
    _, _, manager, run_store, artifacts, policy, contract, lease = setup(tmp_path)
    target = Path(lease.worktree_root, "target.py")
    target.write_text('VALUE = "NEW"\n', encoding="utf-8")
    VerificationCoordinator(run_store=run_store, artifact_store=artifacts, manager=manager, policy=policy).verify(
        run_id="r", contract=contract, lease_id=lease.lease_id
    )
    target.write_text('VALUE = "CHANGED_AFTER_PASS"\n', encoding="utf-8")
    # A different candidate snapshot produces a different report identity; a completed run cannot accept a new failing report.
    with pytest.raises(VerificationIntegrityError, match="completed Run"):
        VerificationCoordinator(run_store=run_store, artifact_store=artifacts, manager=manager, policy=policy).verify(
            run_id="r", contract=contract, lease_id=lease.lease_id
        )


def test_verification_evidence_with_missing_report_artifact_fails_closed(tmp_path):
    _, _, manager, run_store, artifacts, policy, contract, lease = setup(tmp_path)
    Path(lease.worktree_root, "target.py").write_text('VALUE = "NEW"\n', encoding="utf-8")
    from harness.control_plane import Evidence, EvidenceStatus
    from harness.context_manager.integration import artifact_uri
    run_store.record_evidence(
        "r",
        Evidence(
            "fake-verification-evidence", "t", "VERIFICATION_REPORT", "VerificationRunner",
            EvidenceStatus.PASSED,
            {"report_id": "missing-report", "candidate_diff_sha256": manager.snapshot(lease.lease_id).diff_sha256},
            artifact_uri("missing-report:report"),
        ),
    )
    with pytest.raises(VerificationIntegrityError, match="missing report artifact"):
        VerificationCoordinator(run_store=run_store, artifact_store=artifacts, manager=manager, policy=policy).verify(
            run_id="r", contract=contract, lease_id=lease.lease_id
        )
