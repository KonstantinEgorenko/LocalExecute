'Ornith-backed typed scenario selection for research diagnostics shadow evaluation.'

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .compiler import ScenarioCompilerV1
from .models import (
    CompiledScenarioV1,
    ScenarioSelectionV1,
    ScenarioTypeV1,
    scenario_selection_json_schema,
)


_SYSTEM = (
    "You are the Swarm scenario selector. "
    "Classify the mission into exactly one typed execution scenario. "
    "Return only one JSON object matching the supplied JSON Schema. "
    "Do not invent a Todo DAG, dependencies, worker actions, shell commands, "
    "file contents, or execution results."
)


def build_scenario_selector_prompt(objective: str, *, research_required: bool | None = None, scope_evidence=None) -> str:
    if not isinstance(objective, str) or not objective.strip():
        raise ValueError("objective must be a non-empty string")
    if research_required is not None and type(research_required) is not bool:
        raise ValueError("research_required must be bool or None")

    scenario_guidance = {
        ScenarioTypeV1.SIMPLE_MUTATION.value: (
            "One bounded source/config/document mutation; no explicit regression "
            "coverage requirement and no separate research phase."
        ),
        ScenarioTypeV1.BUGFIX_WITH_REGRESSION.value: (
            "A defect must be fixed and regression coverage added or updated. "
            "Repository inspection needed to implement the fix remains inside "
            "the implementation worker; do not create reconnaissance Todos."
        ),
        ScenarioTypeV1.MULTI_FILE_CHANGE.value: (
            "A coordinated implementation intentionally spans multiple files or modules."
        ),
        ScenarioTypeV1.RESEARCH_THEN_IMPLEMENT.value: (
            "A durable research/localization result is genuinely required before "
            "implementation can be specified safely."
        ),
        ScenarioTypeV1.VERIFICATION_ONLY.value: (
            "The mission asks only to verify/audit an existing candidate and does "
            "not require a workspace mutation."
        ),
        ScenarioTypeV1.CUSTOM_TYPED_DAG.value: (
            "None of the bounded predefined scenarios safely represents the mission. "
            "Use only when necessary; do not use it merely because repository "
            "inspection is needed."
        ),
    }
    payload = {
        "objective": objective,
        "rules": {
            "objective_must_be_copied_exactly": True,
            "choose_one_scenario_only": True,
            "do_not_emit_todos": True,
            "do_not_emit_control_flow": True,
            "inspection_is_not_a_separate_research_phase_by_default": True,
        },
        "scenario_guidance": scenario_guidance,
    }
    if research_required is not None:
        payload["mission_constraints"] = {"research_required": research_required}
        payload["rules"]["requires_research_must_match_mission_constraint"] = True
    if scope_evidence is not None:
        payload["authoritative_scope_evidence"] = scope_evidence.to_dict()
        payload["rules"]["allowed_paths_do_not_imply_required_mutations"] = True
        payload["rules"]["required_mutations_are_explicit_authoritative_requirements"] = True
    return (
        "Select the narrowest safe typed scenario for this mission. "
        "The output field `objective` MUST exactly equal the input objective. "
        "`target_hints` are non-authoritative hints only; use [] when targets "
        "cannot be known without repository inspection. "
        "`requires_regression_change` is true only when the mission explicitly "
        "requires adding/updating regression tests or equivalent regression coverage. "
        "`requires_research` is true only for a genuine durable research phase, "
        "not ordinary code inspection. "
        "Return canonical schema-conformant JSON only. Input="
        + json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )


@dataclass(frozen=True, slots=True)
class ScenarioSelectorTelemetryV1:
    model_id: str
    model_tag: str
    context_length: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "model_tag": self.model_tag,
            "context_length": self.context_length,
        }


@dataclass(frozen=True, slots=True)
class ScenarioShadowResultV1:
    selection: ScenarioSelectionV1
    compiled: CompiledScenarioV1
    telemetry: ScenarioSelectorTelemetryV1

    def to_dict(self) -> dict[str, Any]:
        return {
            "selection": self.selection.to_dict(),
            "compiled": self.compiled.to_dict(),
            "compiled_fingerprint": self.compiled.fingerprint,
            "telemetry": self.telemetry.to_dict(),
        }


class ScenarioSelectionError(RuntimeError):
    pass


class OrnithScenarioSelectorV1:
    """Use the existing Ornith runtime but never mutate production planner state."""

    def __init__(self, runtime: Any, compiler: ScenarioCompilerV1 | None = None) -> None:
        if runtime is None or not callable(getattr(runtime, "complete", None)):
            raise TypeError("runtime must expose complete(system, user, response_schema)")
        self.runtime = runtime
        self.compiler = compiler or ScenarioCompilerV1()

    def select(self, objective: str, *, research_required: bool | None = None,
               scope_evidence=None, record_response=None) -> ScenarioShadowResultV1:
        if not isinstance(objective, str) or not objective.strip():
            raise ValueError("objective must be a non-empty string")

        prompt = build_scenario_selector_prompt(objective, research_required=research_required, scope_evidence=scope_evidence)
        schema = scenario_selection_json_schema()
        if research_required is not None:
            schema["properties"]["requires_research"]["enum"] = [research_required]
            if not research_required:
                schema["properties"]["scenario"]["enum"].remove(
                    ScenarioTypeV1.RESEARCH_THEN_IMPLEMENT.value)
        response = self.runtime.complete(
            _SYSTEM,
            prompt,
            schema,
        )
        content = getattr(response, "content", None)
        if record_response is not None:
            record_response(content)
        if not isinstance(content, str):
            raise ScenarioSelectionError("scenario runtime response lacks text content")
        try:
            raw = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ScenarioSelectionError(
                f"scenario selector returned invalid JSON: {exc.msg}"
            ) from exc
        try:
            selection = ScenarioSelectionV1.from_dict(raw)
        except (TypeError, ValueError, KeyError) as exc:
            raise ScenarioSelectionError(
                f"scenario selector returned invalid typed selection: {exc}"
            ) from exc

        if selection.objective != objective:
            raise ScenarioSelectionError(
                "scenario selector changed the mission objective"
            )
        if research_required is not None and (
                selection.requires_research != research_required
                or (not research_required and
                    selection.scenario is ScenarioTypeV1.RESEARCH_THEN_IMPLEMENT)):
            raise ScenarioSelectionError("scenario selector violated mission research_required constraint")

        if scope_evidence is not None:
            scope_evidence.validate_selection(selection)

        try:
            compiled = self.compiler.compile(selection)
        except (ValueError, NotImplementedError) as exc:
            raise ScenarioSelectionError(
                f"scenario selection is not executable by ScenarioCompilerV1: {exc}"
            ) from exc

        try:
            telemetry = ScenarioSelectorTelemetryV1(
                model_id=str(response.model_id),
                model_tag=str(response.model_tag),
                context_length=int(response.context_length),
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise ScenarioSelectionError(
                "scenario runtime response lacks valid telemetry"
            ) from exc

        return ScenarioShadowResultV1(selection, compiled, telemetry)
