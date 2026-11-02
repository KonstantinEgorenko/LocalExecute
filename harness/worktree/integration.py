'Control Plane -> isolated Git worktree -> workspace WorkspaceSession integration.'
from __future__ import annotations

from pathlib import Path
from typing import Iterable

from ..control_plane.models import WorkContract
from ..control_plane.store import RunStore
from ..workspace.models import WorkspaceSession
from ..workspace.session import WorkspaceSessionFactory
from .errors import WorktreePolicyError
from .manager import GitWorktreeManager
from .models import WorktreeLease


class ControlPlaneIsolatedWorkspaceResolver:
    'Resolve a Run into a durable isolated worktree-backed workspace session.\n\n    Model-issued RUN_COMMAND is denied by default. verification deterministic gates may\n    execute trusted commands outside the model tool channel.\n    '

    def __init__(
        self,
        run_store: RunStore,
        *,
        manager: GitWorktreeManager,
        source_repo_root: str | Path,
        command_prefixes: Iterable[Iterable[str]] = (),
        protected_paths: tuple[str, ...] = ("state", "scripts/swarm_monitor.ps1"),
    ) -> None:
        self.run_store = run_store
        self.manager = manager
        self.source_repo_root = Path(source_repo_root).expanduser().resolve(strict=True)
        if command_prefixes and not manager.policy.allow_model_commands:
            raise WorktreePolicyError("model command prefixes require WorktreePolicy.allow_model_commands=True")
        self.factory = WorkspaceSessionFactory(
            command_prefixes=tuple(tuple(value) for value in command_prefixes) if manager.policy.allow_model_commands else (),
            protected_paths=protected_paths,
        )
        self._leases_by_run: dict[str, str] = {}

    def acquire_for_run(self, *, run_id: str, contract: WorkContract, base_ref: str = "HEAD") -> WorktreeLease:
        run = self.run_store.get_run(run_id)
        if run.active_todo_id is None:
            raise ValueError("Run must have an active_todo_id for isolated workspace resolution")
        todo = self.run_store.get_todo(run.active_todo_id)
        if todo.todo_id != contract.todo_id:
            raise ValueError("active Todo and WorkContract mismatch")
        lease = self.manager.acquire(
            source_repo_root=self.source_repo_root,
            run_id=run_id,
            todo_id=todo.todo_id,
            base_ref=base_ref,
        )
        self._leases_by_run[run_id] = lease.lease_id
        return lease

    def resolve(self, *, run_id: str, contract: WorkContract) -> WorkspaceSession:
        lease_id = self._leases_by_run.get(run_id)
        if lease_id is None:
            lease = self.acquire_for_run(run_id=run_id, contract=contract)
        else:
            lease = self.manager.get_active(lease_id)
        run = self.run_store.get_run(run_id)
        todo = self.run_store.get_todo(run.active_todo_id) if run.active_todo_id else None
        if todo is None:
            raise ValueError("Run must have an active Todo")
        session = self.factory.create(
            run=run,
            todo=todo,
            contract=contract,
            root=lease.worktree_root,
        )
        # Defense in depth: isolated agent sessions do not inherit command execution by default.
        if not self.manager.policy.allow_model_commands and session.policy.command_prefixes:
            raise WorktreePolicyError("isolated workspace unexpectedly enabled model commands")
        return session

    def lease_for_run(self, run_id: str) -> WorktreeLease:
        lease_id = self._leases_by_run.get(run_id)
        if lease_id is None:
            raise WorktreePolicyError("no isolated worktree lease is associated with run")
        return self.manager.get_active(lease_id)
