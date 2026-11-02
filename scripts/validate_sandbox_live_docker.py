from __future__ import annotations

import json
import subprocess
import tempfile
import time
from pathlib import Path

from harness.sandbox import (
    DockerCli,
    DockerSandboxExecutor,
    DockerSandboxManager,
    JsonSandboxStore,
    SandboxPolicy,
    SandboxIdentityMismatch,
)
from harness.sandbox.security import mount_source_matches_host

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "state/evaluation/phase20b/live_docker.json"


def git(*args, cwd=None):
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        shell=False,
        check=True,
    )


def make_repo(base: Path) -> tuple[Path, Path]:
    repo = base / "repo"
    worktree = base / "worktree"
    repo.mkdir()
    git("init", str(repo))
    git("-C", str(repo), "config", "user.email", "phase20b@example.invalid")
    git("-C", str(repo), "config", "user.name", "Phase20B Live")
    (repo / "README.md").write_text("baseline\n", encoding="utf-8")
    git("-C", str(repo), "add", "README.md")
    git("-C", str(repo), "commit", "-m", "baseline")
    git("-C", str(repo), "worktree", "add", str(worktree), "-b", "phase20b-live")
    return repo, worktree


def main():
    policy = SandboxPolicy.from_project_root(ROOT)
    docker = DockerCli()
    docker.engine_version()

    sandbox_id = f"live-{time.time_ns()}"
    manager = None

    with tempfile.TemporaryDirectory(prefix="swarm-phase20b-") as tmp:
        base = Path(tmp)
        _, worktree = make_repo(base)
        state = base / "state"
        store = JsonSandboxStore(state)
        manager = DockerSandboxManager(
            control_root=ROOT,
            policy=policy,
            docker=docker,
            store=store,
        )
        executor = DockerSandboxExecutor(
            policy=policy,
            docker=docker,
            store=store,
        )

        try:
            first = manager.acquire(
                sandbox_id=sandbox_id,
                lease_id="lease-live",
                worktree=worktree,
                image=policy.live_image,
            )

            result = executor.exec_argv(
                sandbox_id,
                [
                    "python",
                    "-c",
                    (
                        "from pathlib import Path; "
                        "Path('phase20b_probe.txt').write_text("
                        "'written-inside-container\\n', encoding='utf-8'); "
                        "print('sandbox-command-ok')"
                    ),
                ],
            )
            assert result.exit_code == 0, result.stderr
            assert "sandbox-command-ok" in result.stdout
            host_probe = worktree / "phase20b_probe.txt"
            assert host_probe.read_text(encoding="utf-8") == "written-inside-container\n"

            status = git("-C", str(worktree), "status", "--short").stdout
            assert "phase20b_probe.txt" in status

            inspect = docker.container_inspect(first.container_name)
            security = first.security
            assert security["readonly_rootfs"] is True
            assert security["network_mode"] == "none"
            assert "ALL" in [str(x).upper() for x in security["cap_drop"]]
            assert any(
                "no-new-privileges" in str(x).casefold()
                for x in security["security_opt"]
            )
            assert security["privileged"] is False
            assert security["pid_mode"] not in {"host", "HOST"}
            assert security["pids_limit"]
            assert security["memory_bytes"] > 0
            assert security["nano_cpus"] > 0
            mounts = security["mounts"]
            rw_mounts = [item for item in mounts if item.get("rw")]
            assert len(rw_mounts) == 1
            assert rw_mounts[0]["destination"] == "/workspace"
            assert mount_source_matches_host(
                str(rw_mounts[0]["source"]),
                worktree,
            ), (
                f"Docker mount source does not match linked worktree: "
                f"source={rw_mounts[0]['source']!r} worktree={str(worktree)!r}"
            )
            assert not any(
                "docker.sock" in str(item.get("source", "")).casefold()
                or "docker.sock" in str(item.get("destination", "")).casefold()
                for item in mounts
            )

            labels = inspect.get("Config", {}).get("Labels", {}) or {}
            assert labels.get("swarm.phase") == "20b"
            assert labels.get("swarm.sandbox_id") == sandbox_id
            assert labels.get("swarm.lease_id") == "lease-live"

            reused = manager.acquire(
                sandbox_id=sandbox_id,
                lease_id="lease-live",
                worktree=worktree,
                image=policy.live_image,
            )
            assert reused.container_id == first.container_id
            assert reused.restart_count == 0

            manager.stop(sandbox_id)
            restarted = manager.acquire(
                sandbox_id=sandbox_id,
                lease_id="lease-live",
                worktree=worktree,
                image=policy.live_image,
            )
            assert restarted.restart_count == 1

            mismatch_rejected = False
            try:
                manager.acquire(
                    sandbox_id=sandbox_id,
                    lease_id="different-lease",
                    worktree=worktree,
                    image=policy.live_image,
                )
            except SandboxIdentityMismatch:
                mismatch_rejected = True
            assert mismatch_rejected

            payload = {
                "schema_version": 1,
                "status": "PASS",
                "invocation_id": f"phase20b-live-{time.time_ns()}",
                "sandbox_id": sandbox_id,
                "container_name": first.container_name,
                "container_id": first.container_id,
                "image": policy.live_image,
                "image_id": first.image_id,
                "network_mode": security["network_mode"],
                "readonly_rootfs": security["readonly_rootfs"],
                "pids_limit": security["pids_limit"],
                "memory_bytes": security["memory_bytes"],
                "nano_cpus": security["nano_cpus"],
                "rw_mount_count": len(rw_mounts),
                "workspace_mount": rw_mounts[0]["destination"],
                "docker_socket_exposed": False,
                "host_git_detected_change": True,
                "command_exit_code": result.exit_code,
                "restart_count": restarted.restart_count,
                "identity_mismatch_rejected": mismatch_rejected,
            }
            OUT.parent.mkdir(parents=True, exist_ok=True)
            OUT.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

        finally:
            if manager is not None:
                manager.destroy(sandbox_id)

    assert docker.container_inspect(
        f"{policy.name_prefix}{sandbox_id}", required=False
    ) is None

    print("PHASE 20B LIVE WORKTREE-IN-DOCKER: PASS")
    print(f"sandbox_id={payload['sandbox_id']}")
    print(f"container_name={payload['container_name']}")
    print("linked_worktree_rw_mount=PASS")
    print("host_git_change_roundtrip=PASS")
    print("network_none=PASS")
    print("readonly_rootfs=PASS")
    print("cap_drop_all=PASS")
    print("no_new_privileges=PASS")
    print("docker_socket_absent=PASS")
    print("resource_limits=PASS")
    print("reuse=PASS")
    print("bounded_restart=PASS")
    print("identity_mismatch_rejected=PASS")
    print("destroy_cleanup=PASS")
    print(f"report={OUT}")


if __name__ == "__main__":
    main()
