'workflow dependency-injection ports.\n\nThese ports deliberately preserve the frozen ownership boundaries. The workflow\ncontroller coordinates; it does not become the scheduler, router, verifier,\ncritic/judge, recovery reasoner, workspace dispatcher, or model runtime.\n'
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..context_manager import ArtifactRef
from .models import (
    ExecutionObservationV1,
    ProfileSelectionV1,
    ProjectSnapshotV1,
    RecoveryObservationV1,
    ReviewObservationV1,
    VerificationObservationV1,
    WorkflowTodoV1,
)


@dataclass(frozen=True, slots=True)
class RepositoryContextV1:
    artifact_refs: tuple[ArtifactRef, ...]
    known_target_files: tuple[str, ...] = ()
    retrieval_id: str | None = None


class SchedulerPort(Protocol):
    def ready(self, goal_id: str, *, limit: int) -> tuple[WorkflowTodoV1, ...]: ...
    def begin(self, todo: WorkflowTodoV1) -> None: ...
    def mark_succeeded(self, todo: WorkflowTodoV1, verification: VerificationObservationV1, outcome_artifact_id: str) -> None: ...
    def mark_retryable(self, todo: WorkflowTodoV1, reason: str) -> None: ...
    def mark_failed(self, todo: WorkflowTodoV1, reason: str) -> None: ...
    def all_succeeded(self, goal_id: str) -> bool: ...
    def terminal_reason(self, goal_id: str) -> str | None: ...


class RepositoryContextPort(Protocol):
    def retrieve(
        self,
        todo: WorkflowTodoV1,
        *,
        objective: str,
        expansion_queries: tuple[str, ...] = (),
    ) -> RepositoryContextV1: ...


class RouterPort(Protocol):
    def select(
        self,
        todo: WorkflowTodoV1,
        repository: RepositoryContextV1,
        *,
        objective: str,
        failed_profile_ids: tuple[str, ...],
    ) -> ProfileSelectionV1: ...


class ExecutionPort(Protocol):
    def execute(
        self,
        todo: WorkflowTodoV1,
        *,
        profile_id: str,
        objective_with_context: str,
        attempt_index: int,
        fresh_workspace: bool,
    ) -> ExecutionObservationV1: ...


class VerificationPort(Protocol):
    def verify(
        self,
        todo: WorkflowTodoV1,
        execution: ExecutionObservationV1,
    ) -> VerificationObservationV1: ...


class ReviewPort(Protocol):
    def review(
        self,
        todo: WorkflowTodoV1,
        execution: ExecutionObservationV1,
        verification: VerificationObservationV1,
    ) -> ReviewObservationV1: ...


class RecoveryPort(Protocol):
    def recover(
        self,
        todo: WorkflowTodoV1,
        *,
        objective: str,
        execution: ExecutionObservationV1,
        verification: VerificationObservationV1,
        failed_profile_ids: tuple[str, ...],
        recovery_cycle: int,
    ) -> RecoveryObservationV1: ...


class SnapshotPort(Protocol):
    def current(self, *, goal_id: str, epoch: int) -> ProjectSnapshotV1: ...


class FinalSynthesisPort(Protocol):
    def synthesize(self, *, goal_id: str, completed_todo_ids: tuple[str, ...]) -> str: ...
