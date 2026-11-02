"""Small trusted Git CLI boundary used only by the worktree manager."""
from __future__ import annotations

import subprocess
from pathlib import Path

from .errors import GitRepositoryError


class GitClient:
    def __init__(self, *, executable: str = "git", timeout_seconds: int = 60) -> None:
        self.executable = executable
        self.timeout_seconds = timeout_seconds

    def run(self, cwd: str | Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        try:
            completed = subprocess.run(
                [self.executable, "-C", str(Path(cwd)), *args],
                text=True,
                encoding="utf-8",
                errors="strict",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                timeout=self.timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired, UnicodeError) as exc:
            raise GitRepositoryError(f"git invocation failed: {type(exc).__name__}: {exc}") from exc
        if check and completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise GitRepositoryError(f"git {' '.join(args)} failed ({completed.returncode}): {detail}")
        return completed

    def text(self, cwd: str | Path, *args: str) -> str:
        return self.run(cwd, *args).stdout.strip()
