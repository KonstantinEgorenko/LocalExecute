"""Diagnosable process-local locks for future active inference work."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from threading import Lock
from typing import Iterator

from ..errors import LockBusyError, LockOwnershipError


@dataclass(frozen=True, slots=True)
class LockState:
    locked: bool
    owner: str | None


class OwnedLock:
    """Non-reentrant logical-owner lock; acquisition is non-blocking by default."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._state_guard = Lock()
        self._owner: str | None = None

    @property
    def state(self) -> LockState:
        with self._state_guard:
            return LockState(self._owner is not None, self._owner)

    def acquire(self, owner: str, *, timeout: float = 0.0) -> bool:
        """Try to acquire without waiting unless a finite timeout is supplied."""
        if not owner:
            raise ValueError("Lock owner must be a non-empty logical model ID")
        if timeout < 0:
            raise ValueError("Lock timeout must be non-negative")
        with self._state_guard:
            if self._owner == owner:
                return False
        acquired = self._lock.acquire(blocking=False) if timeout == 0 else self._lock.acquire(timeout=timeout)
        if acquired:
            with self._state_guard:
                self._owner = owner
        return acquired

    def release(self, owner: str) -> None:
        with self._state_guard:
            if self._owner != owner:
                raise LockOwnershipError(f"Lock is owned by {self._owner!r}, not {owner!r}")
            self._owner = None
        self._lock.release()

    @contextmanager
    def held(self, owner: str, *, timeout: float = 0.0) -> Iterator[None]:
        if not self.acquire(owner, timeout=timeout):
            raise LockBusyError(f"Lock is already owned by {self.state.owner!r}")
        try:
            yield
        finally:
            self.release(owner)


class ResourceLocks:
    'configuration runtime locks describe ACTIVE work, never whether a model is loaded.'

    def __init__(self) -> None:
        self.gpu_worker_lock = OwnedLock()
        self.cpu_bandwidth_lock = OwnedLock()