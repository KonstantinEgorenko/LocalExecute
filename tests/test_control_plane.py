from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, fields

import pytest

from harness.control_plane import (
    AttentionItem, AttentionSeverity, AttentionStatus, AttemptRecord, Evidence,
    EvidenceStatus, Gate, GateStatus, Goal, GoalStatus, Handoff, Quota,
    QuotaUsage, Run, RunStatus, Todo, TodoStatus, WorkContract,
)
from harness.executor import ExecutionOutcome, ExecutionStage
from harness.router.scoring import DecisionType, RoutingDecision


def quota() -> Quota:
    return Quota(4, 2, 1, 3, 2)


def decision() -> RoutingDecision:
    return RoutingDecision(DecisionType.WORKER, "qwen35_heavy_fast", 7, 0.9, "test", (), {"mode": "LOAD_DIRECT"}, None)


def contracts() -> tuple[object, ...]:
    goal = Goal("goal-1", "Implement a safe change", GoalStatus.ACTIVE, "2026-08-13T00:00:00+00:00", {"labels": ["core"]})
    todo = Todo("todo-1", goal.goal_id, "Write tests", TodoStatus.READY, ["todo-0"], ["gate-tests"], 3)
    work = WorkContract(goal.goal_id, todo.todo_id, todo.objective, {"paths": ["harness/"]}, ["repository"], ["global-config"], ["pytest"], quota())
    run = Run("run-1", goal.goal_id, todo.todo_id, RunStatus.RUNNING, QuotaUsage(1, 0, 0, {"qwen35_heavy_fast": 1}))
    attempt = AttemptRecord(0, todo.todo_id, decision(), "qwen35_heavy_fast", ExecutionOutcome.SUCCESS)
    evidence = Evidence("evidence-1", todo.todo_id, "PYTEST", "pytest", EvidenceStatus.PASSED, {"exit_code": 0, "artifacts": ["report"]})
    gate = Gate("gate-tests", "TESTS", True, GateStatus.SATISFIED)
    handoff = Handoff(todo.todo_id, "deepseek_fast_coder", "qwen35_heavy_fast", "needs more capability", 1)
    attention = AttentionItem("attention-1", "Human confirmation needed", AttentionSeverity.HIGH, AttentionStatus.OPEN, todo.todo_id, run.run_id, "human")
    return goal, todo, work, run, attempt, evidence, gate, handoff, attention


def test_major_contracts_are_frozen_and_mapping_inputs_are_normalized() -> None:
    goal, todo, work, run, attempt, evidence, gate, handoff, attention = contracts()
    for contract in (goal, todo, work, run, attempt, evidence, gate, handoff, attention):
        with pytest.raises(FrozenInstanceError):
            setattr(contract, next(item.name for item in fields(contract)), "changed")
    assert isinstance(goal.metadata, type(work.constraints))
    with pytest.raises(TypeError):
        goal.metadata["new"] = "value"


def test_json_safe_serialization_is_deterministic_and_optional_values_are_stable() -> None:
    serialized = [contract.to_dict() for contract in contracts()]
    assert json.dumps(serialized, sort_keys=True) == json.dumps(serialized, sort_keys=True)
    run = Run("run-2", "goal-1", None, RunStatus.CREATED, QuotaUsage())
    attention = AttentionItem("attention-2", "Lead review", AttentionSeverity.MEDIUM, AttentionStatus.OPEN, run_id="run-2")
    assert run.to_dict()["active_todo_id"] is None
    assert attention.to_dict()["todo_id"] is None and attention.to_dict()["run_id"] == "run-2"


def test_mutable_input_collections_are_defensively_normalized() -> None:
    metadata = {"nested": {"items": ["a"]}}
    dependencies = ["todo-0"]
    worker_attempts = {"qwen35_heavy_fast": 1}
    goal = Goal("goal-1", "Objective", GoalStatus.CREATED, "created", metadata)
    todo = Todo("todo-1", "goal-1", "Todo", TodoStatus.PENDING, dependencies)
    usage = QuotaUsage(worker_attempts=worker_attempts)
    metadata["nested"]["items"].append("b")
    dependencies.append("todo-2")
    worker_attempts["qwen35_heavy_fast"] = 9
    assert goal.to_dict()["metadata"] == {"nested": {"items": ["a"]}}
    assert todo.dependencies == ("todo-0",)
    assert usage.worker_attempts["qwen35_heavy_fast"] == 1


@pytest.mark.parametrize("factory", [
    lambda: Goal("", "Objective", GoalStatus.CREATED, "created"),
    lambda: Todo("todo", "", "Objective", TodoStatus.PENDING),
    lambda: WorkContract("goal", "", "Objective", {}, (), (), (), quota()),
    lambda: Run("", "goal", None, RunStatus.CREATED, QuotaUsage()),
    lambda: Evidence("evidence", "", "PYTEST", "pytest", EvidenceStatus.PENDING),
    lambda: Gate("", "TESTS", True, GateStatus.PENDING),
    lambda: Handoff("todo", "", "qwen", "reason", 0),
    lambda: AttentionItem("attention", "", AttentionSeverity.LOW, AttentionStatus.OPEN, todo_id="todo"),
])
def test_empty_identifiers_and_objectives_are_rejected(factory) -> None:
    with pytest.raises(ValueError, match="non-empty"):
        factory()


@pytest.mark.parametrize("field_name", [
    "max_worker_calls", "max_same_fast_worker_attempts", "max_same_heavy_worker_attempts", "max_replans", "max_escalations",
])
def test_quota_rejects_negative_and_boolean_values(field_name: str) -> None:
    values = quota().to_dict()
    values[field_name] = -1
    with pytest.raises(ValueError, match="integer"):
        Quota(**values)
    values[field_name] = True
    with pytest.raises(ValueError, match="integer"):
        Quota(**values)


