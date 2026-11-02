from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from typing import Sequence


class DockerUnavailable(RuntimeError):
    pass


class DockerCommandError(RuntimeError):
    def __init__(self, argv, returncode, stdout, stderr):
        self.argv = tuple(argv)
        self.returncode = int(returncode)
        self.stdout = stdout
        self.stderr = stderr
        super().__init__(
            f"Docker command failed rc={returncode}: {' '.join(argv)}\n{stderr.strip()}"
        )


@dataclass(frozen=True, slots=True)
class CompletedDockerCommand:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


class DockerCli:
    def __init__(self, executable: str = "docker") -> None:
        path = shutil.which(executable)
        if path is None:
            raise DockerUnavailable("docker CLI not found")
        self.executable = path

    def run(
        self,
        args: Sequence[str],
        *,
        timeout: int = 60,
        check: bool = True,
    ) -> CompletedDockerCommand:
        argv = [self.executable, *[str(x) for x in args]]
        try:
            result = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise DockerCommandError(
                argv, 124, exc.stdout or "", exc.stderr or "timeout"
            ) from exc
        completed = CompletedDockerCommand(
            tuple(argv), result.returncode, result.stdout, result.stderr
        )
        if check and result.returncode != 0:
            raise DockerCommandError(
                argv, result.returncode, result.stdout, result.stderr
            )
        return completed

    def engine_version(self) -> str:
        result = self.run(["version", "--format", "{{.Server.Version}}"], check=False)
        if result.returncode != 0 or not result.stdout.strip():
            probe = self.run(["ps", "--format", "{{.ID}}"], check=False)
            if probe.returncode != 0:
                raise DockerUnavailable("Docker engine is not reachable")
            return "UNKNOWN"
        return result.stdout.strip()

    def image_inspect(self, image: str) -> dict:
        raw = self.run(["image", "inspect", image]).stdout
        data = json.loads(raw)
        if not isinstance(data, list) or not data:
            raise DockerCommandError(
                ["docker", "image", "inspect", image], 1, raw, "invalid inspect payload"
            )
        return data[0]

    def container_inspect(self, name: str, *, required: bool = True) -> dict | None:
        result = self.run(["inspect", name], check=False)
        if result.returncode != 0:
            if required:
                raise DockerCommandError(
                    result.argv, result.returncode, result.stdout, result.stderr
                )
            return None
        data = json.loads(result.stdout)
        if not isinstance(data, list) or not data:
            if required:
                raise DockerCommandError(result.argv, 1, result.stdout, "invalid inspect payload")
            return None
        return data[0]
