"""A/B shadow comparison between legacy planning and typed scenario selection.

Diagnostic-only: no worker execution, no scheduler writes, no workspace mutation.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from harness.control_plane import InMemoryRunStore
from harness.integration.ornith_planner_adapter import OrnithPlanningAdapter
from harness.integration.plan_shape_normalization import SINGLE_IMPLEMENTATION_TODO_CONSTRAINT
from harness.lead import OrnithLead
from harness.lead.models import LeadRequest, LeadResult
from harness.mission.models import MissionSpecV1

from .compiler import ScenarioCompilerV1
from .models import ScenarioSelectionV1, scenario_selection_json_schema
from .projector import ScenarioPlanProjectorV1
from .selector import build_scenario_selector_prompt

_SELECTOR_SYSTEM = (
    "You are the Swarm scenario selector. "
    "Classify the mission into exactly one typed execution scenario. "
    "Return only one JSON object matching the supplied JSON Schema. "
    "Do not invent a Todo DAG, dependencies, worker actions, shell commands, "
    "file contents, or execution results."
)

@dataclass(slots=True)
class _CapturePlanRegistry:
    result: LeadResult | None = None
    observation: Any | None = None
    def load_plan(self, spec: MissionSpecV1):
        return None
    def save_plan(self, spec: MissionSpecV1, result: LeadResult, observation: Any):
        self.result = result
        self.observation = observation

def _typed_selection(runtime: Any, objective: str) -> ScenarioSelectionV1:
    response = runtime.complete(
        _SELECTOR_SYSTEM,
        build_scenario_selector_prompt(objective),
        scenario_selection_json_schema(),
    )
    content = getattr(response, "content", None)
    if not isinstance(content, str):
        raise RuntimeError("scenario selector response lacks content")
    raw = json.loads(content)
    selection = ScenarioSelectionV1.from_dict(raw)
    if selection.objective != objective:
        raise RuntimeError("scenario selector changed objective")
    return selection

def _request(objective: str, required_gates: tuple[str, ...]) -> LeadRequest:
    return LeadRequest(
        objective=objective,
        constraints={
            SINGLE_IMPLEMENTATION_TODO_CONSTRAINT: True,
            "SEQUENTIAL_MODEL_EXECUTION": True,
            "ISOLATED_WORKTREE_REQUIRED": True,
            "NO_AUTO_MERGE": True,
            "verification_required_gates": list(required_gates),
        },
        forbidden_scope=(".git", "state"),
        metadata={"phase24_ab_shadow": True},
    )

def _spec(case_id: str, objective: str) -> MissionSpecV1:
    safe = case_id.replace("_", "-")
    return MissionSpecV1(
        mission_id=f"phase24-ab-{safe}",
        goal_id=f"phase24-ab-{safe}-goal",
        raw_objective=objective,
        goal_capsule=objective,
        global_constraints={
            SINGLE_IMPLEMENTATION_TODO_CONSTRAINT: True,
            "SEQUENTIAL_MODEL_EXECUTION": True,
            "NO_AUTO_MERGE": True,
            "research_required": False,
        },
    )

def _summarize_legacy(result: LeadResult) -> dict[str, Any]:
    todos = []
    for item in result.work_items:
        todos.append({
            "todo_id": item.todo.todo_id,
            "objective": item.todo.objective,
            "dependencies": list(item.todo.dependencies),
            "required_gates": list(item.todo.required_gates),
            "required_evidence": list(item.work_contract.required_evidence),
            "requires_workspace_mutation":
                item.work_contract.constraints.get("REQUIRES_WORKSPACE_MUTATION") is True,
            "task_type": item.routing_task_spec.task_type.value,
            "scope": item.routing_task_spec.scope.value,
        })
    return {
        "outcome": result.outcome.value,
        "lead_call_count": result.lead_call_count,
        "todo_count": len(todos),
        "todos": todos,
        "warnings": list(result.warnings),
    }

class ScenarioAbShadowRunnerV1:
    def __init__(
        self,
        *,
        lead: Any,
        policies: Mapping[str, Any],
        required_gates: tuple[str, ...],
    ) -> None:
        runtime = getattr(lead, "runtime", None)
        if runtime is None or not callable(getattr(runtime, "complete", None)):
            raise TypeError("lead.runtime must expose complete(...)")
        self.lead = lead
        self.runtime = runtime
        self.policies = policies
        self.required_gates = tuple(required_gates)

    def compare_case(self, *, case_id: str, objective: str) -> dict[str, Any]:
        selection = _typed_selection(self.runtime, objective)
        typed: dict[str, Any] = {
            "selection": selection.to_dict(),
            "compile_status": "UNSUPPORTED",
            "compiled": None,
            "projection": None,
            "error": None,
        }
        try:
            compiled = ScenarioCompilerV1().compile(selection)
            typed["compile_status"] = "COMPILED"
            typed["compiled"] = compiled.to_dict()
            projection = ScenarioPlanProjectorV1().project(
                scenario=compiled,
                request=_request(objective, self.required_gates),
                goal_id=_spec(case_id, objective).goal_id,
                policies=self.policies,
                required_gates=self.required_gates,
            )
            typed["projection"] = projection.to_dict()
        except (NotImplementedError, ValueError) as exc:
            typed["error"] = str(exc)

        capture = _CapturePlanRegistry()
        legacy_lead = OrnithLead(
            InMemoryRunStore(),
            self.runtime,
            self.lead.config,
            self.lead.policies,
        )
        adapter = OrnithPlanningAdapter(
            legacy_lead,
            lambda spec: _request(spec.raw_objective, self.required_gates),
            plan_registry=capture,
            required_gates=self.required_gates,
        )
        observation = adapter.ensure_initial_plan(_spec(case_id, objective))
        if capture.result is None:
            raise RuntimeError("legacy planner did not publish accepted result")

        return {
            "case_id": case_id,
            "objective": objective,
            "typed": typed,
            "legacy": {
                **_summarize_legacy(capture.result),
                "plan_observation": observation.to_dict(),
            },
        }

    def compare_suite(self, suite: Mapping[str, Any]) -> dict[str, Any]:
        raw_cases = suite.get("cases")
        if not isinstance(raw_cases, list) or not raw_cases:
            raise ValueError("suite.cases must be a non-empty list")

        cases = [
            self.compare_case(case_id=str(raw["case_id"]), objective=str(raw["objective"]))
            for raw in raw_cases
        ]
        distribution: dict[str, int] = {}
        for case in cases:
            name = case["typed"]["selection"]["scenario"]
            distribution[name] = distribution.get(name, 0) + 1

        return {
            "schema_version": 1,
            "mode": "SHADOW_ONLY",
            "case_count": len(cases),
            "scenario_distribution": distribution,
            "typed_compiled_cases": sum(
                case["typed"]["compile_status"] == "COMPILED" for case in cases
            ),
            "legacy_total_todos": sum(case["legacy"]["todo_count"] for case in cases),
            "legacy_total_lead_calls": sum(
                case["legacy"]["lead_call_count"] for case in cases
            ),
            "cases": cases,
        }

def write_ab_shadow_report(path: Path, report: Mapping[str, Any]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return path
