"""Pure TaskProfile assembly with caller-supplied fresh snapshots."""
from __future__ import annotations
from ..resource_manager.snapshot import ResourceSnapshot
from ..router.profile import AttemptFailure, TaskProfile
from .models import RoutingTaskSpec

class TaskProfileFactory:
    def build(self, spec: RoutingTaskSpec, snapshot: ResourceSnapshot, failures: tuple[AttemptFailure,...]) -> TaskProfile:
        return TaskProfile(spec.task_type,spec.complexity,spec.scope,spec.reasoning_need,spec.repo_exploration_need,
            spec.tool_depth,spec.context_estimate,spec.testability,spec.risk,spec.router_confidence,failures,snapshot)