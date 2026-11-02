"""Compile a Control Plane WorkContract into a bounded workspace session."""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

from ..control_plane.models import Run, Todo, WorkContract
from .models import WorkspaceAccess, WorkspacePolicy, WorkspaceSession


class WorkspaceSessionFactory:
    def __init__(
        self,
        *,
        command_prefixes: Iterable[Iterable[str]] = (),
        protected_paths: tuple[str, ...] = ("state", "scripts/swarm_monitor.ps1"),
    ) -> None:
        self.command_prefixes = tuple(tuple(value) for value in command_prefixes)
        self.protected_paths = protected_paths

    def create(self, *, run: Run, todo: Todo, contract: WorkContract, root: str | Path) -> WorkspaceSession:
        if run.goal_id != todo.goal_id or contract.goal_id != todo.goal_id:
            raise ValueError("Run/Todo/WorkContract goal identity mismatch")
        if run.active_todo_id != todo.todo_id or contract.todo_id != todo.todo_id:
            raise ValueError("Run/Todo/WorkContract Todo identity mismatch")
        root_path = Path(root).resolve(strict=True)
        if not root_path.is_dir():
            raise ValueError("workspace root must be an existing directory")
        constraints = contract.constraints
        read_only = bool(constraints.get("READ_ONLY", False) or constraints.get("NO_WRITE", False))
        no_commands = bool(constraints.get("NO_COMMANDS", False))
        policy = WorkspacePolicy(
            access=WorkspaceAccess.READ_ONLY if read_only else WorkspaceAccess.READ_WRITE,
            allowed_paths=contract.allowed_scope,
            forbidden_paths=tuple(dict.fromkeys((".git", *contract.forbidden_scope))),
            protected_paths=self.protected_paths,
            command_prefixes=() if no_commands else self.command_prefixes,
        )
        return WorkspaceSession(
            session_id=f"{run.run_id}:workspace",
            run_id=run.run_id,
            todo_id=todo.todo_id,
            root=str(root_path),
            policy=policy,
        )