def test_todo_dependencies_are_only_id_references() -> None:
    todo = Todo("todo-1", "goal-1", "Objective", TodoStatus.PENDING, ["todo-0"])
    assert todo.dependencies == ("todo-0",)
    with pytest.raises(ValueError, match="non-empty"):
        Todo("todo-2", "goal-1", "Objective", TodoStatus.PENDING, [todo])


def test_work_contract_is_router_independent() -> None:
    contract = WorkContract("goal-1", "todo-1", "Objective", {"paths": ["harness/"]}, ("repository",), (), ("pytest",), quota())
    field_names = {item.name for item in fields(WorkContract)}
    assert not {"task_type", "complexity", "routing_decision", "score", "resource_admission", "runtime"} & field_names
    assert "router" not in WorkContract.__module__
    assert contract.to_dict()["quota"]["max_worker_calls"] == 4


def test_attempt_record_preserves_exact_routing_decision_and_serializes_enums() -> None:
    selected = decision()
    attempt = AttemptRecord(2, "todo-1", selected, "qwen35_heavy_fast", ExecutionOutcome.EXECUTION_FAILURE, ExecutionStage.GENERATION, "HTTP 500")
    assert attempt.routing_decision is selected
    encoded = attempt.to_dict()
    assert encoded["routing_decision"] == selected.to_dict()
    assert encoded["execution_outcome"] == "EXECUTION_FAILURE"
    assert encoded["failure_stage"] == "generation"
    with pytest.raises(ValueError, match="selected_model"):
        AttemptRecord(0, "todo-1", selected, "other")


def test_state_enums_have_stable_serialization() -> None:
    todo = Todo("todo-1", "goal-1", "Objective", TodoStatus.WAITING_EVIDENCE)
    run = Run("run-1", "goal-1", None, RunStatus.NEEDS_ATTENTION, QuotaUsage())
    gate = Gate("gate-1", "TESTS", True, GateStatus.PENDING)
    assert todo.to_dict()["status"] == "WAITING_EVIDENCE"
    assert run.to_dict()["status"] == "NEEDS_ATTENTION"
    assert gate.to_dict()["status"] == "PENDING"


def test_evidence_payload_is_compact_immutable_and_json_safe() -> None:
    evidence = Evidence("e-1", "todo-1", "TYPECHECK", "mypy", EvidenceStatus.PASSED, {"counts": (1, 2), "nested": {"ok": True}}, "artifact://typecheck")
    assert evidence.to_dict()["payload"] == {"counts": [1, 2], "nested": {"ok": True}}
    with pytest.raises(ValueError, match="JSON-safe"):
        Evidence("e-2", "todo-1", "PYTEST", "pytest", EvidenceStatus.PASSED, {"unsafe": object()})

def test_every_valid_major_contract_to_dict_is_json_round_trip_safe() -> None:
    for contract in contracts():
        json.dumps(contract.to_dict())


@pytest.mark.parametrize("factory, message", [
    (lambda: Goal("goal", "Objective", "ACTIVE", "created"), "GoalStatus"),
    (lambda: Todo("todo", "goal", "Objective", "READY"), "TodoStatus"),
    (lambda: Run("run", "goal", None, "RUNNING", QuotaUsage()), "RunStatus"),
    (lambda: Evidence("evidence", "todo", "PYTEST", "pytest", "PASSED"), "EvidenceStatus"),
    (lambda: Gate("gate", "TESTS", True, "PENDING"), "GateStatus"),
    (lambda: AttentionItem("attention", "reason", "HIGH", AttentionStatus.OPEN, todo_id="todo"), "AttentionSeverity"),
    (lambda: AttentionItem("attention", "reason", AttentionSeverity.HIGH, "OPEN", todo_id="todo"), "AttentionStatus"),
])
def test_contracts_reject_plain_string_enum_values(factory, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        factory()


def test_attempt_record_rejects_plain_string_execution_enums() -> None:
    selected = decision()
    with pytest.raises(ValueError, match="execution_outcome"):
        AttemptRecord(0, "todo", selected, "qwen35_heavy_fast", "SUCCESS")
    with pytest.raises(ValueError, match="failure_stage"):
        AttemptRecord(0, "todo", selected, "qwen35_heavy_fast", None, "generation")


@pytest.mark.parametrize("field_name", ["dependencies", "required_gates"])
def test_todo_rejects_non_sequence_id_reference_containers(field_name: str) -> None:
    values = {"dependencies": (), "required_gates": ()}
    values[field_name] = "abc"
    with pytest.raises(ValueError, match="list or tuple"):
        Todo("todo", "goal", "Objective", TodoStatus.PENDING, **values)


@pytest.mark.parametrize("field_name", ["allowed_scope", "forbidden_scope", "required_evidence"])
def test_work_contract_rejects_string_scope_and_evidence_containers(field_name: str) -> None:
    values = {"allowed_scope": (), "forbidden_scope": (), "required_evidence": ()}
    values[field_name] = "abc"
    with pytest.raises(ValueError, match="list or tuple"):
        WorkContract("goal", "todo", "Objective", {}, quota=quota(), **values)


@pytest.mark.parametrize("value", [[], "worker", ("worker", 1)])
def test_quota_usage_rejects_malformed_worker_attempts_collections(value: object) -> None:
    with pytest.raises(ValueError, match="worker_attempts"):
        QuotaUsage(worker_attempts=value)