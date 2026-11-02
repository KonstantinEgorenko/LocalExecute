from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from harness.scenarios import ScenarioTypeV1
from harness.scenarios.selector import (
    OrnithScenarioSelectorV1,
    ScenarioSelectionError,
    build_scenario_selector_prompt,
)
from harness.scenarios.shadow import ScenarioShadowRecorderV1


OBJECTIVE = (
    "Customers report incorrect invoice totals when discounts and tax are combined. "
    "Fix the behavior while preserving the public API and add regression tests."
)


class Runtime:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def complete(self, system, user, response_schema):
        self.calls.append((system, user, response_schema))
        content = self.payload
        if not isinstance(content, str):
            content = json.dumps(content)
        return SimpleNamespace(
            content=content,
            model_id="ornith_lead",
            model_tag="ornith:test",
            context_length=65536,
        )


def payload(**updates):
    result = {
        "schema_version": 1,
        "scenario": "BUGFIX_WITH_REGRESSION",
        "objective": OBJECTIVE,
        "scope": "SINGLE_FILE",
        "target_hints": [],
        "requires_regression_change": True,
        "requires_research": False,
        "risk": "LOW",
        "ambiguity": "LOW",
    }
    result.update(updates)
    return result


def test_selector_uses_strict_runtime_schema_and_compiles():
    runtime = Runtime(payload())
    result = OrnithScenarioSelectorV1(runtime).select(OBJECTIVE)

    assert result.selection.scenario is ScenarioTypeV1.BUGFIX_WITH_REGRESSION
    assert [node.node_id for node in result.compiled.nodes] == [
        "implementation", "verify", "judge"
    ]
    assert result.telemetry.model_id == "ornith_lead"

    assert len(runtime.calls) == 1
    system, user, schema = runtime.calls[0]
    assert "scenario selector" in system.lower()
    assert OBJECTIVE in user
    assert schema["additionalProperties"] is False
    assert "todos" not in schema["properties"]


def test_prompt_explicitly_forbids_dag_generation():
    prompt = build_scenario_selector_prompt(OBJECTIVE)
    assert "do_not_emit_todos" in prompt
    assert "do_not_emit_control_flow" in prompt
    assert "inspection_is_not_a_separate_research_phase_by_default" in prompt


def test_selector_rejects_objective_drift():
    runtime = Runtime(payload(objective="Different objective"))
    with pytest.raises(ScenarioSelectionError, match="changed the mission objective"):
        OrnithScenarioSelectorV1(runtime).select(OBJECTIVE)


def test_selector_rejects_unknown_output_field():
    raw = payload()
    raw["todos"] = []
    runtime = Runtime(raw)
    with pytest.raises(ScenarioSelectionError, match="unknown fields"):
        OrnithScenarioSelectorV1(runtime).select(OBJECTIVE)


def test_selector_rejects_unimplemented_scenario_fail_closed():
    runtime = Runtime(payload(
        scenario="RESEARCH_THEN_IMPLEMENT",
        scope="REPOSITORY",
        requires_regression_change=False,
        requires_research=True,
    ))
    with pytest.raises(ScenarioSelectionError, match="not executable"):
        OrnithScenarioSelectorV1(runtime).select(OBJECTIVE)


def test_shadow_recorder_is_diagnostic_only_and_deterministic(tmp_path):
    result = OrnithScenarioSelectorV1(Runtime(payload())).select(OBJECTIVE)
    recorder = ScenarioShadowRecorderV1(tmp_path)

    path = recorder.record(mission_id="shadow-case", result=result)
    first = path.read_text(encoding="utf-8")
    path2 = recorder.record(mission_id="shadow-case", result=result)
    second = path2.read_text(encoding="utf-8")

    assert path == path2
    assert first == second
    raw = json.loads(first)
    assert raw["mode"] == "SHADOW_ONLY"
    assert raw["mission_id"] == "shadow-case"
    assert raw["selection"]["scenario"] == "BUGFIX_WITH_REGRESSION"
    assert raw["compiled"]["nodes"][0]["node_id"] == "implementation"


def test_explicit_no_research_constrains_schema_without_forcing_supported_scenario():
    runtime = Runtime(payload())
    OrnithScenarioSelectorV1(runtime).select(OBJECTIVE, research_required=False)
    _, prompt, schema = runtime.calls[0]
    assert '"research_required":false' in prompt
    assert schema["properties"]["requires_research"]["enum"] == [False]
    assert "RESEARCH_THEN_IMPLEMENT" not in schema["properties"]["scenario"]["enum"]
    assert "CUSTOM_TYPED_DAG" in schema["properties"]["scenario"]["enum"]


@pytest.mark.parametrize("updates", [
    {"requires_research": True},
    {"scenario": "RESEARCH_THEN_IMPLEMENT", "requires_research": False},
])
def test_no_research_mismatch_fails_even_when_transport_ignores_schema(updates):
    with pytest.raises(ScenarioSelectionError, match="research_required constraint"):
        OrnithScenarioSelectorV1(Runtime(payload(**updates))).select(OBJECTIVE, research_required=False)


@pytest.mark.parametrize("required", [None, True])
def test_required_research_cannot_be_silently_omitted_from_implementation(required):
    with pytest.raises(ScenarioSelectionError, match="required research"):
        OrnithScenarioSelectorV1(Runtime(payload(requires_research=True))).select(
            OBJECTIVE, research_required=required)


@pytest.mark.parametrize("invalid", [0, 1, "false"])
def test_invalid_research_constraint_fails_before_model_call(invalid):
    runtime = Runtime(payload())
    with pytest.raises(ValueError, match="research_required"):
        OrnithScenarioSelectorV1(runtime).select(OBJECTIVE, research_required=invalid)
    assert not runtime.calls
