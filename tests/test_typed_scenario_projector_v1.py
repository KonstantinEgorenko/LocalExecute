from __future__ import annotations

from dataclasses import replace

import pytest

from harness.lead.models import LeadRequest
from harness.router import (
    Complexity,
    NeedLevel,
    Scope,
    TaskType,
    ValidationLevel,
)
from harness.scenarios import (
    ScenarioAmbiguityV1,
    ScenarioCompilerV1,
    ScenarioRiskV1,
    ScenarioScopeV1,
    ScenarioSelectionV1,
    ScenarioTypeV1,
)
from harness.scenarios.projector import ScenarioPlanProjectorV1


OBJECTIVE = (
    "Customers report incorrect invoice totals when discounts and tax are combined. "
    "Fix the behavior while preserving the public API and add regression tests."
)


def policies():
    return {
        "retry_and_anti_loop": {
            "max_total_worker_calls_per_task": 16,
            "max_attempts_same_fast_worker": 2,
            "max_attempts_same_heavy_worker": 2,
            "max_replans_per_task": 2,
            "max_escalations_per_task": 3,
        }
    }


def selection(**updates):
    value = ScenarioSelectionV1(
        schema_version=1,
        scenario=ScenarioTypeV1.BUGFIX_WITH_REGRESSION,
        objective=OBJECTIVE,
        scope=ScenarioScopeV1.SINGLE_FILE,
        target_hints=("billing.py", "tests/test_billing.py"),
        requires_regression_change=True,
        requires_research=False,
        risk=ScenarioRiskV1.LOW,
        ambiguity=ScenarioAmbiguityV1.LOW,
    )
    return replace(value, **updates)


def request(**updates):
    value = LeadRequest(
        objective=OBJECTIVE,
        constraints={"NO_AUTO_MERGE": True},
        allowed_scope=("src", "tests"),
        forbidden_scope=(".git", "state"),
        metadata={"shadow": True},
    )
    return replace(value, **updates)


def project(sel=None, req=None):
    compiled = ScenarioCompilerV1().compile(sel or selection())
    return ScenarioPlanProjectorV1().project(
        scenario=compiled,
        request=req or request(),
        goal_id="typed-shadow-goal",
        policies=policies(),
        required_gates=("candidate_diff", "scope_guard", "python_syntax", "pytest"),
    )


def test_bugfix_projects_to_exactly_one_model_executable_todo():
    projection = project()

    assert len(projection.work_items) == 1
    item = projection.work_items[0]
    assert item.todo.todo_id == "typed-shadow-goal:todo:01"
    assert item.todo.dependencies == ()
    assert item.todo.objective == OBJECTIVE
    assert item.todo.required_gates == (
        "candidate_diff", "scope_guard", "python_syntax", "pytest", "regression_change"
    )

    assert projection.control_node_ids == ("verify", "judge")


def test_projection_has_explicit_mutation_and_candidate_diff_contract():
    item = project().work_items[0]

    assert item.work_contract.required_evidence == (
        "WORKER_RESULT", "CANDIDATE_DIFF"
    )
    constraints = dict(item.work_contract.constraints)
    assert constraints["TYPED_SCENARIO_EXECUTION"] is True
    assert constraints["SCENARIO_TYPE"] == "BUGFIX_WITH_REGRESSION"
    assert constraints["SCENARIO_NODE_ID"] == "implementation"
    assert constraints["REQUIRES_WORKSPACE_MUTATION"] is True
    assert constraints["REQUIRES_REGRESSION_CHANGE"] is True


def test_regression_gate_is_only_required_when_declared_by_scenario():
    item = project(selection(scenario=ScenarioTypeV1.SIMPLE_MUTATION,
                             requires_regression_change=False)).work_items[0]
    assert "regression_change" not in item.todo.required_gates


def test_target_hints_are_non_authoritative_and_do_not_narrow_scope():
    item = project().work_items[0]

    assert item.work_contract.allowed_scope == ("src", "tests")
    assert item.work_contract.forbidden_scope == (".git", "state")
    assert tuple(item.work_contract.constraints["SCENARIO_TARGET_HINTS"]) == (
        "billing.py", "tests/test_billing.py"
    )


def test_bugfix_routing_is_deterministic_not_model_generated():
    routing = project().work_items[0].routing_task_spec

    assert routing.task_type is TaskType.BUGFIX
    assert routing.complexity is Complexity.NORMAL
    assert routing.scope is Scope.LOCAL
    assert routing.reasoning_need is NeedLevel.LOW
    assert routing.repo_exploration_need is NeedLevel.LOW
    assert routing.testability is ValidationLevel.HIGH
    assert routing.risk is NeedLevel.LOW
    assert routing.router_confidence == 1.0


def test_unknown_target_raises_repo_exploration_need_without_creating_recon_todo():
    projection = project(
        selection(target_hints=(), ambiguity=ScenarioAmbiguityV1.MEDIUM)
    )
    assert len(projection.work_items) == 1
    routing = projection.work_items[0].routing_task_spec
    assert routing.repo_exploration_need is NeedLevel.MEDIUM
    assert routing.reasoning_need is NeedLevel.MEDIUM


def test_parent_constraints_are_preserved_and_conflicts_fail_closed():
    item = project().work_items[0]
    assert item.work_contract.constraints["NO_AUTO_MERGE"] is True

    conflicting = request(
        constraints={
            "NO_AUTO_MERGE": True,
            "TYPED_SCENARIO_EXECUTION": False,
        }
    )
    with pytest.raises(ValueError, match="conflicts with parent"):
        project(req=conflicting)


def test_objective_drift_fails_closed():
    compiled = ScenarioCompilerV1().compile(selection())
    with pytest.raises(ValueError, match="exactly match"):
        ScenarioPlanProjectorV1().project(
            scenario=compiled,
            request=request(objective="Different mission"),
            goal_id="typed-shadow-goal",
            policies=policies(),
            required_gates=("candidate_diff",),
        )


def test_required_gates_must_be_non_empty():
    compiled = ScenarioCompilerV1().compile(selection())
    with pytest.raises(ValueError, match="requires deterministic gates"):
        ScenarioPlanProjectorV1().project(
            scenario=compiled,
            request=request(),
            goal_id="typed-shadow-goal",
            policies=policies(),
            required_gates=(),
        )


def test_multi_file_change_projects_one_worker_todo_with_multi_file_scope():
    projection = project(
        selection(
            scenario=ScenarioTypeV1.MULTI_FILE_CHANGE,
            scope=ScenarioScopeV1.MULTI_FILE,
            requires_regression_change=False,
            risk=ScenarioRiskV1.MEDIUM,
            ambiguity=ScenarioAmbiguityV1.MEDIUM,
        )
    )
    assert len(projection.work_items) == 1
    routing = projection.work_items[0].routing_task_spec
    assert routing.task_type is TaskType.CODE
    assert routing.scope is Scope.MULTI_FILE
    assert routing.reasoning_need is NeedLevel.MEDIUM
    assert routing.risk is NeedLevel.MEDIUM
    assert projection.control_node_ids == ("verify", "judge")


def test_projection_is_deterministic():
    first = project()
    second = project()
    assert first.to_dict() == second.to_dict()
    assert first.fingerprint == second.fingerprint
