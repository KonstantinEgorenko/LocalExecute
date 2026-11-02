'Thin bridge from mission mission control to the frozen workflow controller.'
from __future__ import annotations

from ..workflow.controller import AutonomousWorkflowController
from ..workflow.models import WorkflowResultV1
from .models import MissionSpecV1


class WorkflowExecutionBridge:
    def __init__(self, controller: AutonomousWorkflowController) -> None:
        self.controller = controller

    def run(self, spec: MissionSpecV1) -> WorkflowResultV1:
        return self.controller.run(
            goal_id=spec.goal_id,
            raw_objective=spec.raw_objective,
            goal_capsule=spec.goal_capsule,
            global_constraints=dict(spec.global_constraints),
        )

# Verified legacy import/pickle compatibility.
Phase16WorkflowBridge = WorkflowExecutionBridge
