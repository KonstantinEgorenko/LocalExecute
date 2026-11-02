"""Durable projection of Lead work items into the existing artifact store."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from ..context_manager import Artifact, ArtifactKind, ArtifactOrigin, ArtifactStore
from ..control_plane import Quota, Todo, TodoStatus, WorkContract
from ..lead.models import CompiledWorkItem, LeadResult
from ..orchestrator.models import RoutingTaskSpec
from ..workflow.models import WorkflowTodoV1
from ..mission.models import MissionSpecV1, PlanObservationV1
from ..router import (
    Complexity,
    ContextBucket,
    ContextEstimate,
    NeedLevel,
    Scope,
    TaskType,
    ValidationLevel,
)
from .execution_slices import ProductionExecutionSlicePolicy, total_contract


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def filesystem_run_id(goal_id: str, todo_id: str) -> str:
    """Project logical identities into a collision-resistant Windows-safe Run ID."""

    raw = f"{goal_id}-{todo_id}"
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip(".-") or "swarm-run"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return f"{safe[:72]}-{digest}"

def _plan_artifact_id(goal_id: str) -> str:
    return f"production-plan-{goal_id}"


def _work_item_artifact_id(goal_id: str, todo_id: str) -> str:
    return f"production-work-item-{goal_id}-{todo_id}"


def _contract(raw: dict) -> WorkContract:
    quota = raw["quota"]
    return WorkContract(
        goal_id=str(raw["goal_id"]),
        todo_id=str(raw["todo_id"]),
        objective=str(raw["objective"]),
        constraints=raw.get("constraints", {}),
        allowed_scope=tuple(raw.get("allowed_scope", ())),
        forbidden_scope=tuple(raw.get("forbidden_scope", ())),
        required_evidence=tuple(raw.get("required_evidence", ())),
        quota=Quota(
            int(quota["max_worker_calls"]),
            int(quota["max_same_fast_worker_attempts"]),
            int(quota["max_same_heavy_worker_attempts"]),
            int(quota["max_replans"]),
            int(quota["max_escalations"]),
        ),
    )


def _routing(raw: dict) -> RoutingTaskSpec:
    estimate = raw["context_estimate"]
    return RoutingTaskSpec(
        TaskType(str(raw["task_type"])),
        Complexity(str(raw["complexity"])),
        Scope(str(raw["scope"])),
        NeedLevel(str(raw["reasoning_need"])),
        NeedLevel(str(raw["repo_exploration_need"])),
        NeedLevel(str(raw["tool_depth"])),
        ContextEstimate(
            None if estimate.get("estimated_tokens") is None else int(estimate["estimated_tokens"]),
            ContextBucket(str(estimate["bucket"])),
        ),
        ValidationLevel(str(raw["testability"])),
        NeedLevel(str(raw["risk"])),
        float(raw["router_confidence"]),
    )


def _work_item(raw: dict) -> WorkflowTodoV1:
    todo_raw = raw["todo"]
    todo = Todo(
        str(todo_raw["todo_id"]),
        str(todo_raw["goal_id"]),
        str(todo_raw["objective"]),
        TodoStatus(str(todo_raw["status"])),
        tuple(todo_raw.get("dependencies", ())),
        tuple(todo_raw.get("required_gates", ())),
        int(todo_raw.get("priority", 0)),
    )
    contract = _contract(raw["work_contract"])
    routing = _routing(raw["routing_task_spec"])
    return WorkflowTodoV1(
        todo.todo_id,
        filesystem_run_id(todo.goal_id, todo.todo_id),
        todo.objective,
        todo.dependencies,
        contract,
        routing,
    )


@dataclass(slots=True)
class DurableWorkItemRegistry:
    artifact_store: ArtifactStore
    execution_slice_policy: ProductionExecutionSlicePolicy | None = None

    def _ensure(self, artifact: Artifact) -> None:
        if self.artifact_store.has_artifact(artifact.artifact_id):
            existing = self.artifact_store.get_artifact(artifact.artifact_id)
            if existing.sha256 != artifact.sha256:
                raise RuntimeError(f"durable work-item artifact conflict: {artifact.artifact_id}")
            return
        self.artifact_store.create_artifact(artifact)

    def save_plan(
        self,
        spec: MissionSpecV1,
        result: LeadResult,
        observation: PlanObservationV1,
    ) -> None:
        if result.goal is None:
            raise ValueError("Lead result has no Goal")
        for item in result.work_items:
            if not isinstance(item, CompiledWorkItem):
                raise TypeError("Lead result contains non-CompiledWorkItem")
            payload = item.to_dict()
            self._ensure(Artifact.from_text(
                artifact_id=_work_item_artifact_id(spec.goal_id, item.todo.todo_id),
                kind=ArtifactKind.CONTROL_PLANE_EVIDENCE,
                origin=ArtifactOrigin.CONTROL_PLANE,
                content=_json(payload),
                media_type="application/json",
                source="ProductionLeadWorkItem",
                created_at="1970-01-01T00:00:00+00:00",
                scope_tags=(spec.goal_id, item.todo.todo_id),
                metadata={
                    "production_role": "WORK_ITEM",
                    "goal_id": spec.goal_id,
                    "todo_id": item.todo.todo_id,
                },
            ))
        plan_payload = observation.to_dict()
        plan_payload["root_task_sha256"] = spec.root_task_sha256
        self._ensure(Artifact.from_text(
            artifact_id=_plan_artifact_id(spec.goal_id),
            kind=ArtifactKind.CONTROL_PLANE_EVIDENCE,
            origin=ArtifactOrigin.CONTROL_PLANE,
            content=_json(plan_payload),
            media_type="application/json",
            source="ProductionLeadPlan",
            created_at="1970-01-01T00:00:00+00:00",
            scope_tags=(spec.goal_id,),
            metadata={"production_role": "PLAN", "goal_id": spec.goal_id},
        ))

    def load_plan(self, spec: MissionSpecV1) -> PlanObservationV1 | None:
        artifact_id = _plan_artifact_id(spec.goal_id)
        if not self.artifact_store.has_artifact(artifact_id):
            return None
        raw = json.loads(self.artifact_store.get_artifact(artifact_id).content)
        if raw.get("root_task_sha256") != spec.root_task_sha256:
            raise RuntimeError("persisted Lead plan belongs to a different root objective")
        observation = PlanObservationV1.from_dict(raw)
        from .planning_evidence import verify_restored_graph
        from harness.lead.required_graph import REQUIRED_GRAPH
        from harness.scenarios.scope_evidence import PlanningContractError
        from harness.context_manager.errors import ArtifactIntegrityError, ArtifactStoreError
        try:
            verify_restored_graph(self, spec, observation)
        except (ValueError, KeyError, ArtifactIntegrityError, ArtifactStoreError) as exc:
            if REQUIRED_GRAPH not in spec.global_constraints:
                raise
            raise PlanningContractError("PLANNING_REQUIRED_GRAPH_CONTRACT_INVALID", reason=str(exc),
                                        exception_type=type(exc).__name__) from exc
        return observation

    def load_work_items(self, spec: MissionSpecV1) -> tuple[WorkflowTodoV1, ...]:
        plan = self.load_plan(spec)
        if plan is None:
            raise RuntimeError(f"missing durable Lead plan for goal {spec.goal_id}")
        items = []
        for todo_id in plan.todo_ids:
            artifact = self.artifact_store.get_artifact(_work_item_artifact_id(spec.goal_id, todo_id))
            item = _work_item(json.loads(artifact.content))
            if self.execution_slice_policy is not None:
                item = WorkflowTodoV1(
                    item.todo_id,
                    item.run_id,
                    item.objective,
                    item.dependencies,
                    total_contract(item.work_contract, self.execution_slice_policy),
                    item.routing_task_spec,
                )
            items.append(item)
        return tuple(items)

    def load_original_work_item(self, *, goal_id: str, todo_id: str) -> WorkflowTodoV1:
        'Load the immutable Lead work item without execution-time rewrites.\n\n        Recovery may replace ``WorkflowTodoV1.routing_task_spec`` with the\n        execution evidence/15 ``TaskProfileV2`` recovery view. AgentLoop model routing,\n        however, still requires the original ``RoutingTaskSpec`` produced by\n        Lead and stored durably with the work item.\n        '
        artifact = self.artifact_store.get_artifact(_work_item_artifact_id(goal_id, todo_id))
        item = _work_item(json.loads(artifact.content))
        if (
            item.todo_id != todo_id
            or item.work_contract.goal_id != goal_id
            or item.work_contract.todo_id != todo_id
        ):
            raise RuntimeError("durable original work-item identity mismatch")
        return item

    def load_control_todos(self, spec: MissionSpecV1) -> tuple[Todo, ...]:
        plan = self.load_plan(spec)
        if plan is None:
            raise RuntimeError(f"missing durable Lead plan for goal {spec.goal_id}")
        todos = []
        for todo_id in plan.todo_ids:
            artifact = self.artifact_store.get_artifact(_work_item_artifact_id(spec.goal_id, todo_id))
            raw = json.loads(artifact.content)["todo"]
            todos.append(Todo(
                str(raw["todo_id"]),
                str(raw["goal_id"]),
                str(raw["objective"]),
                TodoStatus(str(raw["status"])),
                tuple(raw.get("dependencies", ())),
                tuple(raw.get("required_gates", ())),
                int(raw.get("priority", 0)),
            ))
        return tuple(todos)
