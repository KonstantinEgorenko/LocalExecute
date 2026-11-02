from __future__ import annotations
from harness.compatibility.configuration import resolve_config_path, normalize_configuration_document

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import yaml

from .models import NetworkProfile


@dataclass(frozen=True, slots=True)
class SandboxPolicy:
    default_image: str
    allowed_images: tuple[str, ...]
    name_prefix: str
    workspace_mount: str
    workdir: str
    command_timeout_seconds: int
    stop_timeout_seconds: int
    max_stdout_chars: int
    max_stderr_chars: int
    memory: str
    memory_swap: str
    cpus: float
    pids_limit: int
    tmpfs_size: str
    rootfs_read_only: bool
    cap_drop_all: bool
    no_new_privileges: bool
    forbid_privileged: bool
    forbid_docker_socket: bool
    forbid_host_network: bool
    forbid_host_pid: bool
    require_external_worktree: bool
    require_linked_git_worktree: bool
    default_network: NetworkProfile
    allow_package_install_profile: bool
    research_proxy_configured: bool
    max_restarts_per_sandbox: int
    live_image: str
    require_docker_engine: bool

    def __post_init__(self):
        if not self.name_prefix.startswith("swarm-"):
            raise ValueError("sandbox name_prefix must match Monitor V3 swarm-* contract")
        if self.workspace_mount != "/workspace":
            raise ValueError("Phase20B workspace mount is fixed to /workspace")
        if self.command_timeout_seconds <= 0:
            raise ValueError("command timeout must be positive")
        if self.pids_limit <= 0 or self.cpus <= 0:
            raise ValueError("resource limits must be positive")
        if self.max_restarts_per_sandbox < 0:
            raise ValueError("restart bound cannot be negative")
        if self.default_image not in self.allowed_images:
            raise ValueError("default image must be allowlisted")

    @classmethod
    def from_project_root(cls, root) -> "SandboxPolicy":
        raw = normalize_configuration_document(yaml.safe_load((resolve_config_path(Path(root) / "config/phase20b.yaml")).read_text(encoding="utf-8")))
        if not isinstance(raw, Mapping) or raw.get("schema_version") != 1:
            raise ValueError("phase20b.yaml requires schema_version: 1")
        body = raw["worktree_docker_sandbox_v1"]
        image = body["image"]
        container = body["container"]
        resources = body["resources"]
        security = body["security"]
        network = body["network"]
        recovery = body["recovery"]
        live = body["live_acceptance"]
        return cls(
            default_image=str(image["default"]),
            allowed_images=tuple(str(x) for x in image["allowed"]),
            name_prefix=str(container["name_prefix"]),
            workspace_mount=str(container["workspace_mount"]),
            workdir=str(container["workdir"]),
            command_timeout_seconds=int(container["command_timeout_seconds"]),
            stop_timeout_seconds=int(container["stop_timeout_seconds"]),
            max_stdout_chars=int(container["max_stdout_chars"]),
            max_stderr_chars=int(container["max_stderr_chars"]),
            memory=str(resources["memory"]),
            memory_swap=str(resources["memory_swap"]),
            cpus=float(resources["cpus"]),
            pids_limit=int(resources["pids_limit"]),
            tmpfs_size=str(resources["tmpfs_size"]),
            rootfs_read_only=bool(security["rootfs_read_only"]),
            cap_drop_all=bool(security["cap_drop_all"]),
            no_new_privileges=bool(security["no_new_privileges"]),
            forbid_privileged=bool(security["forbid_privileged"]),
            forbid_docker_socket=bool(security["forbid_docker_socket"]),
            forbid_host_network=bool(security["forbid_host_network"]),
            forbid_host_pid=bool(security["forbid_host_pid"]),
            require_external_worktree=bool(security["require_external_worktree"]),
            require_linked_git_worktree=bool(security["require_linked_git_worktree"]),
            default_network=NetworkProfile(str(network["default"]).upper()),
            allow_package_install_profile=bool(network["allow_package_install_profile"]),
            research_proxy_configured=bool(network["research_proxy_configured"]),
            max_restarts_per_sandbox=int(recovery["max_restarts_per_sandbox"]),
            live_image=str(live["image"]),
            require_docker_engine=bool(live["require_docker_engine"]),
        )

# Verified legacy import/pickle aliases, not canonical public exports.
Phase20BPolicy = SandboxPolicy
