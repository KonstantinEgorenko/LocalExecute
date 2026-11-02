from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any


class NetworkProfile(str, Enum):
    NONE = "NONE"
    RESEARCH_ONLY = "RESEARCH_ONLY"
    PACKAGE_INSTALL = "PACKAGE_INSTALL"


class SandboxLifecycle(str, Enum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    STOPPED = "STOPPED"
    DESTROYED = "DESTROYED"


@dataclass(frozen=True, slots=True)
class SandboxIdentity:
    sandbox_id: str
    lease_id: str
    worktree_path: str
    worktree_fingerprint: str
    image: str
    network_profile: NetworkProfile

    def to_dict(self) -> dict[str, Any]:
        raw = asdict(self)
        raw["network_profile"] = self.network_profile.value
        return raw


@dataclass(frozen=True, slots=True)
class SandboxSnapshot:
    identity: SandboxIdentity
    container_name: str
    container_id: str
    image_id: str
    lifecycle: SandboxLifecycle
    created_at: str
    updated_at: str
    restart_count: int
    security: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity": self.identity.to_dict(),
            "container_name": self.container_name,
            "container_id": self.container_id,
            "image_id": self.image_id,
            "lifecycle": self.lifecycle.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "restart_count": self.restart_count,
            "security": dict(self.security),
        }


@dataclass(frozen=True, slots=True)
class SandboxCommandResult:
    sandbox_id: str
    argv: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str
    started_at: str
    finished_at: str
    duration_ms: int
    timed_out: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "sandbox_id": self.sandbox_id,
            "argv": list(self.argv),
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": self.duration_ms,
            "timed_out": self.timed_out,
        }
