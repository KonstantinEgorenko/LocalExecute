'mission ownership-preserving dependency-injection ports.'
from __future__ import annotations

from typing import Protocol

from ..workflow.models import WorkflowResultV1
from .models import (
    DagAmendmentV1,
    GlobalCompletionGateV1,
    MissionCheckpointV1,
    MissionSpecV1,
    PlanObservationV1,
)


class MissionPlanningPort(Protocol):
    def ensure_initial_plan(self, spec: MissionSpecV1) -> PlanObservationV1: ...


class WorkflowExecutionPort(Protocol):
    def run(self, spec: MissionSpecV1) -> WorkflowResultV1: ...


class DagAmendmentPort(Protocol):
    def propose(
        self,
        spec: MissionSpecV1,
        checkpoint: MissionCheckpointV1,
        workflow_result: WorkflowResultV1,
    ) -> DagAmendmentV1 | None: ...

    def apply(self, amendment: DagAmendmentV1) -> None:
        """Ensure amendment is applied; repeated amendment_id calls MUST be idempotent."""
        ...


class GlobalCompletionPort(Protocol):
    def evaluate(
        self,
        spec: MissionSpecV1,
        checkpoint: MissionCheckpointV1,
        workflow_result: WorkflowResultV1,
    ) -> GlobalCompletionGateV1: ...

# Verified compatibility aliases for historical Python interfaces.
Phase16WorkflowPort = WorkflowExecutionPort
