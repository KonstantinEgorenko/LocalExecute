from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .docker_cli import DockerCli
from .models import NetworkProfile, SandboxIdentity, SandboxLifecycle, SandboxSnapshot
from .policy import SandboxPolicy
from .security import (
    SandboxSecurityError,
    docker_network_name,
    ensure_image_allowed,
    fingerprint_worktree,
    sanitize_sandbox_id,
    validate_external_worktree,
    validate_linked_git_worktree,
)
from .store import JsonSandboxStore


class SandboxIdentityMismatch(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DockerSandboxManager:
    LABEL_PHASE = "swarm.phase"
    LABEL_SANDBOX = "swarm.sandbox_id"
    LABEL_LEASE = "swarm.lease_id"
    LABEL_WORKTREE = "swarm.worktree_fingerprint"
    LABEL_NETWORK = "swarm.network_profile"

    def __init__(
        self,
        *,
        control_root: Path,
        policy: SandboxPolicy,
        docker: DockerCli,
        store: JsonSandboxStore,
    ) -> None:
        self.control_root = Path(control_root).resolve()
        self.policy = policy
        self.docker = docker
        self.store = store

    def acquire(
        self,
        *,
        sandbox_id: str,
        lease_id: str,
        worktree: Path,
        image: str | None = None,
        network_profile: NetworkProfile | None = None,
    ) -> SandboxSnapshot:
        safe_id = sanitize_sandbox_id(sandbox_id)
        candidate = validate_external_worktree(worktree, self.control_root)
        if self.policy.require_linked_git_worktree:
            validate_linked_git_worktree(candidate)
        selected_image = ensure_image_allowed(
            image or self.policy.default_image,
            self.policy,
        )
        profile = network_profile or self.policy.default_network
        network_name = docker_network_name(profile, self.policy)
        fingerprint = fingerprint_worktree(candidate)
        identity = SandboxIdentity(
            sandbox_id=safe_id,
            lease_id=str(lease_id),
            worktree_path=str(candidate),
            worktree_fingerprint=fingerprint,
            image=selected_image,
            network_profile=profile,
        )
        name = f"{self.policy.name_prefix}{safe_id}"

        existing = self.docker.container_inspect(name, required=False)
        if existing is not None:
            self._verify_existing(existing, identity)
            running = bool(existing.get("State", {}).get("Running"))
            restart_count = self._restart_count(safe_id)
            if not running:
                if restart_count >= self.policy.max_restarts_per_sandbox:
                    raise RuntimeError(
                        f"sandbox restart budget exhausted: {restart_count}"
                    )
                self.docker.run(["start", name], timeout=60)
                restart_count += 1
                existing = self.docker.container_inspect(name)
                self._event(safe_id, "RESTARTED", {"restart_count": restart_count})
            snapshot = self._snapshot(existing, identity, restart_count)
            self._persist(snapshot)
            return snapshot

        image_info = self.docker.image_inspect(selected_image)
        image_id = str(image_info.get("Id", ""))
        mount_spec = (
            f"type=bind,src={candidate},dst={self.policy.workspace_mount}"
        )
        args = [
            "create",
            "--name", name,
            "--label", f"{self.LABEL_PHASE}=20b",
            "--label", f"{self.LABEL_SANDBOX}={safe_id}",
            "--label", f"{self.LABEL_LEASE}={identity.lease_id}",
            "--label", f"{self.LABEL_WORKTREE}={fingerprint}",
            "--label", f"{self.LABEL_NETWORK}={profile.value}",
            "--network", network_name,
            "--workdir", self.policy.workdir,
            "--mount", mount_spec,
            "--memory", self.policy.memory,
            "--memory-swap", self.policy.memory_swap,
            "--cpus", str(self.policy.cpus),
            "--pids-limit", str(self.policy.pids_limit),
            "--tmpfs",
            f"/tmp:rw,noexec,nosuid,size={self.policy.tmpfs_size}",
        ]
        if self.policy.rootfs_read_only:
            args.append("--read-only")
        if self.policy.cap_drop_all:
            args += ["--cap-drop", "ALL"]
        if self.policy.no_new_privileges:
            args += ["--security-opt", "no-new-privileges:true"]

        args += [
            selected_image,
            "python",
            "-c",
            "import time; time.sleep(86400)",
        ]
        created = self.docker.run(args, timeout=120)
        container_id = created.stdout.strip()
        self.docker.run(["start", name], timeout=60)
        inspect = self.docker.container_inspect(name)
        snapshot = SandboxSnapshot(
            identity=identity,
            container_name=name,
            container_id=container_id or str(inspect.get("Id", "")),
            image_id=image_id or str(inspect.get("Image", "")),
            lifecycle=SandboxLifecycle.RUNNING,
            created_at=_now(),
            updated_at=_now(),
            restart_count=0,
            security=self._security_snapshot(inspect),
        )
        self._persist(snapshot)
        self._event(safe_id, "CREATED", {"container_name": name})
        return snapshot

    def stop(self, sandbox_id: str) -> None:
        safe_id = sanitize_sandbox_id(sandbox_id)
        name = f"{self.policy.name_prefix}{safe_id}"
        inspect = self.docker.container_inspect(name, required=False)
        if inspect is None:
            return
        if bool(inspect.get("State", {}).get("Running")):
            self.docker.run(
                ["stop", "--time", str(self.policy.stop_timeout_seconds), name],
                timeout=self.policy.stop_timeout_seconds + 20,
            )
        self._event(safe_id, "STOPPED", {})

    def destroy(self, sandbox_id: str) -> None:
        safe_id = sanitize_sandbox_id(sandbox_id)
        name = f"{self.policy.name_prefix}{safe_id}"
        inspect = self.docker.container_inspect(name, required=False)
        if inspect is not None:
            self.docker.run(["rm", "-f", name], timeout=60)
        self._event(safe_id, "DESTROYED", {})

    def _verify_existing(self, inspect: dict, identity: SandboxIdentity) -> None:
        labels = inspect.get("Config", {}).get("Labels", {}) or {}
        expected = {
            self.LABEL_PHASE: "20b",
            self.LABEL_SANDBOX: identity.sandbox_id,
            self.LABEL_LEASE: identity.lease_id,
            self.LABEL_WORKTREE: identity.worktree_fingerprint,
            self.LABEL_NETWORK: identity.network_profile.value,
        }
        actual = {key: labels.get(key) for key in expected}
        if actual != expected:
            raise SandboxIdentityMismatch(
                f"existing sandbox labels do not match expected identity: "
                f"actual={actual} expected={expected}"
            )

    def _restart_count(self, sandbox_id: str) -> int:
        snapshot = self.store.read_snapshot(sandbox_id)
        if not snapshot:
            return 0
        return int(snapshot.get("restart_count", 0))

    def _snapshot(
        self,
        inspect: dict,
        identity: SandboxIdentity,
        restart_count: int,
    ) -> SandboxSnapshot:
        previous = self.store.read_snapshot(identity.sandbox_id) or {}
        return SandboxSnapshot(
            identity=identity,
            container_name=f"{self.policy.name_prefix}{identity.sandbox_id}",
            container_id=str(inspect.get("Id", "")),
            image_id=str(inspect.get("Image", "")),
            lifecycle=SandboxLifecycle.RUNNING
            if bool(inspect.get("State", {}).get("Running"))
            else SandboxLifecycle.STOPPED,
            created_at=str(previous.get("created_at") or _now()),
            updated_at=_now(),
            restart_count=restart_count,
            security=self._security_snapshot(inspect),
        )

    @staticmethod
    def _security_snapshot(inspect: dict) -> dict:
        host = inspect.get("HostConfig", {}) or {}
        mounts = inspect.get("Mounts", []) or []
        return {
            "readonly_rootfs": bool(host.get("ReadonlyRootfs")),
            "network_mode": str(host.get("NetworkMode", "")),
            "cap_drop": list(host.get("CapDrop") or []),
            "security_opt": list(host.get("SecurityOpt") or []),
            "pids_limit": host.get("PidsLimit"),
            "memory_bytes": host.get("Memory"),
            "nano_cpus": host.get("NanoCpus"),
            "privileged": bool(host.get("Privileged")),
            "pid_mode": str(host.get("PidMode", "")),
            "mounts": [
                {
                    "type": item.get("Type"),
                    "source": item.get("Source"),
                    "destination": item.get("Destination"),
                    "rw": item.get("RW"),
                }
                for item in mounts
            ],
        }

    def _persist(self, snapshot: SandboxSnapshot) -> None:
        self.store.write_snapshot(snapshot.identity.sandbox_id, snapshot.to_dict())

    def _event(self, sandbox_id: str, event: str, detail: dict) -> None:
        self.store.append_event(
            sandbox_id,
            {
                "schema_version": 1,
                "timestamp": _now(),
                "sandbox_id": sandbox_id,
                "event": event,
                "detail": detail,
            },
        )
