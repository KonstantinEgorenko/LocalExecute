'recovery immutable failure-recovery, diagnosis, and replan contracts.'
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Any, Mapping

from ..evaluation.models import BenchmarkDifficulty, BenchmarkTaskClass
from harness.router.policy_models import AmbiguityLevel, NavigationNeed


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _text(value: object, name: str, *, max_length: int | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty string")
    result = value.strip()
    if max_length is not None and len(result) > max_length:
        raise ValueError(f"{name} exceeds max length {max_length}")
    return result


def _confidence(value: object, name: str = "confidence") -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError(f"{name} must be finite numeric")
    result = float(value)
    if not 0.0 <= result <= 1.0:
        raise ValueError(f"{name} must be in [0,1]")
    return result


def _safe_path(value: object, name: str) -> str:
    text = _text(value, name).replace("\\", "/")
    path = PurePosixPath(text)
    if path.is_absolute() or ".." in path.parts or not path.parts or ":" in path.parts[0]:
        raise ValueError(f"{name} must be safe repository-relative path")
    return path.as_posix()


def _unique_texts(value: object, name: str, *, maximum: int | None = None) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{name} must be list/tuple")
    result = tuple(_text(item, name) for item in value)
    if len(result) != len(set(result)):
        raise ValueError(f"{name} must contain unique items")
    if maximum is not None and len(result) > maximum:
        raise ValueError(f"{name} exceeds maximum {maximum}")
    return result


def _unique_paths(value: object, name: str, *, maximum: int | None = None) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{name} must be list/tuple")
    result = tuple(_safe_path(item, name) for item in value)
    if len(result) != len(set(result)):
        raise ValueError(f"{name} must contain unique paths")
    if maximum is not None and len(result) > maximum:
        raise ValueError(f"{name} exceeds maximum {maximum}")
    return result


def _freeze_mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be mapping")
    try:
        plain = json.loads(json.dumps(dict(value), sort_keys=True, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be JSON serializable") from exc
    return MappingProxyType(plain)


class FailureKind(StrEnum):
    WRONG_HYPOTHESIS = "WRONG_HYPOTHESIS"
    WRONG_FILE = "WRONG_FILE"
    INCOMPLETE_IMPLEMENTATION = "INCOMPLETE_IMPLEMENTATION"
    API_SEMANTICS = "API_SEMANTICS"
    TEST_SEMANTICS = "TEST_SEMANTICS"
    CONTEXT_INSUFFICIENT = "CONTEXT_INSUFFICIENT"
    TOOL_PROTOCOL = "TOOL_PROTOCOL"
    INFRASTRUCTURE = "INFRASTRUCTURE"
    UNKNOWN = "UNKNOWN"


def diagnosis_replan_values(failure_kind: FailureKind) -> tuple[bool, ...]:
    """Semantic replan flags admitted by the authoritative diagnosis contract."""
    return (False,) if failure_kind is FailureKind.INFRASTRUCTURE else (False, True)


class RecoveryAction(StrEnum):
    ORNITH_REPLAN = "ORNITH_REPLAN"
    EXPAND_CONTEXT = "EXPAND_CONTEXT"
    RETRY_INFRASTRUCTURE = "RETRY_INFRASTRUCTURE"
    HUMAN_REVIEW = "HUMAN_REVIEW"
    STOP = "STOP"


@dataclass(frozen=True, slots=True)
class RecoveryPolicy:
    diagnosis_model_id: str
    replan_model_id: str
    max_diagnosis_model_attempts: int
    max_replan_model_attempts: int
    max_replans_per_task: int
    min_diagnosis_confidence: float
    max_context_tokens: int
    max_target_files: int
    max_evidence_items: int
    max_response_bytes: int
    require_fresh_workspace: bool
    require_deterministic_verification: bool
    live_recovery_cases: int
    live_min_recovered_rate: float
    live_max_infrastructure_invalid_runs: int

    def __post_init__(self) -> None:
        for name in ("diagnosis_model_id", "replan_model_id"):
            _text(getattr(self, name), name)
        for name in (
            "max_diagnosis_model_attempts", "max_replan_model_attempts", "max_replans_per_task",
            "max_context_tokens", "max_target_files", "max_evidence_items", "max_response_bytes",
            "live_recovery_cases",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be positive integer")
        if not isinstance(self.live_max_infrastructure_invalid_runs, int) or isinstance(self.live_max_infrastructure_invalid_runs, bool) or self.live_max_infrastructure_invalid_runs < 0:
            raise ValueError("live_max_infrastructure_invalid_runs must be integer >= 0")
        object.__setattr__(self, "min_diagnosis_confidence", _confidence(self.min_diagnosis_confidence, "min_diagnosis_confidence"))
        object.__setattr__(self, "live_min_recovered_rate", _confidence(self.live_min_recovered_rate, "live_min_recovered_rate"))
        if self.max_replans_per_task > 2:
            raise ValueError("Phase 15 preserves global max_replans_per_task <= 2")
        if self.require_fresh_workspace is not True:
            raise ValueError("Phase 15 requires fresh workspace for recovery attempts")
        if self.require_deterministic_verification is not True:
            raise ValueError("Phase 15 deterministic verification must remain authoritative")


@dataclass(frozen=True, slots=True)
class RecoveryEvidenceItem:
    evidence_id: str
    evidence_type: str
    summary: str
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        _text(self.evidence_id, "evidence_id")
        _text(self.evidence_type, "evidence_type")
        _text(self.summary, "summary", max_length=4000)
        object.__setattr__(self, "payload", _freeze_mapping(self.payload, "payload"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "evidence_type": self.evidence_type,
            "summary": self.summary,
            "payload": dict(self.payload),
        }


@dataclass(frozen=True, slots=True)
class FailureEvidenceV2:
    task_id: str
    objective: str
    source_task_class: BenchmarkTaskClass
    source_difficulty: BenchmarkDifficulty
    source_metadata: Mapping[str, Any]
    workspace_paths: tuple[str, ...]
    immutable_paths: tuple[str, ...]
    failed_profile_ids: tuple[str, ...]
    evidence_items: tuple[RecoveryEvidenceItem, ...]

    def __post_init__(self) -> None:
        _text(self.task_id, "task_id")
        _text(self.objective, "objective", max_length=12000)
        if not isinstance(self.source_task_class, BenchmarkTaskClass):
            raise ValueError("source_task_class must be BenchmarkTaskClass")
        if not isinstance(self.source_difficulty, BenchmarkDifficulty):
            raise ValueError("source_difficulty must be BenchmarkDifficulty")
        object.__setattr__(self, "source_metadata", _freeze_mapping(self.source_metadata, "source_metadata"))
        object.__setattr__(self, "workspace_paths", _unique_paths(self.workspace_paths, "workspace_paths"))
        object.__setattr__(self, "immutable_paths", _unique_paths(self.immutable_paths, "immutable_paths"))
        object.__setattr__(self, "failed_profile_ids", _unique_texts(self.failed_profile_ids, "failed_profile_ids"))
        if not isinstance(self.evidence_items, tuple) or not self.evidence_items:
            raise ValueError("evidence_items must be non-empty tuple")
        if any(not isinstance(item, RecoveryEvidenceItem) for item in self.evidence_items):
            raise ValueError("evidence_items must contain RecoveryEvidenceItem")
        ids = tuple(item.evidence_id for item in self.evidence_items)
        if len(ids) != len(set(ids)):
            raise ValueError("evidence IDs must be unique")
        domain = self.source_metadata.get('diagnosis_path_domain')
        if domain is not None:
            from ..workspace.diagnosis_domain import DiagnosisPathDomainV1
            parsed = DiagnosisPathDomainV1.from_dict(domain)
            if self.workspace_paths != parsed.workspace_paths or self.immutable_paths != parsed.immutable_paths:
                raise ValueError('diagnosis path domain identity mismatch')
        elif set(self.immutable_paths) - set(self.workspace_paths):
            raise ValueError("immutable_paths must be workspace paths")

    @property
    def allowed_evidence_ids(self) -> frozenset[str]:
        return frozenset(item.evidence_id for item in self.evidence_items)

    @property
    def fingerprint(self) -> str:
        payload = {
            "task_id": self.task_id,
            "objective": self.objective,
            "source_task_class": self.source_task_class.value,
            "source_difficulty": self.source_difficulty.value,
            "source_metadata": dict(self.source_metadata),
            "workspace_paths": list(self.workspace_paths),
            "immutable_paths": list(self.immutable_paths),
            "failed_profile_ids": list(self.failed_profile_ids),
            "evidence_items": [item.to_dict() for item in self.evidence_items],
        }
        return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "objective": self.objective,
            "source_task_class": self.source_task_class.value,
            "source_difficulty": self.source_difficulty.value,
            "source_metadata": dict(self.source_metadata),
            "workspace_paths": list(self.workspace_paths),
            "immutable_paths": list(self.immutable_paths),
            "failed_profile_ids": list(self.failed_profile_ids),
            "allowed_evidence_ids": sorted(self.allowed_evidence_ids),
            "fingerprint": self.fingerprint,
            "evidence_items": [item.to_dict() for item in self.evidence_items],
        }


@dataclass(frozen=True, slots=True)
class FailureDiagnosisV2:
    failure_kind: FailureKind
    confidence: float
    root_cause: str
    evidence: tuple[str, ...]
    implicated_files: tuple[str, ...]
    corrected_invariants: tuple[str, ...]
    replan_required: bool
    context_expansion_required: bool
    can_override_deterministic: bool = False
    invariant_evidence: tuple[tuple[str, ...], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.failure_kind, FailureKind):
            raise ValueError("failure_kind must be FailureKind")
        object.__setattr__(self, "confidence", _confidence(self.confidence))
        _text(self.root_cause, "root_cause", max_length=5000)
        object.__setattr__(self, "evidence", _unique_texts(self.evidence, "evidence", maximum=8))
        if not self.evidence:
            raise ValueError("diagnosis must cite evidence")
        object.__setattr__(self, "implicated_files", _unique_paths(self.implicated_files, "implicated_files", maximum=8))
        object.__setattr__(self, "corrected_invariants", _unique_texts(self.corrected_invariants, "corrected_invariants", maximum=8))
        if self.invariant_evidence:
            if len(self.invariant_evidence) != len(self.corrected_invariants):
                raise ValueError('every corrected invariant requires evidence linkage')
            for refs in self.invariant_evidence:
                if not isinstance(refs, tuple) or not refs or _unique_texts(refs, 'invariant evidence', maximum=8) != refs:
                    raise ValueError('invalid invariant evidence linkage')
        if not isinstance(self.replan_required, bool) or not isinstance(self.context_expansion_required, bool):
            raise ValueError("replan/context flags must be bool")
        if self.can_override_deterministic is not False:
            raise ValueError("diagnosis cannot override deterministic verification")
        if self.replan_required not in diagnosis_replan_values(self.failure_kind):
            raise ValueError("infrastructure diagnosis must not request semantic replan")

    def to_dict(self) -> dict[str, Any]:
        return {
            "failure_kind": self.failure_kind.value,
            "confidence": self.confidence,
            "root_cause": self.root_cause,
            "evidence": list(self.evidence),
            "implicated_files": list(self.implicated_files),
            "corrected_invariants": list(self.corrected_invariants),
            "replan_required": self.replan_required,
            "context_expansion_required": self.context_expansion_required,
            "can_override_deterministic": False,
            **({'invariant_evidence': [list(x) for x in self.invariant_evidence]} if self.invariant_evidence else {}),
        }


@dataclass(frozen=True, slots=True)
class ReplanContractV2:
    source_task_id: str
    revised_objective: str
    task_class: BenchmarkTaskClass
    difficulty: BenchmarkDifficulty
    target_files: tuple[str, ...]
    navigation_need: NavigationNeed
    context_tokens: int
    dependency_depth: int
    ambiguity: AmbiguityLevel
    risk: AmbiguityLevel
    required_evidence: tuple[str, ...]
    constraints: tuple[str, ...]
    fresh_workspace_required: bool
    preserve_tests: bool
    confidence: float

    def __post_init__(self) -> None:
        _text(self.source_task_id, "source_task_id")
        _text(self.revised_objective, "revised_objective", max_length=12000)
        if not isinstance(self.task_class, BenchmarkTaskClass):
            raise ValueError("task_class must be BenchmarkTaskClass")
        if not isinstance(self.difficulty, BenchmarkDifficulty):
            raise ValueError("difficulty must be BenchmarkDifficulty")
        object.__setattr__(self, "target_files", _unique_paths(self.target_files, "target_files", maximum=8))
        if not self.target_files:
            raise ValueError("target_files must be non-empty")
        if not isinstance(self.navigation_need, NavigationNeed):
            raise ValueError("navigation_need must be NavigationNeed")
        if not isinstance(self.context_tokens, int) or isinstance(self.context_tokens, bool) or self.context_tokens <= 0:
            raise ValueError("context_tokens must be integer > 0")
        if not isinstance(self.dependency_depth, int) or isinstance(self.dependency_depth, bool) or self.dependency_depth < 0:
            raise ValueError("dependency_depth must be integer >= 0")
        if not isinstance(self.ambiguity, AmbiguityLevel) or not isinstance(self.risk, AmbiguityLevel):
            raise ValueError("ambiguity/risk must be AmbiguityLevel")
        object.__setattr__(self, "required_evidence", _unique_texts(self.required_evidence, "required_evidence", maximum=8))
        object.__setattr__(self, "constraints", _unique_texts(self.constraints, "constraints", maximum=12))
        if self.fresh_workspace_required is not True:
            raise ValueError("fresh_workspace_required must be true")
        if self.preserve_tests is not True:
            raise ValueError("preserve_tests must be true")
        object.__setattr__(self, "confidence", _confidence(self.confidence))

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_task_id": self.source_task_id,
            "revised_objective": self.revised_objective,
            "task_class": self.task_class.value,
            "difficulty": self.difficulty.value,
            "target_files": list(self.target_files),
            "navigation_need": self.navigation_need.value,
            "context_tokens": self.context_tokens,
            "dependency_depth": self.dependency_depth,
            "ambiguity": self.ambiguity.value,
            "risk": self.risk.value,
            "required_evidence": list(self.required_evidence),
            "constraints": list(self.constraints),
            "fresh_workspace_required": True,
            "preserve_tests": True,
            "confidence": self.confidence,
        }


@dataclass(frozen=True, slots=True)
class ContextExpansionRequestV2:
    task_id: str
    reason: str
    required_paths: tuple[str, ...]
    evidence: tuple[str, ...]
    max_context_tokens: int

    def __post_init__(self) -> None:
        _text(self.task_id, "task_id")
        _text(self.reason, "reason", max_length=4000)
        object.__setattr__(self, "required_paths", _unique_paths(self.required_paths, "required_paths", maximum=16))
        object.__setattr__(self, "evidence", _unique_texts(self.evidence, "evidence", maximum=8))
        if not isinstance(self.max_context_tokens, int) or isinstance(self.max_context_tokens, bool) or self.max_context_tokens <= 0:
            raise ValueError("max_context_tokens must be positive integer")

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "reason": self.reason,
            "required_paths": list(self.required_paths),
            "evidence": list(self.evidence),
            "max_context_tokens": self.max_context_tokens,
        }
