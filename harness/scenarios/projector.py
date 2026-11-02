"""Projection from typed scenarios into existing Swarm execution contracts.

Phase 24C remains shadow-only.  Only model-executable IMPLEMENTATION nodes are
projected into CompiledWorkItem.  Deterministic verification and Judge nodes
remain control-plane responsibilities and never become worker Todos.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping

from harness.control_plane import (
    Goal,
    GoalStatus,
    Quota,
    Todo,
    TodoStatus,
    WorkContract,
)
from harness.lead.models import CompiledWorkItem, LeadRequest
from harness.orchestrator.models import RoutingTaskSpec
from harness.router import (
    Complexity,
    ContextBucket,
    ContextEstimate,
    NeedLevel,
    Scope,
    TaskType,
    ValidationLevel,
)

from .models import (
    CompiledScenarioV1,
    ScenarioAmbiguityV1,
    ScenarioNodeKindV1,
    ScenarioRiskV1,
    ScenarioScopeV1,
    ScenarioTypeV1,
)


def _quota_from_policies(policies: Mapping[str, Any]) -> Quota:
    try:
        retry = policies["retry_and_anti_loop"]
        return Quota(
            int(retry["max_total_worker_calls_per_task"]),
            int(retry["max_attempts_same_fast_worker"]),
            int(retry["max_attempts_same_heavy_worker"]),
            int(retry["max_replans_per_task"]),
            int(retry["max_escalations_per_task"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("invalid retry_and_anti_loop policy for scenario projection") from exc


def _need_from_risk(risk: ScenarioRiskV1) -> NeedLevel:
    return {
        ScenarioRiskV1.LOW: NeedLevel.LOW,
        ScenarioRiskV1.MEDIUM: NeedLevel.MEDIUM,
        ScenarioRiskV1.HIGH: NeedLevel.HIGH,
    }[risk]


def _reasoning_from_ambiguity(ambiguity: ScenarioAmbiguityV1) -> NeedLevel:
    return {
        ScenarioAmbiguityV1.LOW: NeedLevel.LOW,
        ScenarioAmbiguityV1.MEDIUM: NeedLevel.MEDIUM,
        ScenarioAmbiguityV1.HIGH: NeedLevel.HIGH,
    }[ambiguity]


def _scope(scope: ScenarioScopeV1) -> Scope:
    return {
        ScenarioScopeV1.LOCAL: Scope.LOCAL,
        ScenarioScopeV1.SINGLE_FILE: Scope.LOCAL,
        ScenarioScopeV1.MULTI_FILE: Scope.MULTI_FILE,
        ScenarioScopeV1.REPOSITORY: Scope.REPOSITORY,
    }[scope]


def _context_bucket(scope: ScenarioScopeV1) -> ContextBucket:
    return {
        ScenarioScopeV1.LOCAL: ContextBucket.SMALL,
        ScenarioScopeV1.SINGLE_FILE: ContextBucket.SMALL,
        ScenarioScopeV1.MULTI_FILE: ContextBucket.MEDIUM,
        ScenarioScopeV1.REPOSITORY: ContextBucket.LARGE,
    }[scope]


def _task_type(scenario: ScenarioTypeV1) -> TaskType:
    if scenario is ScenarioTypeV1.BUGFIX_WITH_REGRESSION:
        return TaskType.BUGFIX
    if scenario in {
        ScenarioTypeV1.SIMPLE_MUTATION,
        ScenarioTypeV1.MULTI_FILE_CHANGE,
    }:
        return TaskType.CODE
    raise ValueError(
        f"ScenarioPlanProjectorV1 cannot route implementation for {scenario.value}"
    )


@dataclass(frozen=True, slots=True)
class ScenarioProjectionV1:
    goal: Goal
    work_items: tuple[CompiledWorkItem, ...]
    control_node_ids: tuple[str, ...]
    scenario_fingerprint: str

    def __post_init__(self) -> None:
        if not isinstance(self.goal, Goal):
            raise ValueError("goal must be Goal")
        object.__setattr__(self, "work_items", tuple(self.work_items))
        object.__setattr__(self, "control_node_ids", tuple(self.control_node_ids))
        if any(not isinstance(item, CompiledWorkItem) for item in self.work_items):
            raise ValueError("work_items must contain CompiledWorkItem")
        if not self.work_items:
            raise ValueError("scenario projection requires model-executable work")
        if not self.scenario_fingerprint:
            raise ValueError("scenario_fingerprint must be non-empty")

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal.to_dict(),
            "work_items": [item.to_dict() for item in self.work_items],
            "control_node_ids": list(self.control_node_ids),
            "scenario_fingerprint": self.scenario_fingerprint,
        }

    @property
    def fingerprint(self) -> str:
        canonical = json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ScenarioPlanProjectorV1:
    """Project deterministic scenario semantics into current execution contracts."""

    def project(
        self,
        *,
        scenario: CompiledScenarioV1,
        request: LeadRequest,
        goal_id: str,
        policies: Mapping[str, Any],
        required_gates: tuple[str, ...],
    ) -> ScenarioProjectionV1:
        if not isinstance(scenario, CompiledScenarioV1):
            raise TypeError("scenario must be CompiledScenarioV1")
        if not isinstance(request, LeadRequest):
            raise TypeError("request must be LeadRequest")
        if not isinstance(goal_id, str) or not goal_id.strip():
            raise ValueError("goal_id must be a non-empty string")
        if scenario.selection.objective != request.objective:
            raise ValueError("scenario objective must exactly match parent request objective")

        implementation_nodes = tuple(
            node
            for node in scenario.nodes
            if node.kind is ScenarioNodeKindV1.IMPLEMENTATION
        )
        if len(implementation_nodes) != 1:
            raise ValueError(
                "Phase 24C supports exactly one IMPLEMENTATION node"
            )
        implementation = implementation_nodes[0]
        if implementation.dependencies:
            raise ValueError(
                "Phase 24C implementation node must be the model-executable root"
            )

        control_nodes = tuple(
            node.node_id
            for node in scenario.nodes
            if node.kind in {
                ScenarioNodeKindV1.DETERMINISTIC_VERIFICATION,
                ScenarioNodeKindV1.JUDGE,
            }
        )
        if not control_nodes:
            raise ValueError("scenario projection requires control-plane nodes")

        quota = _quota_from_policies(policies)
        from ..control_plane.todo_identity import make_todo_id
        todo_id = make_todo_id(goal_id, 1)
        required_gates = tuple(dict.fromkeys(str(gate) for gate in required_gates))
        if not required_gates:
            raise ValueError("scenario projection requires deterministic gates")
        if scenario.selection.requires_regression_change:
            from harness.verification.models import BuiltinGate
            required_gates = tuple(dict.fromkeys((*required_gates, BuiltinGate.REGRESSION_CHANGE.value)))

        goal = Goal(
            goal_id,
            request.objective,
            GoalStatus.ACTIVE,
            "scenario-compiler-v1",
            metadata={
                "scenario_type": scenario.selection.scenario.value,
                "scenario_fingerprint": scenario.fingerprint,
            },
        )
        todo = Todo(
            todo_id,
            goal_id,
            request.objective,
            TodoStatus.READY,
            (),
            required_gates,
            0,
        )

        constraints = dict(request.constraints)
        scenario_constraints = {
            "TYPED_SCENARIO_EXECUTION": True,
            "SCENARIO_TYPE": scenario.selection.scenario.value,
            "SCENARIO_NODE_ID": implementation.node_id,
            "REQUIRES_WORKSPACE_MUTATION": True,
            "REQUIRES_REGRESSION_CHANGE": scenario.selection.requires_regression_change,
            # Hints remain non-authoritative. They are deliberately not projected
            # to allowed_scope.
            "SCENARIO_TARGET_HINTS": list(scenario.selection.target_hints),
            "SCENARIO_SCOPE": scenario.selection.scope.value,
            "SCENARIO_RISK": scenario.selection.risk.value,
            "SCENARIO_AMBIGUITY": scenario.selection.ambiguity.value,
        }
        for key, value in scenario_constraints.items():
            if key in constraints and constraints[key] != value:
                raise ValueError(f"scenario constraint conflicts with parent: {key}")
            constraints[key] = value

        evidence = ("WORKER_RESULT", "CANDIDATE_DIFF")
        contract = WorkContract(
            goal_id,
            todo_id,
            request.objective,
            constraints,
            request.allowed_scope,
            request.forbidden_scope,
            evidence,
            quota,
        )

        risk = _need_from_risk(scenario.selection.risk)
        ambiguity = _reasoning_from_ambiguity(scenario.selection.ambiguity)
        routing = RoutingTaskSpec(
            task_type=_task_type(scenario.selection.scenario),
            complexity=(
                Complexity.HARD
                if scenario.selection.risk is ScenarioRiskV1.HIGH
                else Complexity.NORMAL
            ),
            scope=_scope(scenario.selection.scope),
            reasoning_need=ambiguity,
            repo_exploration_need=(
                NeedLevel.MEDIUM
                if not scenario.selection.target_hints
                else NeedLevel.LOW
            ),
            tool_depth=NeedLevel.MEDIUM,
            context_estimate=ContextEstimate(
                None,
                _context_bucket(scenario.selection.scope),
            ),
            testability=ValidationLevel.HIGH,
            risk=risk,
            router_confidence=1.0,
        )

        item = CompiledWorkItem(todo, contract, routing)
        return ScenarioProjectionV1(
            goal=goal,
            work_items=(item,),
            control_node_ids=control_nodes,
            scenario_fingerprint=scenario.fingerprint,
        )
