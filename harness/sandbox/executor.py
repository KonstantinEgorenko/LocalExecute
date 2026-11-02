from __future__ import annotations

import subprocess
import time
from datetime import datetime, timezone
from typing import Sequence

from .docker_cli import DockerCli
from .models import SandboxCommandResult
from .policy import SandboxPolicy
from .security import sanitize_sandbox_id
from .store import JsonSandboxStore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DockerSandboxExecutor:
    def __init__(
        self,
        *,
        policy: SandboxPolicy,
        docker: DockerCli,
        store: JsonSandboxStore,
    ) -> None:
        self.policy = policy
        self.docker = docker
        self.store = store

    def exec_argv(
        self,
        sandbox_id: str,
        argv: Sequence[str],
        *,
        timeout_seconds: int | None = None,
    ) -> SandboxCommandResult:
        safe_id = sanitize_sandbox_id(sandbox_id)
        command = tuple(str(x) for x in argv)
        if not command:
            raise ValueError("argv must not be empty")
        if any("\x00" in item for item in command):
            raise ValueError("argv contains NUL byte")

        container = f"{self.policy.name_prefix}{safe_id}"
        timeout = timeout_seconds or self.policy.command_timeout_seconds
        docker_argv = [
            self.docker.executable,
            "exec",
            "--workdir",
            self.policy.workdir,
            container,
            *command,
        ]
        started = _now()
        monotonic = time.monotonic()
        timed_out = False
        try:
            completed = subprocess.run(
                docker_argv,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
                timeout=timeout,
                check=False,
            )
            exit_code = int(completed.returncode)
            stdout = completed.stdout[: self.policy.max_stdout_chars]
            stderr = completed.stderr[: self.policy.max_stderr_chars]
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            exit_code = 124
            stdout = str(exc.stdout or "")[: self.policy.max_stdout_chars]
            stderr = str(exc.stderr or "command timeout")[: self.policy.max_stderr_chars]
        duration_ms = int((time.monotonic() - monotonic) * 1000)
        result = SandboxCommandResult(
            sandbox_id=safe_id,
            argv=command,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            started_at=started,
            finished_at=_now(),
            duration_ms=duration_ms,
            timed_out=timed_out,
        )
        self.store.append_event(
            safe_id,
            {
                "schema_version": 1,
                "timestamp": result.finished_at,
                "sandbox_id": safe_id,
                "event": "COMMAND",
                "detail": result.to_dict(),
            },
        )
        return result
