'mission immutable durable mission lifecycle contracts.'
from __future__ import annotations
from harness.compatibility.configuration import resolve_config_path, normalize_configuration_document

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping

import yaml

from ..errors import ConfigurationError
from ..control_plane.todo_identity import validate_todo_id
from ..workflow.models import WorkflowOutcome, WorkflowResultV1

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _text(value: object, name: str, *, max_length: int | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty string")
    if max_length is not None and len(value) > max_length:
        raise ValueError(f"{name} exceeds max_length={max_length}")
    return value


def _safe_id(value: object, name: str) -> str:
    value = _text(value, name)
    if not _ID_RE.fullmatch(value):
        raise ValueError(f"{name} must match {_ID_RE.pattern}")
    return value


def _non_negative_int(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be integer >= 0")
    return value


def _positive_int(value: object, name: str) -> int:
    value = _non_negative_int(value, name)
    if value == 0:
        raise ValueError(f"{name} must be > 0")
    return value


def _unique_text(values: object, name: str, *, maximum: int | None = None) -> tuple[str, ...]:
    if not isinstance(values, (tuple, list)):
        raise ValueError(f"{name} must be list/tuple")
    result = tuple(_text(value, name) for value in values)
    if len(set(result)) != len(result):
        raise ValueError(f"{name} must not contain duplicates")
    if maximum is not None and len(result) > maximum:
        raise ValueError(f"{name} exceeds maximum={maximum}")
    return result


def _freeze_json(value: Any, name: str = "value") -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{name} must not contain non-finite float")
        return value
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{name} mapping keys must be strings")
            frozen[key] = _freeze_json(item, f"{name}.{key}")
        return MappingProxyType(frozen)
    if isinstance(value, (tuple, list)):
        return tuple(_freeze_json(item, name) for item in value)
    raise ValueError(f"{name} must be JSON-safe")


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    if isinstance(value, StrEnum):
        return value.value
    return value


def canonical_json(value: object) -> str:
    return json.dumps(_plain(value), sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def stable_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class MissionState(StrEnum):
    CREATED = "CREATED"
    PLANNED = "PLANNED"
    RUNNING = "RUNNING"
    BLOCKED = "BLOCKED"
    RECOVERING = "RECOVERING"
    VERIFYING = "VERIFYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class DagOperationKind(StrEnum):
    ADD_TODO = "ADD_TODO"
    ADD_DEPENDENCY = "ADD_DEPENDENCY"
    REVISE_PENDING_TODO = "REVISE_PENDING_TODO"


@dataclass(frozen=True, slots=True)
class MissionLifecyclePolicy:
    max_workflow_invocations_per_run: int
    max_dag_amendments_per_mission: int
    max_operations_per_amendment: int
    max_journal_events: int
    max_report_chars: int
    require_atomic_store: bool
    require_generation_cas: bool
    require_workflow_for_execution: bool
    require_final_regression: bool
    require_root_invariant_gate: bool
    require_clean_workspace: bool
    require_snapshot_consistency: bool
    live_mission_cases: int
    live_min_success_rate: float

    def __post_init__(self) -> None:
        for name in (
            'max_workflow_invocations_per_run',
            "max_dag_amendments_per_mission",
            "max_operations_per_amendment",
            "max_journal_events",
            "max_report_chars",
            "live_mission_cases",
        ):
            _positive_int(getattr(self, name), name)
        if not isinstance(self.live_min_success_rate, (int, float)) or isinstance(self.live_min_success_rate, bool):
            raise ValueError("live_min_success_rate must be number")
        if not math.isfinite(float(self.live_min_success_rate)) or not 0 <= float(self.live_min_success_rate) <= 1:
            raise ValueError("live_min_success_rate must be in [0,1]")
        for name in (
            "require_atomic_store",
            "require_generation_cas",
            'require_workflow_for_execution',
            "require_final_regression",
            "require_root_invariant_gate",
            "require_clean_workspace",
            "require_snapshot_consistency",
        ):
            if getattr(self, name) is not True:
                raise ValueError(f"Phase 17 invariant requires {name}=true")

    @classmethod
    def from_project_root(cls, root: object) -> "MissionLifecyclePolicy":
        from pathlib import Path

        path = resolve_config_path(Path(root) / "config" / "phase17.yaml")
        try:
            raw = normalize_configuration_document(yaml.safe_load(path.read_text(encoding="utf-8")))
        except OSError as exc:
            raise ConfigurationError(f"cannot read Phase 17 config: {exc}") from exc
        except yaml.YAMLError as exc:
            raise ConfigurationError(f"malformed Phase 17 YAML: {exc}") from exc
        if not isinstance(raw, Mapping) or raw.get("schema_version") != 1:
            raise ConfigurationError("phase17.yaml requires schema_version: 1")
        body = raw.get("durable_mission_v1")
        if not isinstance(body, Mapping):
            raise ConfigurationError("phase17.yaml.durable_mission_v1 must be mapping")
        live = body.get("live_acceptance")
        if not isinstance(live, Mapping):
            raise ConfigurationError("phase17 live_acceptance must be mapping")
        try:
            return cls(
                int(body['max_workflow_invocations_per_run']),
                int(body["max_dag_amendments_per_mission"]),
                int(body["max_operations_per_amendment"]),
                int(body["max_journal_events"]),
                int(body["max_report_chars"]),
                body["require_atomic_store"],
                body["require_generation_cas"],
                body['require_workflow_for_execution'],
                body["require_final_regression"],
                body["require_root_invariant_gate"],
                body["require_clean_workspace"],
                body["require_snapshot_consistency"],
                int(live["mission_cases"]),
                float(live["min_success_rate"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigurationError(f"invalid Phase 17 policy: {exc}") from exc


@dataclass(frozen=True, slots=True)
class MissionSpecV1:
    mission_id: str
    goal_id: str
    raw_objective: str
    goal_capsule: str
    global_constraints: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _safe_id(self.mission_id, "mission_id")
        _safe_id(self.goal_id, "goal_id")
        _text(self.raw_objective, "raw_objective", max_length=2_000_000)
        _text(self.goal_capsule, "goal_capsule", max_length=100_000)
        object.__setattr__(self, "global_constraints", _freeze_json(self.global_constraints, "global_constraints"))

    @property
    def root_task_sha256(self) -> str:
        return hashlib.sha256(self.raw_objective.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "mission_id": self.mission_id,
            "goal_id": self.goal_id,
            "raw_objective": self.raw_objective,
            "goal_capsule": self.goal_capsule,
            "global_constraints": _plain(self.global_constraints),
            "root_task_sha256": self.root_task_sha256,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "MissionSpecV1":
        if raw.get("schema_version") != 1:
            raise ValueError("MissionSpecV1 schema_version must be 1")
        result = cls(
            str(raw["mission_id"]),
            str(raw["goal_id"]),
            str(raw["raw_objective"]),
            str(raw["goal_capsule"]),
            raw.get("global_constraints", {}),
        )
        if raw.get("root_task_sha256") != result.root_task_sha256:
            raise ValueError("MissionSpecV1 root_task_sha256 mismatch")
        return result


@dataclass(frozen=True, slots=True)
class PlanObservationV1:
    plan_id: str
    todo_ids: tuple[str, ...]
    fingerprint: str
    summary: str

    def __post_init__(self) -> None:
        _safe_id(self.plan_id, "plan_id")
        object.__setattr__(self, "todo_ids", _unique_text(self.todo_ids, "todo_ids"))
        if not self.todo_ids:
            raise ValueError("initial plan must contain at least one Todo")
        _text(self.fingerprint, "fingerprint")
        _text(self.summary, "summary", max_length=16000)

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "todo_ids": list(self.todo_ids),
            "fingerprint": self.fingerprint,
            "summary": self.summary,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "PlanObservationV1":
        return cls(str(raw["plan_id"]), tuple(raw["todo_ids"]), str(raw["fingerprint"]), str(raw["summary"]))


@dataclass(frozen=True, slots=True)
class DagAmendmentOperationV1:
    kind: DagOperationKind
    todo_id: str
    dependency_id: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.kind, DagOperationKind):
            raise ValueError("kind must be DagOperationKind")
        validate_todo_id(self.todo_id)
        if self.dependency_id is not None:
            validate_todo_id(self.dependency_id, "dependency_id")
        object.__setattr__(self, "payload", _freeze_json(self.payload, "payload"))
        if self.kind is DagOperationKind.ADD_DEPENDENCY and self.dependency_id is None:
            raise ValueError("ADD_DEPENDENCY requires dependency_id")
        if self.kind is not DagOperationKind.ADD_DEPENDENCY and self.dependency_id is not None:
            raise ValueError("dependency_id only valid for ADD_DEPENDENCY")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "todo_id": self.todo_id,
            "dependency_id": self.dependency_id,
            "payload": _plain(self.payload),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "DagAmendmentOperationV1":
        return cls(
            DagOperationKind(str(raw["kind"])),
            str(raw["todo_id"]),
            None if raw.get("dependency_id") is None else str(raw["dependency_id"]),
            raw.get("payload", {}),
        )


@dataclass(frozen=True, slots=True)
class DagAmendmentV1:
    amendment_id: str
    reason: str
    operations: tuple[DagAmendmentOperationV1, ...]
    evidence_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _safe_id(self.amendment_id, "amendment_id")
        _text(self.reason, "reason", max_length=16000)
        if not isinstance(self.operations, (tuple, list)) or not self.operations:
            raise ValueError("operations must contain at least one operation")
        operations = tuple(self.operations)
        if any(not isinstance(item, DagAmendmentOperationV1) for item in operations):
            raise ValueError("operations must contain DagAmendmentOperationV1")
        object.__setattr__(self, "operations", operations)
        object.__setattr__(self, "evidence_ids", _unique_text(self.evidence_ids, "evidence_ids", maximum=32))

    @property
    def fingerprint(self) -> str:
        return stable_sha256({
            "amendment_id": self.amendment_id,
            "reason": self.reason,
            "operations": [item.to_dict() for item in self.operations],
            "evidence_ids": list(self.evidence_ids),
        })

    def to_dict(self) -> dict[str, Any]:
        return {
            "amendment_id": self.amendment_id,
            "reason": self.reason,
            "operations": [item.to_dict() for item in self.operations],
            "evidence_ids": list(self.evidence_ids),
            "fingerprint": self.fingerprint,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "DagAmendmentV1":
        result = cls(
            str(raw["amendment_id"]),
            str(raw["reason"]),
            tuple(DagAmendmentOperationV1.from_dict(item) for item in raw["operations"]),
            tuple(raw.get("evidence_ids", ())),
        )
        if raw.get("fingerprint") != result.fingerprint:
            raise ValueError("DagAmendmentV1 fingerprint mismatch")
        return result


@dataclass(frozen=True, slots=True)
class GlobalCompletionGateV1:
    workflow_succeeded: bool
    all_todos_verified: bool
    final_regression_passed: bool
    root_invariants_satisfied: bool
    no_blocked_todos: bool
    workspace_clean: bool
    project_snapshot_consistent: bool
    evidence_ids: tuple[str, ...]
    summary: str

    def __post_init__(self) -> None:
        for name in (
            "phase16_succeeded",
            "all_todos_verified",
            "final_regression_passed",
            "root_invariants_satisfied",
            "no_blocked_todos",
            "workspace_clean",
            "project_snapshot_consistent",
        ):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be bool")
        object.__setattr__(self, "evidence_ids", _unique_text(self.evidence_ids, "evidence_ids", maximum=64))
        _text(self.summary, "summary", max_length=16000)

    @property
    def passed(self) -> bool:
        return all((
            self.workflow_succeeded,
            self.all_todos_verified,
            self.final_regression_passed,
            self.root_invariants_satisfied,
            self.no_blocked_todos,
            self.workspace_clean,
            self.project_snapshot_consistent,
        ))

    @property
    def fingerprint(self) -> str:
        return stable_sha256(self.to_dict(include_fingerprint=False))

    def to_dict(self, *, include_fingerprint: bool = True) -> dict[str, Any]:
        raw = {
            "phase16_succeeded": self.workflow_succeeded,
            "all_todos_verified": self.all_todos_verified,
            "final_regression_passed": self.final_regression_passed,
            "root_invariants_satisfied": self.root_invariants_satisfied,
            "no_blocked_todos": self.no_blocked_todos,
            "workspace_clean": self.workspace_clean,
            "project_snapshot_consistent": self.project_snapshot_consistent,
            "evidence_ids": list(self.evidence_ids),
            "summary": self.summary,
            "passed": self.passed,
        }
        if include_fingerprint:
            raw["fingerprint"] = stable_sha256(raw)
        return raw

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "GlobalCompletionGateV1":
        result = cls(
            raw["phase16_succeeded"],
            raw["all_todos_verified"],
            raw["final_regression_passed"],
            raw["root_invariants_satisfied"],
            raw["no_blocked_todos"],
            raw["workspace_clean"],
            raw["project_snapshot_consistent"],
            tuple(raw.get("evidence_ids", ())),
            str(raw["summary"]),
        )
        if raw.get("passed") is not result.passed:
            raise ValueError("GlobalCompletionGateV1 passed mismatch")
        expected = result.to_dict().get("fingerprint")
        if raw.get("fingerprint") != expected:
            raise ValueError("GlobalCompletionGateV1 fingerprint mismatch")
        return result


@dataclass(frozen=True, slots=True)
class MissionEventV1:
    sequence: int
    event_type: str
    state: MissionState
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _non_negative_int(self.sequence, "sequence")
        _text(self.event_type, "event_type")
        if not isinstance(self.state, MissionState):
            raise ValueError("state must be MissionState")
        object.__setattr__(self, "details", _freeze_json(self.details, "details"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "event_type": self.event_type,
            "state": self.state.value,
            "details": _plain(self.details),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "MissionEventV1":
        return cls(int(raw["sequence"]), str(raw["event_type"]), MissionState(str(raw["state"])), raw.get("details", {}))


@dataclass(frozen=True, slots=True)
class MissionCheckpointV1:
    spec: MissionSpecV1
    state: MissionState
    generation: int
    plan: PlanObservationV1 | None = None
    workflow_invocations: int = 0
    amendment_count: int = 0
    restart_count: int = 0
    completed_todo_ids: tuple[str, ...] = ()
    amendments: tuple[DagAmendmentV1, ...] = ()
    applied_amendment_ids: tuple[str, ...] = ()
    last_workflow_outcome: str | None = None
    last_workflow_summary: str | None = None
    completion_gate: GlobalCompletionGateV1 | None = None
    final_report_ref: str | None = None
    failure_reason: str | None = None
    events: tuple[MissionEventV1, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.spec, MissionSpecV1):
            raise ValueError("spec must be MissionSpecV1")
        if not isinstance(self.state, MissionState):
            raise ValueError("state must be MissionState")
        _non_negative_int(self.generation, "generation")
        _non_negative_int(self.workflow_invocations, "phase16_invocations")
        _non_negative_int(self.amendment_count, "amendment_count")
        _non_negative_int(self.restart_count, "restart_count")
        if self.plan is not None and not isinstance(self.plan, PlanObservationV1):
            raise ValueError("plan must be PlanObservationV1|None")
        object.__setattr__(self, "completed_todo_ids", _unique_text(self.completed_todo_ids, "completed_todo_ids"))
        amendments = tuple(self.amendments)
        if any(not isinstance(item, DagAmendmentV1) for item in amendments):
            raise ValueError("amendments must contain DagAmendmentV1")
        if self.amendment_count != len(amendments):
            raise ValueError("amendment_count must equal len(amendments)")
        object.__setattr__(self, "amendments", amendments)
        applied = _unique_text(self.applied_amendment_ids, "applied_amendment_ids")
        amendment_ids = {item.amendment_id for item in amendments}
        if not set(applied) <= amendment_ids:
            raise ValueError("applied_amendment_ids must reference persisted amendments")
        object.__setattr__(self, "applied_amendment_ids", applied)
        if self.last_workflow_outcome is not None:
            _text(self.last_workflow_outcome, "last_workflow_outcome")
        if self.last_workflow_summary is not None:
            _text(self.last_workflow_summary, "last_workflow_summary", max_length=32000)
        if self.final_report_ref is not None:
            _text(self.final_report_ref, "final_report_ref")
        if self.failure_reason is not None:
            _text(self.failure_reason, "failure_reason", max_length=32000)
        events = tuple(self.events)
        if any(not isinstance(item, MissionEventV1) for item in events):
            raise ValueError("events must contain MissionEventV1")
        if tuple(item.sequence for item in events) != tuple(range(len(events))):
            raise ValueError("events sequence must be contiguous from zero")
        object.__setattr__(self, "events", events)
        if self.state is MissionState.CREATED and self.plan is not None:
            raise ValueError("CREATED mission must not already have a plan")
        if self.state not in {MissionState.CREATED, MissionState.FAILED} and self.plan is None:
            raise ValueError("planned/running mission requires plan")
        if self.state is MissionState.COMPLETED:
            if self.completion_gate is None or not self.completion_gate.passed or self.final_report_ref is None:
                raise ValueError("COMPLETED mission requires passing completion gate and final report")

    @property
    def mission_id(self) -> str:
        return self.spec.mission_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "spec": self.spec.to_dict(),
            "state": self.state.value,
            "generation": self.generation,
            "plan": None if self.plan is None else self.plan.to_dict(),
            "phase16_invocations": self.workflow_invocations,
            "amendment_count": self.amendment_count,
            "restart_count": self.restart_count,
            "completed_todo_ids": list(self.completed_todo_ids),
            "amendments": [item.to_dict() for item in self.amendments],
            "applied_amendment_ids": list(self.applied_amendment_ids),
            "last_workflow_outcome": self.last_workflow_outcome,
            "last_workflow_summary": self.last_workflow_summary,
            "completion_gate": None if self.completion_gate is None else self.completion_gate.to_dict(),
            "final_report_ref": self.final_report_ref,
            "failure_reason": self.failure_reason,
            "events": [item.to_dict() for item in self.events],
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "MissionCheckpointV1":
        if raw.get("schema_version") != 1:
            raise ValueError("MissionCheckpointV1 schema_version must be 1")
        return cls(
            MissionSpecV1.from_dict(raw["spec"]),
            MissionState(str(raw["state"])),
            int(raw["generation"]),
            None if raw.get("plan") is None else PlanObservationV1.from_dict(raw["plan"]),
            int(raw.get("phase16_invocations", 0)),
            int(raw.get("amendment_count", 0)),
            int(raw.get("restart_count", 0)),
            tuple(raw.get("completed_todo_ids", ())),
            tuple(DagAmendmentV1.from_dict(item) for item in raw.get("amendments", ())),
            tuple(raw.get("applied_amendment_ids", ())),
            raw.get("last_workflow_outcome"),
            raw.get("last_workflow_summary"),
            None if raw.get("completion_gate") is None else GlobalCompletionGateV1.from_dict(raw["completion_gate"]),
            raw.get("final_report_ref"),
            raw.get("failure_reason"),
            tuple(MissionEventV1.from_dict(item) for item in raw.get("events", ())),
        )


@dataclass(frozen=True, slots=True)
class MissionReportV1:
    mission_id: str
    goal_id: str
    root_task_sha256: str
    plan_id: str
    workflow_invocations: int
    amendment_count: int
    restart_count: int
    completed_todo_ids: tuple[str, ...]
    completion_gate_fingerprint: str
    final_summary: str
    journal_generation: int
    event_count: int

    def __post_init__(self) -> None:
        _safe_id(self.mission_id, "mission_id")
        _safe_id(self.goal_id, "goal_id")
        _text(self.root_task_sha256, "root_task_sha256")
        _safe_id(self.plan_id, "plan_id")
        _non_negative_int(self.workflow_invocations, "phase16_invocations")
        _non_negative_int(self.amendment_count, "amendment_count")
        _non_negative_int(self.restart_count, "restart_count")
        object.__setattr__(self, "completed_todo_ids", _unique_text(self.completed_todo_ids, "completed_todo_ids"))
        _text(self.completion_gate_fingerprint, "completion_gate_fingerprint")
        _text(self.final_summary, "final_summary", max_length=100_000)
        _non_negative_int(self.journal_generation, "journal_generation")
        _non_negative_int(self.event_count, "event_count")

    @property
    def fingerprint(self) -> str:
        return stable_sha256(self.to_dict(include_fingerprint=False))

    def to_dict(self, *, include_fingerprint: bool = True) -> dict[str, Any]:
        raw = {
            "schema_version": 1,
            "mission_id": self.mission_id,
            "goal_id": self.goal_id,
            "root_task_sha256": self.root_task_sha256,
            "plan_id": self.plan_id,
            "phase16_invocations": self.workflow_invocations,
            "amendment_count": self.amendment_count,
            "restart_count": self.restart_count,
            "completed_todo_ids": list(self.completed_todo_ids),
            "completion_gate_fingerprint": self.completion_gate_fingerprint,
            "final_summary": self.final_summary,
            "journal_generation": self.journal_generation,
            "event_count": self.event_count,
        }
        if include_fingerprint:
            raw["fingerprint"] = stable_sha256(raw)
        return raw

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "MissionReportV1":
        if raw.get("schema_version") != 1:
            raise ValueError("MissionReportV1 schema_version must be 1")
        result = cls(
            str(raw["mission_id"]),
            str(raw["goal_id"]),
            str(raw["root_task_sha256"]),
            str(raw["plan_id"]),
            int(raw["phase16_invocations"]),
            int(raw["amendment_count"]),
            int(raw["restart_count"]),
            tuple(raw.get("completed_todo_ids", ())),
            str(raw["completion_gate_fingerprint"]),
            str(raw["final_summary"]),
            int(raw["journal_generation"]),
            int(raw["event_count"]),
        )
        if raw.get("fingerprint") != result.fingerprint:
            raise ValueError("MissionReportV1 fingerprint mismatch")
        return result


@dataclass(frozen=True, slots=True)
class MissionRunResultV1:
    mission_id: str
    state: MissionState
    checkpoint_generation: int
    workflow_invocations: int
    amendment_count: int
    restart_count: int
    completed_todo_ids: tuple[str, ...]
    final_report_ref: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        _safe_id(self.mission_id, "mission_id")
        if not isinstance(self.state, MissionState):
            raise ValueError("state must be MissionState")
        for name in ("checkpoint_generation", "phase16_invocations", "amendment_count", "restart_count"):
            _non_negative_int(getattr(self, name), name)
        object.__setattr__(self, "completed_todo_ids", _unique_text(self.completed_todo_ids, "completed_todo_ids"))
        if self.final_report_ref is not None:
            _text(self.final_report_ref, "final_report_ref")
        if self.reason is not None:
            _text(self.reason, "reason", max_length=32000)


def workflow_summary(result: WorkflowResultV1) -> str:
    summary = result.final_summary or ""
    return (
        f"outcome={result.outcome.value}; completed={len(result.completed_todo_ids)}; "
        f"failed={result.failed_todo_id or '-'}; steps={result.workflow_steps}; "
        f"recovery_cycles={result.recovery_cycles}; {summary}"
    ).strip()


def workflow_is_amendable(result: WorkflowResultV1) -> bool:
    return result.outcome in {WorkflowOutcome.BLOCKED, WorkflowOutcome.NEEDS_ATTENTION}

# Verified legacy import/pickle compatibility.
Phase17Policy = MissionLifecyclePolicy

from harness.compatibility.configuration import install_legacy_fields
install_legacy_fields(MissionLifecyclePolicy, {'max_phase16_invocations_per_run': 'max_workflow_invocations_per_run', 'require_phase16_for_execution': 'require_workflow_for_execution'})

# Version-one constructor/attribute compatibility; wire keys stay byte-stable.
from harness.compatibility.configuration import install_legacy_fields
install_legacy_fields(GlobalCompletionGateV1, {'phase16_succeeded': 'workflow_succeeded'})
install_legacy_fields(MissionCheckpointV1, {'phase16_invocations': 'workflow_invocations'})
install_legacy_fields(MissionReportV1, {'phase16_invocations': 'workflow_invocations'})
install_legacy_fields(MissionRunResultV1, {'phase16_invocations': 'workflow_invocations'})
