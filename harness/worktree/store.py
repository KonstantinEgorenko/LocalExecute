'Durable create-only/update-by-identity lease store for worker agent.5.'
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .errors import WorktreeConflictError
from .models import WorktreeLease


class FileWorktreeLeaseStore:
    SCHEMA_VERSION = 1

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve(strict=False)
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path = self.root / "index.json"
        self._leases: dict[str, WorktreeLease] = {}
        self._load()

    def _load(self) -> None:
        if not self.index_path.exists():
            return
        try:
            payload = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise WorktreeConflictError(f"invalid worktree lease index: {exc}") from exc
        if not isinstance(payload, dict) or payload.get("schema_version") != self.SCHEMA_VERSION:
            raise WorktreeConflictError("unsupported worktree lease index schema")
        raw = payload.get("leases")
        if not isinstance(raw, list):
            raise WorktreeConflictError("worktree lease index leases must be a list")
        leases: dict[str, WorktreeLease] = {}
        try:
            for item in raw:
                if not isinstance(item, dict):
                    raise ValueError("lease entry must be an object")
                lease = WorktreeLease.from_dict(item)
                if lease.lease_id in leases:
                    raise ValueError("duplicate lease_id")
                leases[lease.lease_id] = lease
        except (KeyError, TypeError, ValueError) as exc:
            raise WorktreeConflictError(f"invalid worktree lease entry: {exc}") from exc
        self._leases = leases

    def _persist(self) -> None:
        payload = {
            "schema_version": self.SCHEMA_VERSION,
            "leases": [self._leases[key].to_dict() for key in sorted(self._leases)],
        }
        data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
        fd, name = tempfile.mkstemp(prefix=".worktree-index-", suffix=".tmp", dir=str(self.root))
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, self.index_path)
        finally:
            try:
                os.unlink(name)
            except FileNotFoundError:
                pass

    def get(self, lease_id: str) -> WorktreeLease | None:
        return self._leases.get(lease_id)

    def all(self) -> tuple[WorktreeLease, ...]:
        return tuple(self._leases[key] for key in sorted(self._leases))

    def put_new(self, lease: WorktreeLease) -> None:
        existing = self._leases.get(lease.lease_id)
        if existing is not None:
            if existing == lease:
                return
            raise WorktreeConflictError(f"lease_id already exists with different payload: {lease.lease_id}")
        self._leases[lease.lease_id] = lease
        self._persist()

    def replace(self, lease: WorktreeLease) -> None:
        if lease.lease_id not in self._leases:
            raise WorktreeConflictError(f"cannot replace unknown lease: {lease.lease_id}")
        self._leases[lease.lease_id] = lease
        self._persist()
