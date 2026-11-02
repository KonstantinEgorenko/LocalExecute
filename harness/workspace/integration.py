'Control Plane -> Workspace session integration for workspace.'
from __future__ import annotations

from pathlib import Path
from typing import Iterable

from ..control_plane.models import WorkContract
from ..control_plane.store import RunStore
from .models import WorkspaceSession
from .session import WorkspaceSessionFactory


class ControlPlaneWorkspaceResolver:
    """Resolve one Run/Todo/WorkContract into a deterministic workspace session."""

    def __init__(
        self,
        run_store: RunStore,
        *,
        workspace_root: str | Path,
        command_prefixes: Iterable[Iterable[str]] = (),
        protected_paths: tuple[str, ...] = ("state", "scripts/swarm_monitor.ps1"),
    ) -> None:
        self.run_store = run_store
        self.workspace_root = Path(workspace_root).resolve(strict=True)
        self.factory = WorkspaceSessionFactory(
            command_prefixes=command_prefixes,
            protected_paths=protected_paths,
        )

    def resolve(self, *, run_id: str, contract: WorkContract) -> WorkspaceSession:
        run = self.run_store.get_run(run_id)
        if run.active_todo_id is None:
            raise ValueError("Run must have an active_todo_id for workspace resolution")
        todo = self.run_store.get_todo(run.active_todo_id)
        return self.factory.create(run=run, todo=todo, contract=contract, root=self.workspace_root)
