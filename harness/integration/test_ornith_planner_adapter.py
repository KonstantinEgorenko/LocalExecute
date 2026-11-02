from __future__ import annotations

from harness.context_manager import FileArtifactStore
from harness.control_plane import Goal, GoalStatus, Quota, Todo, TodoStatus, WorkContract
from harness.integration.ornith_planner_adapter import OrnithPlanningAdapter
from harness.integration.work_item_registry import DurableWorkItemRegistry
from harness.lead.models import CompiledWorkItem, LeadOutcome, LeadRequest, LeadResult
from harness.orchestrator.models import RoutingTaskSpec
from harness.mission.models import MissionSpecV1
from harness.router import (
    Complexity,
    ContextBucket,
    ContextEstimate,
    NeedLevel,
    Scope,
    TaskType,
    ValidationLevel,
)


def _work_item(goal_id: str) -> CompiledWorkItem:
    contract = WorkContract(
        goal_id,
        "todo-1",
        "Implement the production entrypoint",
        {},
        ("harness",),
        (".git",),
        ("WORKER_RESULT",),
        Quota(4, 2, 1, 1, 1),
    )
    todo = Todo(
        "todo-1",
        goal_id,
        contract.objective,
        TodoStatus.PENDING,
        (),
        (),
        10,
    )
    routing = RoutingTaskSpec(
        TaskType.CODE,
        Complexity.NORMAL,
        Scope.MULTI_FILE,
        NeedLevel.MEDIUM,
        NeedLevel.MEDIUM,
        NeedLevel.MEDIUM,
        ContextEstimate(2400, ContextBucket.MEDIUM),
        ValidationLevel.HIGH,
        NeedLevel.LOW,
        0.9,
    )
    return CompiledWorkItem(todo, contract, routing)


def test_adapter_uses_real_mission_and_lead_contracts_and_persists_plan(tmp_path):
    spec = MissionSpecV1(
        "mission-real-contract",
        "goal-real-contract",
        "Implement production Swarm",
        "Keep deterministic routing",
        {},
    )
    item = _work_item(spec.goal_id)
    result = LeadResult(
        LeadOutcome.PLAN_READY,
        Goal(spec.goal_id, spec.raw_objective, GoalStatus.ACTIVE, "ornith"),
        (item,),
    )

    class Lead:
        calls = []

        def decompose(self, *, request, goal_id):
            self.calls.append((request, goal_id))
            return result

    lead = Lead()
    registry = DurableWorkItemRegistry(FileArtifactStore(tmp_path / "artifacts"))
    adapter = OrnithPlanningAdapter(
        ornith_lead=lead,
        request_factory=lambda mission: LeadRequest(mission.raw_objective),
        plan_registry=registry,
        required_gates=("candidate_diff", "scope_guard", "python_syntax"),
    )

    plan = adapter.ensure_initial_plan(spec)

    assert plan.plan_id == spec.goal_id
    assert plan.todo_ids == ("todo-1",)
    assert lead.calls[0][1] == spec.goal_id
    assert isinstance(lead.calls[0][0], LeadRequest)
    assert registry.load_plan(spec) == plan
    persisted = registry.load_work_items(spec)[0]
    assert persisted.todo_id == "todo-1"
    control_todo = registry.load_control_todos(spec)[0]
    assert control_todo.required_gates == ("candidate_diff", "scope_guard", "python_syntax")


def test_adapter_reuses_durable_plan_without_a_second_lead_call(tmp_path):
    spec = MissionSpecV1("mission", "goal", "Implement", "Implement", {})
    item = _work_item(spec.goal_id)
    result = LeadResult(
        LeadOutcome.PLAN_READY,
        Goal(spec.goal_id, spec.raw_objective, GoalStatus.ACTIVE, "ornith"),
        (item,),
    )

    class Lead:
        calls = 0

        def decompose(self, **_kwargs):
            self.calls += 1
            return result

    lead = Lead()
    registry = DurableWorkItemRegistry(FileArtifactStore(tmp_path / "artifacts"))
    adapter = OrnithPlanningAdapter(lead, lambda mission: LeadRequest(mission.raw_objective), registry)

    first = adapter.ensure_initial_plan(spec)
    second = adapter.ensure_initial_plan(spec)

    assert first == second
    assert lead.calls == 1
