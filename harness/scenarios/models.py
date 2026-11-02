"""Strict JSON-safe contracts for typed scenario execution.

Phase 24A is deliberately additive and shadow-only. These contracts do not
modify the production planner or scheduler. Their purpose is to make the
control-flow meaning explicit before any runtime integration is attempted.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping


class ScenarioTypeV1(StrEnum):
    SIMPLE_MUTATION = "SIMPLE_MUTATION"
    BUGFIX_WITH_REGRESSION = "BUGFIX_WITH_REGRESSION"
    MULTI_FILE_CHANGE = "MULTI_FILE_CHANGE"
    RESEARCH_THEN_IMPLEMENT = "RESEARCH_THEN_IMPLEMENT"
    VERIFICATION_ONLY = "VERIFICATION_ONLY"
    CUSTOM_TYPED_DAG = "CUSTOM_TYPED_DAG"


class ScenarioScopeV1(StrEnum):
    LOCAL = "LOCAL"
    SINGLE_FILE = "SINGLE_FILE"
    MULTI_FILE = "MULTI_FILE"
    REPOSITORY = "REPOSITORY"


class ScenarioRiskV1(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class ScenarioAmbiguityV1(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class ScenarioNodeKindV1(StrEnum):
    IMPLEMENTATION = "IMPLEMENTATION"
    RESEARCH = "RESEARCH"
    DETERMINISTIC_VERIFICATION = "DETERMINISTIC_VERIFICATION"
    JUDGE = "JUDGE"


class DeliverableTypeV1(StrEnum):
    CANDIDATE_DIFF = "CANDIDATE_DIFF"
    RESEARCH_EVIDENCE = "RESEARCH_EVIDENCE"
    VERIFICATION_REPORT = "VERIFICATION_REPORT"
    JUDGE_DECISION = "JUDGE_DECISION"


class ToolProfileV1(StrEnum):
    CODE_EDIT = "CODE_EDIT"
    READ_ONLY_RESEARCH = "READ_ONLY_RESEARCH"
    NONE = "NONE"


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _unique_text(values: object, name: str) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        raise ValueError(f"{name} must be a list or tuple")
    result = tuple(_required_text(value, name) for value in values)
    if len(set(result)) != len(result):
        raise ValueError(f"{name} must not contain duplicates")
    return result


@dataclass(frozen=True, slots=True)
class ScenarioSelectionV1:
    schema_version: int
    scenario: ScenarioTypeV1
    objective: str
    scope: ScenarioScopeV1
    target_hints: tuple[str, ...]
    requires_regression_change: bool
    requires_research: bool
    risk: ScenarioRiskV1
    ambiguity: ScenarioAmbiguityV1

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("ScenarioSelectionV1 schema_version must be 1")
        if not isinstance(self.scenario, ScenarioTypeV1):
            raise ValueError("scenario must be ScenarioTypeV1")
        _required_text(self.objective, "objective")
        if not isinstance(self.scope, ScenarioScopeV1):
            raise ValueError("scope must be ScenarioScopeV1")
        object.__setattr__(
            self,
            "target_hints",
            _unique_text(self.target_hints, "target_hints"),
        )
        if not isinstance(self.requires_regression_change, bool):
            raise ValueError("requires_regression_change must be bool")
        if not isinstance(self.requires_research, bool):
            raise ValueError("requires_research must be bool")
        if not isinstance(self.risk, ScenarioRiskV1):
            raise ValueError("risk must be ScenarioRiskV1")
        if not isinstance(self.ambiguity, ScenarioAmbiguityV1):
            raise ValueError("ambiguity must be ScenarioAmbiguityV1")

        if (
            self.scenario is ScenarioTypeV1.BUGFIX_WITH_REGRESSION
            and not self.requires_regression_change
        ):
            raise ValueError(
                "BUGFIX_WITH_REGRESSION requires requires_regression_change=true"
            )
        if self.scenario is ScenarioTypeV1.VERIFICATION_ONLY:
            if self.requires_regression_change:
                raise ValueError(
                    "VERIFICATION_ONLY cannot require a regression artifact change"
                )
            if self.requires_research:
                raise ValueError("VERIFICATION_ONLY cannot require research")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "scenario": self.scenario.value,
            "objective": self.objective,
            "scope": self.scope.value,
            "target_hints": list(self.target_hints),
            "requires_regression_change": self.requires_regression_change,
            "requires_research": self.requires_research,
            "risk": self.risk.value,
            "ambiguity": self.ambiguity.value,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ScenarioSelectionV1":
        if not isinstance(raw, Mapping):
            raise ValueError("ScenarioSelectionV1 payload must be a mapping")
        expected = {
            "schema_version",
            "scenario",
            "objective",
            "scope",
            "target_hints",
            "requires_regression_change",
            "requires_research",
            "risk",
            "ambiguity",
        }
        unknown = set(raw) - expected
        missing = expected - set(raw)
        if unknown:
            raise ValueError(
                "ScenarioSelectionV1 contains unknown fields: "
                + ", ".join(sorted(unknown))
            )
        if missing:
            raise ValueError(
                "ScenarioSelectionV1 missing fields: "
                + ", ".join(sorted(missing))
            )
        return cls(
            schema_version=raw["schema_version"],
            scenario=ScenarioTypeV1(str(raw["scenario"])),
            objective=str(raw["objective"]),
            scope=ScenarioScopeV1(str(raw["scope"])),
            target_hints=tuple(raw["target_hints"]),
            requires_regression_change=raw["requires_regression_change"],
            requires_research=raw["requires_research"],
            risk=ScenarioRiskV1(str(raw["risk"])),
            ambiguity=ScenarioAmbiguityV1(str(raw["ambiguity"])),
        )


@dataclass(frozen=True, slots=True)
class ScenarioNodeV1:
    node_id: str
    kind: ScenarioNodeKindV1
    dependencies: tuple[str, ...]
    deliverable: DeliverableTypeV1
    mutation_required: bool
    tool_profile: ToolProfileV1

    def __post_init__(self) -> None:
        _required_text(self.node_id, "node_id")
        if not isinstance(self.kind, ScenarioNodeKindV1):
            raise ValueError("kind must be ScenarioNodeKindV1")
        object.__setattr__(
            self,
            "dependencies",
            _unique_text(self.dependencies, "dependencies"),
        )
        if not isinstance(self.deliverable, DeliverableTypeV1):
            raise ValueError("deliverable must be DeliverableTypeV1")
        if not isinstance(self.mutation_required, bool):
            raise ValueError("mutation_required must be bool")
        if not isinstance(self.tool_profile, ToolProfileV1):
            raise ValueError("tool_profile must be ToolProfileV1")

        if (
            self.kind is ScenarioNodeKindV1.DETERMINISTIC_VERIFICATION
            and self.mutation_required
        ):
            raise ValueError("verification node cannot require workspace mutation")
        if self.kind is ScenarioNodeKindV1.JUDGE and self.mutation_required:
            raise ValueError("judge node cannot require workspace mutation")

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "kind": self.kind.value,
            "dependencies": list(self.dependencies),
            "deliverable": self.deliverable.value,
            "mutation_required": self.mutation_required,
            "tool_profile": self.tool_profile.value,
        }


@dataclass(frozen=True, slots=True)
class CompiledScenarioV1:
    schema_version: int
    selection: ScenarioSelectionV1
    nodes: tuple[ScenarioNodeV1, ...]

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("CompiledScenarioV1 schema_version must be 1")
        if not isinstance(self.selection, ScenarioSelectionV1):
            raise ValueError("selection must be ScenarioSelectionV1")
        object.__setattr__(self, "nodes", tuple(self.nodes))
        if not self.nodes:
            raise ValueError("compiled scenario requires at least one node")
        if any(not isinstance(node, ScenarioNodeV1) for node in self.nodes):
            raise ValueError("nodes must contain only ScenarioNodeV1")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "selection": self.selection.to_dict(),
            "nodes": [node.to_dict() for node in self.nodes],
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


def scenario_selection_json_schema() -> dict[str, Any]:
    """Strict schema intended for future model response_format use.

    Phase 24A does not bind this schema to production inference yet.
    """

    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "scenario",
            "objective",
            "scope",
            "target_hints",
            "requires_regression_change",
            "requires_research",
            "risk",
            "ambiguity",
        ],
        "properties": {
            "schema_version": {"type": "integer", "enum": [1]},
            "scenario": {
                "type": "string",
                "enum": [item.value for item in ScenarioTypeV1],
            },
            "objective": {"type": "string", "minLength": 1},
            "scope": {
                "type": "string",
                "enum": [item.value for item in ScenarioScopeV1],
            },
            "target_hints": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "uniqueItems": True,
            },
            "requires_regression_change": {"type": "boolean"},
            "requires_research": {"type": "boolean"},
            "risk": {
                "type": "string",
                "enum": [item.value for item in ScenarioRiskV1],
            },
            "ambiguity": {
                "type": "string",
                "enum": [item.value for item in ScenarioAmbiguityV1],
            },
        },
    }
