'Trusted isolated Git worktree lifecycle manager for worker agent.5.'
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from .errors import (
    GitRepositoryError,
    WorktreeConflictError,
    WorktreeDirtyError,
    WorktreePolicyError,
)
from .git import GitClient
from .guard import RepositoryGuard
from .models import WorktreeLease, WorktreeLeaseStatus, WorktreePolicy, WorktreeSnapshot
from .store import FileWorktreeLeaseStore

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _slug(value: str) -> str:
    normalized = _SAFE.sub("-", value.strip()).strip("-.")
    if not normalized:
        normalized = "id"
    return normalized[:48]


class GitWorktreeManager:
    def __init__(
        self,
        *,
        protected_repo_roots: tuple[str | Path, ...],
        managed_root: str | Path,
        state_root: str | Path,
        policy: WorktreePolicy | None = None,
        git: GitClient | None = None,
    ) -> None:
        self.git = git or GitClient()
        self.policy = policy or WorktreePolicy()
        self.managed_root = Path(managed_root).expanduser().resolve(strict=False)
        self.managed_root.mkdir(parents=True, exist_ok=True)
        self.guard = RepositoryGuard(
            protected_roots=protected_repo_roots,
            managed_root=self.managed_root,
            git=self.git,
        )
        self.store = FileWorktreeLeaseStore(state_root)

    def _lease_identity(self, *, source_common: str, run_id: str, todo_id: str) -> tuple[str, str, Path]:
        digest = hashlib.sha256(f"{source_common}\0{run_id}\0{todo_id}".encode("utf-8")).hexdigest()[:12]
        lease_id = f"wt-{digest}"
        branch = f"{self.policy.branch_prefix}/{_slug(run_id)}/{_slug(todo_id)}/{digest}"
        path = self.managed_root / f"{_slug(run_id)}--{_slug(todo_id)}--{digest}"
        return lease_id, branch, path

    def _active_count(self) -> int:
        return sum(1 for lease in self.store.all() if lease.status is WorktreeLeaseStatus.ACTIVE)

    def planned_lease_id(self, *, source_repo_root: str | Path, run_id: str, todo_id: str) -> str:
        """Read-only identity reservation for a durable pre-allocation binding."""
        source = self.guard.assert_target_allowed(source_repo_root,
            protect_same_git_common_dir=self.policy.protect_same_git_common_dir)
        return self._lease_identity(source_common=source.git_common_dir, run_id=run_id, todo_id=todo_id)[0]

    def _source_clean(self, repo_root: Path) -> bool:
        return not self.git.text(repo_root, "status", "--porcelain=v1", "--untracked-files=normal")

    def _verify_existing_active(self, lease: WorktreeLease) -> WorktreeLease:
        root = Path(lease.worktree_root)
        if not root.is_dir():
            raise WorktreeConflictError("active durable lease points to a missing worktree")
        identity = self.guard.discover(root)
        if identity.git_common_dir != lease.source_git_common_dir:
            raise WorktreeConflictError("active worktree Git common-dir does not match durable lease")
        branch = self.git.text(root, "rev-parse", "--abbrev-ref", "HEAD")
        if branch != lease.branch_name:
            raise WorktreeConflictError("active worktree branch does not match durable lease")
        return lease

    def acquire(
        self,
        *,
        source_repo_root: str | Path,
        run_id: str,
        todo_id: str,
        base_ref: str = "HEAD",
    ) -> WorktreeLease:
        source = self.guard.assert_target_allowed(
            source_repo_root,
            protect_same_git_common_dir=self.policy.protect_same_git_common_dir,
        )
        source_root = Path(source.root)
        if self.policy.require_clean_source and not self._source_clean(source_root):
            raise WorktreePolicyError("source repository must be clean before acquiring an isolated worktree")
        base_commit = self.git.text(source_root, "rev-parse", "--verify", f"{base_ref}^{{commit}}")
        lease_id, branch, worktree_root = self._lease_identity(
            source_common=source.git_common_dir,
            run_id=run_id,
            todo_id=todo_id,
        )
        existing = self.store.get(lease_id)
        if existing is not None:
            if existing.status is WorktreeLeaseStatus.ACTIVE:
                if (
                    existing.source_repo_root != source.root
                    or existing.source_git_common_dir != source.git_common_dir
                    or existing.base_commit != base_commit
                ):
                    raise WorktreeConflictError("active lease identity conflicts with requested source/base")
                return self._verify_existing_active(existing)
            raise WorktreeConflictError("released lease identity cannot be silently reused")
        if self._active_count() >= self.policy.max_active_leases:
            raise WorktreePolicyError("max_active_leases reached")
        if worktree_root.exists():
            raise WorktreeConflictError("candidate worktree path already exists without a durable active lease")
        branch_exists = self.git.run(source_root, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False).returncode == 0
        if branch_exists:
            raise WorktreeConflictError("candidate branch already exists without a durable active lease")
        try:
            self.git.run(source_root, "worktree", "add", "-b", branch, str(worktree_root), base_commit)
        except Exception:
            if worktree_root.exists():
                shutil.rmtree(worktree_root, ignore_errors=True)
            raise
        lease = WorktreeLease(
            lease_id=lease_id,
            run_id=run_id,
            todo_id=todo_id,
            source_repo_root=source.root,
            source_git_common_dir=source.git_common_dir,
            worktree_root=str(worktree_root.resolve(strict=True)),
            branch_name=branch,
            base_commit=base_commit,
            status=WorktreeLeaseStatus.ACTIVE,
            created_at=_utc_now(),
        )
        try:
            self.store.put_new(lease)
        except Exception:
            self.git.run(source_root, "worktree", "remove", "--force", str(worktree_root), check=False)
            self.git.run(source_root, "branch", "-D", branch, check=False)
            raise
        return lease

    def get_active(self, lease_id: str) -> WorktreeLease:
        lease = self.store.get(lease_id)
        if lease is None or lease.status is not WorktreeLeaseStatus.ACTIVE:
            raise WorktreeConflictError("lease is not ACTIVE")
        return self._verify_existing_active(lease)

    def snapshot(self, lease_id: str) -> WorktreeSnapshot:
        lease = self.get_active(lease_id)
        root = Path(lease.worktree_root)
        status = self.git.run(root, "status", "--porcelain=v1", "--untracked-files=all").stdout.rstrip("\n")
        paths: list[str] = []
        for line in status.splitlines():
            if len(line) < 4:
                continue
            raw = line[3:]
            if " -> " in raw:
                raw = raw.split(" -> ", 1)[1]
            paths.append(raw.strip().strip('"'))
        paths = sorted(dict.fromkeys(paths), key=str.casefold)
        diff = self.git.run(root, "diff", "--binary", "--no-ext-diff", "HEAD", "--").stdout
        # Include untracked UTF-8 files deterministically because plain git diff omits them.
        extra: list[str] = []
        for rel in paths:
            candidate = root / rel
            tracked = self.git.run(root, "ls-files", "--error-unmatch", "--", rel, check=False).returncode == 0
            if not tracked and candidate.is_file() and not candidate.is_symlink():
                try:
                    content = candidate.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    content = "<BINARY_OR_NON_UTF8>"
                extra.append(f"\n--- /dev/null\n+++ b/{rel}\n@@ UNTRACKED @@\n{content}")
        diff_text = diff + "".join(extra)
        files = []
        names = self.git.run(root, 'ls-files', '-z', '--cached', '--others', '--exclude-standard').stdout
        for rel in sorted(set(filter(None, names.split('\0')))):
            target = root / rel
            if any(p.is_symlink() or getattr(p.lstat(), 'st_file_attributes', 0) & 0x400
                   for p in target.parents if p != root and root in p.parents):
                raise WorktreeConflictError('candidate inventory contains an aliased parent')
            if target.is_symlink():
                raw = ('SYMLINK\0' + os.readlink(target)).encode('utf8')
                digest = hashlib.sha256(raw).hexdigest()
            elif target.is_file():
                before_stat = target.stat()
                with target.open('rb') as stream:
                    digest = hashlib.file_digest(stream, 'sha256').hexdigest()
                after_stat = target.stat()
                if any(getattr(after_stat, key) != getattr(before_stat, key)
                       for key in ('st_ino', 'st_dev', 'st_mode', 'st_size', 'st_mtime_ns')):
                    raise WorktreeConflictError('candidate changed during raw inventory')
            else:
                digest = hashlib.sha256(b'DIRECTORY' if target.is_dir() else b'MISSING').hexdigest()
            files.append((rel, digest))
        content_sha = hashlib.sha256(json.dumps(files, ensure_ascii=False,
            separators=(',', ':')).encode('utf8')).hexdigest()
        return WorktreeSnapshot(
            lease_id=lease.lease_id,
            head_commit=self.git.text(root, "rev-parse", "HEAD"),
            dirty_paths=tuple(paths),
            diff_sha256=hashlib.sha256(diff_text.encode("utf-8")).hexdigest(),
            diff_text=diff_text,
            content_sha256=content_sha, file_sha256=tuple(files),
        )

    def release(self, lease_id: str, *, force: bool = False, delete_branch: bool = True) -> WorktreeLease:
        lease = self.get_active(lease_id)
        snapshot = self.snapshot(lease_id)
        if snapshot.dirty and not force:
            raise WorktreeDirtyError("worktree is dirty; capture results and pass force=True for destructive cleanup")
        source = Path(lease.source_repo_root)
        args = ["worktree", "remove"]
        if force:
            args.append("--force")
        args.append(lease.worktree_root)
        self.git.run(source, *args)
        if Path(lease.worktree_root).exists():
            raise WorktreeConflictError("git reported worktree removal success but path still exists")
        if delete_branch:
            self.git.run(source, "branch", "-D", lease.branch_name)
        released = replace(lease, status=WorktreeLeaseStatus.RELEASED, released_at=_utc_now())
        self.store.replace(released)
        return released

    def reconcile(self) -> tuple[WorktreeLease, ...]:
        active: list[WorktreeLease] = []
        for lease in self.store.all():
            if lease.status is WorktreeLeaseStatus.ACTIVE:
                active.append(self._verify_existing_active(lease))
        return tuple(active)
