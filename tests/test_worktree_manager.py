from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from harness.worktree import GitWorktreeManager, WorktreeConflictError, WorktreeDirtyError, WorktreeLeaseStatus, WorktreePolicy


def git(cwd: Path, *args: str, check: bool = True) -> str:
    p = subprocess.run(["git", "-C", str(cwd), *args], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=check)
    return p.stdout.strip()


def init_repo(path: Path, text: str = "BASE\n") -> Path:
    path.mkdir()
    git(path, "init")
    git(path, "config", "user.email", "test@example.com")
    git(path, "config", "user.name", "Test")
    (path / "target.txt").write_text(text, encoding="utf-8")
    git(path, "add", "target.txt")
    git(path, "commit", "-m", "init")
    return path


def manager(tmp_path: Path):
    control = init_repo(tmp_path / "control")
    external = init_repo(tmp_path / "external")
    m = GitWorktreeManager(
        protected_repo_roots=(control,), managed_root=tmp_path / "managed", state_root=tmp_path / "state"
    )
    return m, control, external


def test_acquire_isolated_worktree_does_not_modify_source_working_tree(tmp_path):
    m, _, external = manager(tmp_path)
    lease = m.acquire(source_repo_root=external, run_id="r", todo_id="t")
    wt = Path(lease.worktree_root)
    assert wt != external
    assert (wt / "target.txt").read_text(encoding="utf-8") == "BASE\n"
    (wt / "target.txt").write_text("CANDIDATE\n", encoding="utf-8")
    assert (external / "target.txt").read_text(encoding="utf-8") == "BASE\n"
    snap = m.snapshot(lease.lease_id)
    assert snap.dirty and snap.dirty_paths == ("target.txt",)
    assert "CANDIDATE" in snap.diff_text
    with pytest.raises(WorktreeDirtyError):
        m.release(lease.lease_id)
    released = m.release(lease.lease_id, force=True)
    assert released.status is WorktreeLeaseStatus.RELEASED
    assert not wt.exists()


def test_restart_rehydrates_active_lease_without_creating_second_worktree(tmp_path):
    m, control, external = manager(tmp_path)
    lease = m.acquire(source_repo_root=external, run_id="r", todo_id="t")
    restarted = GitWorktreeManager(
        protected_repo_roots=(control,), managed_root=tmp_path / "managed", state_root=tmp_path / "state"
    )
    same = restarted.acquire(source_repo_root=external, run_id="r", todo_id="t")
    assert same == lease
    assert restarted.reconcile() == (lease,)
    restarted.release(lease.lease_id, force=True)


def test_missing_active_worktree_fails_closed_on_restart(tmp_path):
    m, control, external = manager(tmp_path)
    lease = m.acquire(source_repo_root=external, run_id="r", todo_id="t")
    git(external, "worktree", "remove", "--force", lease.worktree_root)
    restarted = GitWorktreeManager(
        protected_repo_roots=(control,), managed_root=tmp_path / "managed", state_root=tmp_path / "state"
    )
    with pytest.raises(WorktreeConflictError, match="missing"):
        restarted.reconcile()


def test_dirty_source_is_denied_by_default(tmp_path):
    m, _, external = manager(tmp_path)
    (external / "target.txt").write_text("DIRTY\n", encoding="utf-8")
    with pytest.raises(Exception, match="clean"):
        m.acquire(source_repo_root=external, run_id="r", todo_id="t")


def test_active_lease_limit(tmp_path):
    control = init_repo(tmp_path / "control")
    external = init_repo(tmp_path / "external")
    m = GitWorktreeManager(
        protected_repo_roots=(control,), managed_root=tmp_path / "managed", state_root=tmp_path / "state",
        policy=WorktreePolicy(max_active_leases=1),
    )
    a = m.acquire(source_repo_root=external, run_id="r1", todo_id="t1")
    with pytest.raises(Exception, match="max_active_leases"):
        m.acquire(source_repo_root=external, run_id="r2", todo_id="t2")
    m.release(a.lease_id, force=True)
