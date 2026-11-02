'workflow immutable long-horizon workflow/context contracts.'
from __future__ import annotations
from harness.compatibility.configuration import resolve_config_path, normalize_configuration_document

import hashlib
import json
import math
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping

import yaml

from ..errors import ConfigurationError


def _text(value: object, name: str, *, max_length: int | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty string")
    if max_length is not None and len(value) > max_length:
        raise ValueError(f"{name} exceeds max_length={max_length}")
    return value


def _non_negative_int(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be integer >= 0")
    return value


def _positive_int(value: object, name: str) -> int:
    result = _non_negative_int(value, name)
    if result == 0:
        raise ValueError(f"{name} must be > 0")
    return result


def _unique_text(values: object, name: str) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        raise ValueError(f"{name} must be list/tuple")
    result = tuple(_text(item, name) for item in values)
    if len(set(result)) != len(result):
        raise ValueError(f"{name} must not contain duplicates")
    return result


def _safe_path(value: str, name: str) -> str:
    value = _text(value, name).replace("\\", "/").strip("/")
    if value in {".", ".."} or value.startswith("../") or "/../" in f"/{value}/":
        raise ValueError(f"{name} must be safe repository-relative path")
    return value


def _sha(value: object, name: str) -> str:
    value = _text(value, name).lower()
    if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
        raise ValueError(f"{name} must be lowercase SHA-256 hex")
    return value


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
    if isinstance(value, (list, tuple)):
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


class WorkflowOutcome(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    NEEDS_ATTENTION = "NEEDS_ATTENTION"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    BLOCKED = "BLOCKED"


class AttemptDisposition(StrEnum):
    VERIFIED = "VERIFIED"
    RECOVERABLE_FAILURE = "RECOVERABLE_FAILURE"
    TERMINAL_FAILURE = "TERMINAL_FAILURE"
    INFRASTRUCTURE_RETRY = "INFRASTRUCTURE_RETRY"


@dataclass(frozen=True, slots=True)
class ContextProfilePolicy:
    profile_id: str
    model_context_tokens: int
    initial_pack_tokens: int
    max_items: int
    max_bytes: int
    feedback_reserve_tokens: int
    output_reserve_tokens: int
    safety_reserve_tokens: int

    def __post_init__(self) -> None:
        _text(self.profile_id, "profile_id")
        for name in (
            "model_context_tokens", "initial_pack_tokens", "max_items", "max_bytes",
            "feedback_reserve_tokens", "output_reserve_tokens", "safety_reserve_tokens",
        ):
            _positive_int(getattr(self, name), name)
        reserved = self.initial_pack_tokens + self.feedback_reserve_tokens + self.output_reserve_tokens + self.safety_reserve_tokens
        if reserved > self.model_context_tokens:
            raise ValueError(
                f"context reserves exceed model context for {self.profile_id}: {reserved}>{self.model_context_tokens}"
            )
        if self.initial_pack_tokens > int(self.model_context_tokens * 0.55):
            raise ValueError("initial context pack must not consume >55% of model context")

    @property
    def reserved_tokens(self) -> int:
        return self.feedback_reserve_tokens + self.output_reserve_tokens + self.safety_reserve_tokens

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "model_context_tokens": self.model_context_tokens,
            "initial_pack_tokens": self.initial_pack_tokens,
            "max_items": self.max_items,
            "max_bytes": self.max_bytes,
            "feedback_reserve_tokens": self.feedback_reserve_tokens,
            "output_reserve_tokens": self.output_reserve_tokens,
            "safety_reserve_tokens": self.safety_reserve_tokens,
        }


@dataclass(frozen=True, slots=True)
class WorkflowExecutionPolicy:
    max_workflow_steps: int
    max_recovery_cycles_per_todo: int
    max_context_expansions_per_todo: int
    max_dependency_outcomes: int
    max_optional_repo_artifacts: int
    max_verified_outcome_chars: int
    serialize_gpu_execution: bool
    require_root_goal: bool
    require_project_snapshot: bool
    require_dependency_outcomes: bool
    require_deterministic_verification: bool
    require_fresh_recovery_workspace: bool
    profile_contexts: Mapping[str, ContextProfilePolicy]
    live_workflow_cases: int
    live_min_workflow_success_rate: float
    live_max_infrastructure_invalid_runs: int

    def __post_init__(self) -> None:
        for name in (
            "max_workflow_steps", "max_recovery_cycles_per_todo", "max_context_expansions_per_todo",
            "max_dependency_outcomes", "max_optional_repo_artifacts", "max_verified_outcome_chars",
            "live_workflow_cases",
        ):
            _positive_int(getattr(self, name), name)
        _non_negative_int(self.live_max_infrastructure_invalid_runs, "live_max_infrastructure_invalid_runs")
        if not isinstance(self.live_min_workflow_success_rate, (int, float)) or isinstance(self.live_min_workflow_success_rate, bool):
            raise ValueError("live_min_workflow_success_rate must be number")
        if not math.isfinite(float(self.live_min_workflow_success_rate)) or not 0 <= float(self.live_min_workflow_success_rate) <= 1:
            raise ValueError("live_min_workflow_success_rate must be in [0,1]")
        for name in (
            "serialize_gpu_execution", "require_root_goal", "require_project_snapshot",
            "require_dependency_outcomes", "require_deterministic_verification", "require_fresh_recovery_workspace",
        ):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be bool")
        if self.serialize_gpu_execution is not True:
            raise ValueError("Phase 16 V1 serializes physical GPU execution")
        if self.require_root_goal is not True or self.require_project_snapshot is not True:
            raise ValueError("Phase 16 requires root goal and project snapshot in every execution context")
        if self.require_dependency_outcomes is not True:
            raise ValueError("Phase 16 requires verified dependency outcomes")
        if self.require_deterministic_verification is not True:
            raise ValueError("deterministic verification remains authoritative")
        if self.require_fresh_recovery_workspace is not True:
            raise ValueError("Phase 16 recovery must preserve Phase 15 fresh-workspace invariant")
        profiles = dict(self.profile_contexts)
        if not profiles:
            raise ValueError("profile_contexts must not be empty")
        if any(not isinstance(value, ContextProfilePolicy) for value in profiles.values()):
            raise ValueError("profile_contexts values must be ContextProfilePolicy")
        if any(key != value.profile_id for key, value in profiles.items()):
            raise ValueError("profile_contexts key/profile_id mismatch")
        object.__setattr__(self, "profile_contexts", MappingProxyType(dict(sorted(profiles.items()))))

    @classmethod
    def from_project_root(cls, root: object) -> "WorkflowExecutionPolicy":
        from pathlib import Path

        path = resolve_config_path(Path(root) / "config" / "phase16.yaml")
        try:
            raw = normalize_configuration_document(yaml.safe_load(path.read_text(encoding="utf-8")))
        except OSError as exc:
            raise ConfigurationError(f"cannot read Phase 16 config: {exc}") from exc
        except yaml.YAMLError as exc:
            raise ConfigurationError(f"malformed Phase 16 YAML: {exc}") from exc
        if not isinstance(raw, Mapping) or raw.get("schema_version") != 1:
            raise ConfigurationError("phase16.yaml requires schema_version: 1")
        body = raw.get("autonomous_workflow_v1")
        if not isinstance(body, Mapping):
            raise ConfigurationError("phase16.yaml.autonomous_workflow_v1 must be mapping")
        contexts = body.get("context_profiles")
        if not isinstance(contexts, Mapping):
            raise ConfigurationError("phase16 context_profiles must be mapping")
        profiles: dict[str, ContextProfilePolicy] = {}
        try:
            for profile_id, item in contexts.items():
                if not isinstance(item, Mapping):
                    raise ValueError(f"context profile {profile_id} must be mapping")
                profiles[str(profile_id)] = ContextProfilePolicy(
                    str(profile_id),
                    int(item["model_context_tokens"]),
                    int(item["initial_pack_tokens"]),
                    int(item["max_items"]),
                    int(item["max_bytes"]),
                    int(item["feedback_reserve_tokens"]),
                    int(item["output_reserve_tokens"]),
                    int(item["safety_reserve_tokens"]),
                )
            live = body.get("live_acceptance", {})
            if not isinstance(live, Mapping):
                raise ValueError("live_acceptance must be mapping")
            return cls(
                int(body["max_workflow_steps"]),
                int(body["max_recovery_cycles_per_todo"]),
                int(body["max_context_expansions_per_todo"]),
                int(body["max_dependency_outcomes"]),
                int(body["max_optional_repo_artifacts"]),
                int(body["max_verified_outcome_chars"]),
                bool(body["serialize_gpu_execution"]),
                bool(body["require_root_goal"]),
                bool(body["require_project_snapshot"]),
                bool(body["require_dependency_outcomes"]),
                bool(body["require_deterministic_verification"]),
                bool(body["require_fresh_recovery_workspace"]),
                profiles,
                int(live["workflow_cases"]),
                float(live["min_workflow_success_rate"]),
                int(live["max_infrastructure_invalid_runs"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigurationError(f"invalid Phase 16 policy: {exc}") from exc

    def profile(self, profile_id: str) -> ContextProfilePolicy:
        try:
            return self.profile_contexts[profile_id]
        except KeyError as exc:
            raise ValueError(f"no Phase 16 context policy for profile: {profile_id}") from exc


@dataclass(frozen=True, slots=True)
class ProjectSnapshotV1:
    goal_id: str
    epoch: int
    head_commit: str
    diff_sha256: str
    dirty_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _text(self.goal_id, "goal_id")
        _non_negative_int(self.epoch, "epoch")
        _text(self.head_commit, "head_commit")
        _sha(self.diff_sha256, "diff_sha256")
        paths = tuple(sorted({_safe_path(item, "dirty_paths") for item in self.dirty_paths}, key=str.casefold))
        object.__setattr__(self, "dirty_paths", paths)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "goal_id": self.goal_id,
            "epoch": self.epoch,
            "head_commit": self.head_commit,
            "diff_sha256": self.diff_sha256,
            "dirty_paths": list(self.dirty_paths),
        }


@dataclass(frozen=True, slots=True)
class VerifiedTodoOutcomeV1:
    goal_id: str
    todo_id: str
    run_id: str
    objective: str
    summary: str
    epoch_before: int
    epoch_after: int
    changed_paths: tuple[str, ...]
    verification_report_id: str
    verification_fingerprint: str
    invariants: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("goal_id", "todo_id", "run_id", "objective", "summary", "verification_report_id", "verification_fingerprint"):
            _text(getattr(self, name), name, max_length=16000 if name in {"objective", "summary"} else None)
        _non_negative_int(self.epoch_before, "epoch_before")
        _positive_int(self.epoch_after, "epoch_after")
        if self.epoch_after != self.epoch_before + 1:
            raise ValueError("epoch_after must equal epoch_before + 1")
        object.__setattr__(self, "changed_paths", tuple(sorted({_safe_path(item, "changed_paths") for item in self.changed_paths}, key=str.casefold)))
        object.__setattr__(self, "invariants", _unique_text(self.invariants, "invariants"))
        object.__setattr__(self, "evidence_ids", _unique_text(self.evidence_ids, "evidence_ids"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "goal_id": self.goal_id,
            "todo_id": self.todo_id,
            "run_id": self.run_id,
            "objective": self.objective,
            "summary": self.summary,
            "epoch_before": self.epoch_before,
            "epoch_after": self.epoch_after,
            "changed_paths": list(self.changed_paths),
            "verification_report_id": self.verification_report_id,
            "verification_fingerprint": self.verification_fingerprint,
            "invariants": list(self.invariants),
            "evidence_ids": list(self.evidence_ids),
        }


@dataclass(frozen=True, slots=True)
class ContextPackV1:
    goal_id: str
    todo_id: str
    profile_id: str
    project_epoch: int
    context_fingerprint: str
    used_tokens: int
    max_tokens: int
    required_artifact_ids: tuple[str, ...]
    selected_artifact_ids: tuple[str, ...]
    omitted_optional_artifact_ids: tuple[str, ...]
    rendered_text: str

    def __post_init__(self) -> None:
        for name in ("goal_id", "todo_id", "profile_id", "context_fingerprint", "rendered_text"):
            _text(getattr(self, name), name)
        _non_negative_int(self.project_epoch, "project_epoch")
        _non_negative_int(self.used_tokens, "used_tokens")
        _positive_int(self.max_tokens, "max_tokens")
        if self.used_tokens > self.max_tokens:
            raise ValueError("context pack exceeds max_tokens")
        _sha(self.context_fingerprint, "context_fingerprint")
        required = _unique_text(self.required_artifact_ids, "required_artifact_ids")
        selected = _unique_text(self.selected_artifact_ids, "selected_artifact_ids")
        omitted = _unique_text(self.omitted_optional_artifact_ids, "omitted_optional_artifact_ids")
        if not set(required) <= set(selected):
            raise ValueError("all required artifacts must be selected")
        if set(selected) & set(omitted):
            raise ValueError("selected and omitted artifacts overlap")
        object.__setattr__(self, "required_artifact_ids", required)
        object.__setattr__(self, "selected_artifact_ids", selected)
        object.__setattr__(self, "omitted_optional_artifact_ids", omitted)

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal_id": self.goal_id,
            "todo_id": self.todo_id,
            "profile_id": self.profile_id,
            "project_epoch": self.project_epoch,
            "context_fingerprint": self.context_fingerprint,
            "used_tokens": self.used_tokens,
            "max_tokens": self.max_tokens,
            "required_artifact_ids": list(self.required_artifact_ids),
            "selected_artifact_ids": list(self.selected_artifact_ids),
            "omitted_optional_artifact_ids": list(self.omitted_optional_artifact_ids),
            "rendered_text": self.rendered_text,
        }


@dataclass(frozen=True, slots=True)
class WorkflowTodoV1:
    todo_id: str
    run_id: str
    objective: str
    dependencies: tuple[str, ...]
    work_contract: Any
    routing_task_spec: Any

    def __post_init__(self) -> None:
        for name in ("todo_id", "run_id", "objective"):
            _text(getattr(self, name), name)
        object.__setattr__(self, "dependencies", _unique_text(self.dependencies, "dependencies"))
        if not hasattr(self.work_contract, "to_dict"):
            raise ValueError("work_contract must expose to_dict")


@dataclass(frozen=True, slots=True)
class ProfileSelectionV1:
    profile_id: str
    cascade_profile_ids: tuple[str, ...] = ()
    reason: str = "router"

    def __post_init__(self) -> None:
        _text(self.profile_id, "profile_id")
        object.__setattr__(self, "cascade_profile_ids", _unique_text(self.cascade_profile_ids, "cascade_profile_ids"))
        _text(self.reason, "reason")
        if self.profile_id in self.cascade_profile_ids:
            raise ValueError("selected profile must not repeat in cascade_profile_ids")


@dataclass(frozen=True, slots=True)
class ExecutionObservationV1:
    profile_id: str
    finalized: bool
    changed_paths: tuple[str, ...]
    model_calls: int
    tool_calls: int
    outcome: str
    summary: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _text(self.profile_id, "profile_id")
        if not isinstance(self.finalized, bool):
            raise ValueError("finalized must be bool")
        object.__setattr__(self, "changed_paths", tuple(sorted({_safe_path(item, "changed_paths") for item in self.changed_paths}, key=str.casefold)))
        _non_negative_int(self.model_calls, "model_calls")
        _non_negative_int(self.tool_calls, "tool_calls")
        _text(self.outcome, "outcome")
        if self.summary is not None and not isinstance(self.summary, str):
            raise ValueError("summary must be str|None")
        object.__setattr__(self, "raw", _freeze_json(self.raw, "raw"))


@dataclass(frozen=True, slots=True)
class VerificationObservationV1:
    passed: bool
    report_id: str
    fingerprint: str
    summary: str
    evidence_ids: tuple[str, ...] = ()
    raw: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.passed, bool):
            raise ValueError("passed must be bool")
        for name in ("report_id", "fingerprint", "summary"):
            _text(getattr(self, name), name)
        object.__setattr__(self, "evidence_ids", _unique_text(self.evidence_ids, "evidence_ids"))
        object.__setattr__(self, "raw", _freeze_json(self.raw, "raw"))


@dataclass(frozen=True, slots=True)
class RecoveryObservationV1:
    continue_work: bool
    revised_objective: str | None = None
    context_artifact_ids: tuple[str, ...] = ()
    reason: str = "recovery"
    routing_task_spec: Any | None = None
    expansion_queries: tuple[str, ...] = ()
    raw: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.continue_work, bool):
            raise ValueError("continue_work must be bool")
        if self.revised_objective is not None:
            _text(self.revised_objective, "revised_objective")
        object.__setattr__(self, "context_artifact_ids", _unique_text(self.context_artifact_ids, "context_artifact_ids"))
        object.__setattr__(self, "expansion_queries", _unique_text(self.expansion_queries, "expansion_queries"))
        object.__setattr__(self, "raw", _freeze_json(self.raw, "raw"))
        _text(self.reason, "reason")
        if self.continue_work and self.revised_objective is None:
            raise ValueError("continuing recovery requires revised_objective")


@dataclass(frozen=True, slots=True)
class WorkflowResultV1:
    outcome: WorkflowOutcome
    goal_id: str
    completed_todo_ids: tuple[str, ...]
    failed_todo_id: str | None
    workflow_steps: int
    recovery_cycles: int
    context_packs_built: int
    final_summary: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, WorkflowOutcome):
            raise ValueError("outcome must be WorkflowOutcome")
        _text(self.goal_id, "goal_id")
        object.__setattr__(self, "completed_todo_ids", _unique_text(self.completed_todo_ids, "completed_todo_ids"))
        if self.failed_todo_id is not None:
            _text(self.failed_todo_id, "failed_todo_id")
        _non_negative_int(self.workflow_steps, "workflow_steps")
        _non_negative_int(self.recovery_cycles, "recovery_cycles")
        _non_negative_int(self.context_packs_built, "context_packs_built")
        if self.final_summary is not None and not isinstance(self.final_summary, str):
            raise ValueError("final_summary must be str|None")

@dataclass(frozen=True, slots=True)
class ReviewObservationV1:
    accept: bool
    recovery_required: bool
    decision: str
    summary: str
    evidence_ids: tuple[str, ...] = ()
    raw: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.accept, bool) or not isinstance(self.recovery_required, bool):
            raise ValueError("review flags must be bool")
        _text(self.decision, "decision")
        _text(self.summary, "summary")
        object.__setattr__(self, "evidence_ids", _unique_text(self.evidence_ids, "evidence_ids"))
        object.__setattr__(self, "raw", _freeze_json(self.raw, "raw"))
        if self.accept and self.recovery_required:
            raise ValueError("accepted review cannot require recovery")

# Verified legacy import/pickle compatibility.
Phase16Policy = WorkflowExecutionPolicy
