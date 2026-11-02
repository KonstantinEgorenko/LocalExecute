"""Cross-process, same-thread-reentrant GPU_HEAVY lease.

The OS byte-range/file lock is authoritative. Owner JSON is diagnostic only.
The lock is released by the OS when a process exits/crashes because the file
handle is closed by process teardown.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


class GpuHeavyLeaseError(RuntimeError):
    'Base gpu coordination lease failure.'


class GpuHeavyLeaseTimeout(GpuHeavyLeaseError):
    """The machine-wide GPU_HEAVY lease could not be acquired in time."""


@dataclass(frozen=True, slots=True)
class GpuHeavyStatus:
    locked: bool
    owner: str | None
    owner_pid: int | None
    owner_process: str | None
    metadata_stale: bool = False


class _OsFileLock:
    def __init__(self, path: Path, *, poll_interval_seconds: float) -> None:
        self.path = Path(path)
        self.poll_interval_seconds = poll_interval_seconds
        self.handle = None

    def acquire(self, timeout_seconds: float) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        self.handle.seek(0, os.SEEK_END)
        if self.handle.tell() == 0:
            self.handle.write(b"0")
            self.handle.flush()
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    self.handle.seek(0)
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except OSError as exc:
                if time.monotonic() >= deadline:
                    self.handle.close()
                    self.handle = None
                    raise GpuHeavyLeaseTimeout(
                        f"GPU_HEAVY lease timeout after {timeout_seconds:.3f}s: {self.path}"
                    ) from exc
                time.sleep(min(self.poll_interval_seconds, max(0.0, deadline - time.monotonic())))

    def release(self) -> None:
        if self.handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None


class GpuHeavyLease:
    """Machine-wide GPU_HEAVY lease with reentrancy for one Python thread."""

    def __init__(self, path: str | Path, *, poll_interval_seconds: float = 0.05) -> None:
        self.path = Path(path)
        self.owner_path = self.path.with_suffix(self.path.suffix + ".owner.json")
        self.poll_interval_seconds = float(poll_interval_seconds)
        if self.poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be > 0")
        self._thread_gate = threading.RLock()
        self._local = threading.local()
        self._state_guard = threading.Lock()
        self._active_owner: str | None = None
        self._active_owner_pid: int | None = None
        self._os_lock: _OsFileLock | None = None

    @property
    def held_by_current_thread(self) -> bool:
        return int(getattr(self._local, "depth", 0)) > 0

    @property
    def active_owner(self) -> str | None:
        with self._state_guard:
            return self._active_owner

    def _write_owner(self, owner: str) -> None:
        payload = {
            "schema_version": 1,
            "owner": owner,
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "process": Path(sys.executable).name,
            "argv": list(sys.argv),
            "acquired_at_unix": time.time(),
            "lock_path": str(self.path),
        }
        self.owner_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=self.owner_path.name + ".", suffix=".tmp", dir=self.owner_path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, sort_keys=True, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.owner_path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def _read_owner(self) -> tuple[str | None, int | None, str | None]:
        try:
            raw = json.loads(self.owner_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None, None, None
        owner = raw.get("owner") if isinstance(raw, dict) else None
        pid = raw.get("pid") if isinstance(raw, dict) else None
        process = raw.get("process") if isinstance(raw, dict) else None
        return (
            owner if isinstance(owner, str) and owner else None,
            pid if isinstance(pid, int) and not isinstance(pid, bool) else None,
            process if isinstance(process, str) and process else None,
        )

    def _remove_owner(self) -> None:
        try:
            self.owner_path.unlink()
        except FileNotFoundError:
            pass

    @contextmanager
    def held(self, owner: str, *, timeout_seconds: float) -> Iterator[None]:
        if not isinstance(owner, str) or not owner.strip():
            raise ValueError("owner must be non-empty")
        if timeout_seconds < 0:
            raise ValueError("timeout_seconds must be >= 0")
        started = time.monotonic()
        if not self._thread_gate.acquire(timeout=timeout_seconds):
            raise GpuHeavyLeaseTimeout(
                f"GPU_HEAVY process-local gate timeout after {timeout_seconds:.3f}s"
            )
        outermost = int(getattr(self._local, "depth", 0)) == 0
        os_acquired = False
        try:
            if outermost:
                remaining = max(0.0, timeout_seconds - (time.monotonic() - started))
                os_lock = _OsFileLock(self.path, poll_interval_seconds=self.poll_interval_seconds)
                os_lock.acquire(remaining)
                self._os_lock = os_lock
                os_acquired = True
                self._remove_owner()  # discard metadata left by a crashed previous owner
                self._write_owner(owner)
                with self._state_guard:
                    self._active_owner = owner
                    self._active_owner_pid = os.getpid()
            self._local.depth = int(getattr(self._local, "depth", 0)) + 1
            yield
        finally:
            try:
                depth = int(getattr(self._local, "depth", 0))
                if depth > 0:
                    self._local.depth = depth - 1
                # Only an acquired OS lock authorizes touching owner metadata.
                if outermost and os_acquired:
                    try:
                        self._remove_owner()
                    finally:
                        with self._state_guard:
                            self._active_owner = None
                            self._active_owner_pid = None
                        os_lock = self._os_lock
                        self._os_lock = None
                        if os_lock is not None:
                            os_lock.release()
            finally:
                self._thread_gate.release()

    def status(self) -> GpuHeavyStatus:
        with self._state_guard:
            if self._active_owner is not None:
                return GpuHeavyStatus(True, self._active_owner, self._active_owner_pid, Path(sys.executable).name, False)
        owner, pid, process = self._read_owner()
        probe = _OsFileLock(self.path, poll_interval_seconds=self.poll_interval_seconds)
        try:
            probe.acquire(0.0)
        except GpuHeavyLeaseTimeout:
            return GpuHeavyStatus(True, owner, pid, process, False)
        else:
            probe.release()
            return GpuHeavyStatus(False, owner, pid, process, owner is not None or pid is not None)
