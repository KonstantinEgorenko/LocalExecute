'Immutable contracts for worker agent.5 isolated Git worktrees.'
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _positive_int(value: object, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


class WorktreeLeaseStatus(StrEnum):
    ACTIVE = "ACTIVE"
    RELEASED = "RELEASED"


@dataclass(frozen=True, slots=True)
class RepositoryIdentity:
    root: str
    git_common_dir: str
    head_commit: str

    def __post_init__(self) -> None:
        for field_name in ("root", "git_common_dir", "head_commit"):
            _required_text(getattr(self, field_name), field_name)

    def to_dict(self) -> dict[str, str]:
        return {
            "root": self.root,
            "git_common_dir": self.git_common_dir,
            "head_commit": self.head_commit,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "RepositoryIdentity":
        return cls(
            root=str(payload["root"]),
            git_common_dir=str(payload["git_common_dir"]),
            head_commit=str(payload["head_commit"]),
        )


@dataclass(frozen=True, slots=True)
class WorktreePolicy:
    branch_prefix: str = "swarm/candidate"
    require_clean_source: bool = True
    protect_same_git_common_dir: bool = True
    allow_model_commands: bool = False
    max_active_leases: int = 8

    def __post_init__(self) -> None:
        _required_text(self.branch_prefix, "branch_prefix")
        if self.branch_prefix.startswith("/") or self.branch_prefix.endswith("/"):
            raise ValueError("branch_prefix must not start/end with '/'")
        if ".." in self.branch_prefix.split("/"):
            raise ValueError("branch_prefix must not contain '..'")
        for field_name in ("require_clean_source", "protect_same_git_common_dir", "allow_model_commands"):
            if not isinstance(getattr(self, field_name), bool):
                raise ValueError(f"{field_name} must be a bool")
        _positive_int(self.max_active_leases, "max_active_leases")

    def to_dict(self) -> dict[str, object]:
        return {
            "branch_prefix": self.branch_prefix,
            "require_clean_source": self.require_clean_source,
            "protect_same_git_common_dir": self.protect_same_git_common_dir,
            "allow_model_commands": self.allow_model_commands,
            "max_active_leases": self.max_active_leases,
        }


@dataclass(frozen=True, slots=True)
class WorktreeLease:
    lease_id: str
    run_id: str
    todo_id: str
    source_repo_root: str
    source_git_common_dir: str
    worktree_root: str
    branch_name: str
    base_commit: str
    status: WorktreeLeaseStatus
    created_at: str
    released_at: str | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "lease_id", "run_id", "todo_id", "source_repo_root", "source_git_common_dir",
            "worktree_root", "branch_name", "base_commit", "created_at",
        ):
            _required_text(getattr(self, field_name), field_name)
        if not isinstance(self.status, WorktreeLeaseStatus):
            raise ValueError("status must be a WorktreeLeaseStatus")
        if self.released_at is not None:
            _required_text(self.released_at, "released_at")
        if self.status is WorktreeLeaseStatus.ACTIVE and self.released_at is not None:
            raise ValueError("ACTIVE lease must not have released_at")
        if self.status is WorktreeLeaseStatus.RELEASED and self.released_at is None:
            raise ValueError("RELEASED lease requires released_at")

    def to_dict(self) -> dict[str, object]:
        return {
            "lease_id": self.lease_id,
            "run_id": self.run_id,
            "todo_id": self.todo_id,
            "source_repo_root": self.source_repo_root,
            "source_git_common_dir": self.source_git_common_dir,
            "worktree_root": self.worktree_root,
            "branch_name": self.branch_name,
            "base_commit": self.base_commit,
            "status": self.status.value,
            "created_at": self.created_at,
            "released_at": self.released_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "WorktreeLease":
        return cls(
            lease_id=str(payload["lease_id"]),
            run_id=str(payload["run_id"]),
            todo_id=str(payload["todo_id"]),
            source_repo_root=str(payload["source_repo_root"]),
            source_git_common_dir=str(payload["source_git_common_dir"]),
            worktree_root=str(payload["worktree_root"]),
            branch_name=str(payload["branch_name"]),
            base_commit=str(payload["base_commit"]),
            status=WorktreeLeaseStatus(str(payload["status"])),
            created_at=str(payload["created_at"]),
            released_at=None if payload.get("released_at") is None else str(payload["released_at"]),
        )


@dataclass(frozen=True, slots=True)
class WorktreeSnapshot:
    lease_id: str
    head_commit: str
    dirty_paths: tuple[str, ...]
    diff_sha256: str
    diff_text: str
    content_sha256: str | None = None
    file_sha256: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        _required_text(self.lease_id, "lease_id")
        _required_text(self.head_commit, "head_commit")
        _required_text(self.diff_sha256, "diff_sha256")
        if not isinstance(self.diff_text, str):
            raise ValueError("diff_text must be a string")
        normalized = tuple(_required_text(value, "dirty_paths") for value in self.dirty_paths)
        if len(set(normalized)) != len(normalized):
            raise ValueError("dirty_paths must not contain duplicates")
        object.__setattr__(self, "dirty_paths", normalized)

    @property
    def dirty(self) -> bool:
        return bool(self.dirty_paths)

    def to_dict(self) -> dict[str, object]:
        return {
            "lease_id": self.lease_id,
            "head_commit": self.head_commit,
            "dirty_paths": list(self.dirty_paths),
            "diff_sha256": self.diff_sha256,
            "diff_text": self.diff_text,
            "content_sha256": self.content_sha256,
            "file_sha256": [list(row) for row in self.file_sha256],
        }
