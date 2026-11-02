'Immutable critic critic contracts.'
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping

from ..verification.models import VerificationOutcome


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _confidence(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("confidence must be finite numeric")
    value = float(value)
    if not 0.0 <= value <= 1.0:
        raise ValueError("confidence must be in [0, 1]")
    return value


def _unique_text(values: object, name: str) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        raise ValueError(f"{name} must be a list/tuple")
    result = tuple(_text(item, name) for item in values)
    if len(result) != len(set(result)):
        raise ValueError(f"{name} must not contain duplicates")
    return result


class CriticVerdict(StrEnum):
    ACCEPT = "ACCEPT"
    CONCERN = "CONCERN"
    RETRY = "RETRY"
    ESCALATE = "ESCALATE"
    REPLAN = "REPLAN"
    BLOCKED = "BLOCKED"


class FailureClass(StrEnum):
    NONE = "NONE"
    IMPLEMENTATION_ERROR = "IMPLEMENTATION_ERROR"
    TEST_FAILURE = "TEST_FAILURE"
    ENVIRONMENT_ERROR = "ENVIRONMENT_ERROR"
    TOOL_ERROR = "TOOL_ERROR"
    CONTRACT_ERROR = "CONTRACT_ERROR"
    CONTEXT_INSUFFICIENT = "CONTEXT_INSUFFICIENT"
    ROUTING_ERROR = "ROUTING_ERROR"
    WORKER_ERROR = "WORKER_ERROR"
    UNKNOWN = "UNKNOWN"


class CriticAction(StrEnum):
    NONE = "NONE"
    RETRY_SAME_WORKER = "RETRY_SAME_WORKER"
    RETRY_WITH_CONTEXT = "RETRY_WITH_CONTEXT"
    ESCALATE_WORKER = "ESCALATE_WORKER"
    REPLAN = "REPLAN"
    HUMAN_REVIEW = "HUMAN_REVIEW"
    STOP = "STOP"


class CriticTurnStatus(StrEnum):
    RESPONSE = "RESPONSE"
    EXECUTION_ERROR = "EXECUTION_ERROR"


@dataclass(frozen=True, slots=True)
class CriticPolicy:
    model_id: str = "ministral_reasoner"
    max_model_attempts: int = 2
    max_evidence_items: int = 32
    max_diff_bytes: int = 131072
    max_artifact_bytes: int = 131072
    max_prompt_bytes: int = 262144
    max_response_bytes: int = 65536
    temperature: float = 0.0
    keep_alive: int | str = -1
    enforce_no_workspace_mutation: bool = True

    def __post_init__(self) -> None:
        _text(self.model_id, "model_id")
        if self.model_id != "ministral_reasoner":
            raise ValueError("Phase 8 critic model_id must be ministral_reasoner")
        for name in ("max_model_attempts", "max_evidence_items", "max_diff_bytes", "max_artifact_bytes", "max_prompt_bytes", "max_response_bytes"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be positive integer")
        _confidence(self.temperature / 2.0 if self.temperature > 1 else self.temperature)
        if not 0.0 <= self.temperature <= 2.0:
            raise ValueError("temperature must be in [0, 2]")
        if not isinstance(self.keep_alive, (int, str)) or isinstance(self.keep_alive, bool):
            raise ValueError("keep_alive must be int or str")
        if not isinstance(self.enforce_no_workspace_mutation, bool):
            raise ValueError("enforce_no_workspace_mutation must be bool")

    @classmethod
    def from_policies(cls, policies: Mapping[str, Any]) -> "CriticPolicy":
        raw = policies.get("critic", {})
        if not isinstance(raw, Mapping):
            raise ValueError("policies.critic must be a mapping")
        return cls(
            model_id=str(raw.get("model", "ministral_reasoner")),
            max_model_attempts=int(raw.get("max_model_attempts", 2)),
            max_evidence_items=int(raw.get("max_evidence_items", 32)),
            max_diff_bytes=int(raw.get("max_diff_bytes", 131072)),
            max_artifact_bytes=int(raw.get("max_artifact_bytes", 131072)),
            max_prompt_bytes=int(raw.get("max_prompt_bytes", 262144)),
            max_response_bytes=int(raw.get("max_response_bytes", 65536)),
            temperature=float(raw.get("temperature", 0.0)),
            keep_alive=raw.get("keep_alive", -1),
            enforce_no_workspace_mutation=bool(raw.get("enforce_no_workspace_mutation", True)),
        )


@dataclass(frozen=True, slots=True)
class CriticEvidenceItem:
    evidence_id: str
    evidence_type: str
    source: str
    status: str
    summary: str
    reference: str | None = None

    def __post_init__(self) -> None:
        for name in ("evidence_id", "evidence_type", "source", "status", "summary"):
            _text(getattr(self, name), name)
        if self.reference is not None:
            _text(self.reference, "reference")

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "evidence_type": self.evidence_type,
            "source": self.source,
            "status": self.status,
            "summary": self.summary,
            "reference": self.reference,
        }


@dataclass(frozen=True, slots=True)
class CriticEvidenceBundle:
    request_id: str
    request_fingerprint: str
    run_id: str
    goal_id: str
    todo_id: str
    objective: str
    verification_report_id: str
    verification_outcome: VerificationOutcome
    candidate_head: str
    candidate_diff_sha256: str
    candidate_diff_text: str
    dirty_paths: tuple[str, ...]
    work_contract: Mapping[str, Any]
    verification_report: Mapping[str, Any]
    evidence_items: tuple[CriticEvidenceItem, ...]
    original_source_revision_identity: str | None = None

    def __post_init__(self) -> None:
        for name in ("request_id", "request_fingerprint", "run_id", "goal_id", "todo_id", "objective", "verification_report_id", "candidate_head", "candidate_diff_sha256"):
            _text(getattr(self, name), name)
        if not isinstance(self.verification_outcome, VerificationOutcome):
            raise ValueError("verification_outcome must be VerificationOutcome")
        if not isinstance(self.candidate_diff_text, str):
            raise ValueError("candidate_diff_text must be string")
        object.__setattr__(self, "dirty_paths", _unique_text(self.dirty_paths, "dirty_paths"))
        object.__setattr__(self, "work_contract", MappingProxyType(dict(self.work_contract)))
        object.__setattr__(self, "verification_report", MappingProxyType(dict(self.verification_report)))
        if any(not isinstance(item, CriticEvidenceItem) for item in self.evidence_items):
            raise ValueError("evidence_items must contain CriticEvidenceItem")
        ids = [item.evidence_id for item in self.evidence_items]
        if len(ids) != len(set(ids)):
            raise ValueError("evidence_items IDs must be unique")

    @property
    def allowed_evidence_ids(self) -> frozenset[str]:
        return frozenset(item.evidence_id for item in self.evidence_items)


@dataclass(frozen=True, slots=True)
class CriticAnalysis:
    deterministic_outcome: VerificationOutcome
    verdict: CriticVerdict
    failure_class: FailureClass
    confidence: float
    summary: str
    evidence: tuple[str, ...]
    recommended_action: CriticAction
    retry_guidance: tuple[str, ...]
    can_override_deterministic: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.deterministic_outcome, VerificationOutcome):
            raise ValueError("deterministic_outcome must be VerificationOutcome")
        if not isinstance(self.verdict, CriticVerdict) or not isinstance(self.failure_class, FailureClass) or not isinstance(self.recommended_action, CriticAction):
            raise ValueError("critic enum fields are invalid")
        object.__setattr__(self, "confidence", _confidence(self.confidence))
        _text(self.summary, "summary")
        object.__setattr__(self, "evidence", _unique_text(self.evidence, "evidence"))
        object.__setattr__(self, "retry_guidance", _unique_text(self.retry_guidance, "retry_guidance"))
        if self.can_override_deterministic is not False:
            raise ValueError("critic cannot override deterministic verification")
        if self.deterministic_outcome is VerificationOutcome.FAILED:
            if self.verdict is CriticVerdict.ACCEPT:
                raise ValueError("critic cannot ACCEPT a deterministically failed candidate")
            if self.failure_class is FailureClass.NONE:
                raise ValueError("failed verification requires a failure_class")
            if self.recommended_action is CriticAction.NONE:
                raise ValueError("failed verification requires a recommended action")
        if self.deterministic_outcome is VerificationOutcome.PASSED and self.verdict is CriticVerdict.ACCEPT:
            if self.failure_class is not FailureClass.NONE or self.recommended_action is not CriticAction.NONE:
                raise ValueError("ACCEPT on passing verification requires NONE failure/action")

    def to_dict(self) -> dict[str, Any]:
        return {
            "deterministic_outcome": self.deterministic_outcome.value,
            "verdict": self.verdict.value,
            "failure_class": self.failure_class.value,
            "confidence": self.confidence,
            "summary": self.summary,
            "evidence": list(self.evidence),
            "recommended_action": self.recommended_action.value,
            "retry_guidance": list(self.retry_guidance),
            "can_override_deterministic": False,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CriticAnalysis":
        return cls(
            VerificationOutcome(str(payload["deterministic_outcome"])),
            CriticVerdict(str(payload["verdict"])),
            FailureClass(str(payload["failure_class"])),
            payload["confidence"],
            str(payload["summary"]),
            tuple(payload["evidence"]),
            CriticAction(str(payload["recommended_action"])),
            tuple(payload["retry_guidance"]),
            payload["can_override_deterministic"],
        )


@dataclass(frozen=True, slots=True)
class CriticTurnRecord:
    request_id: str
    attempt_index: int
    status: CriticTurnStatus
    model_id: str | None
    model_tag: str | None
    content: str | None
    error: str | None
    prompt_eval_count: int | None = None
    eval_count: int | None = None
    total_duration: int | None = None

    def __post_init__(self) -> None:
        _text(self.request_id, "request_id")
        if not isinstance(self.attempt_index, int) or isinstance(self.attempt_index, bool) or self.attempt_index < 0:
            raise ValueError("attempt_index must be non-negative int")
        if not isinstance(self.status, CriticTurnStatus):
            raise ValueError("status must be CriticTurnStatus")
        if self.status is CriticTurnStatus.RESPONSE:
            _text(self.model_id, "model_id")
            _text(self.model_tag, "model_tag")
            if not isinstance(self.content, str):
                raise ValueError("response turn requires content")
            if self.error is not None:
                raise ValueError("response turn cannot have error")
        else:
            _text(self.error, "error")

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "attempt_index": self.attempt_index,
            "status": self.status.value,
            "model_id": self.model_id,
            "model_tag": self.model_tag,
            "content": self.content,
            "error": self.error,
            "prompt_eval_count": self.prompt_eval_count,
            "eval_count": self.eval_count,
            "total_duration": self.total_duration,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CriticTurnRecord":
        return cls(
            str(payload["request_id"]), int(payload["attempt_index"]), CriticTurnStatus(str(payload["status"])),
            payload.get("model_id"), payload.get("model_tag"), payload.get("content"), payload.get("error"),
            payload.get("prompt_eval_count"), payload.get("eval_count"), payload.get("total_duration"),
        )


@dataclass(frozen=True, slots=True)
class CriticReport:
    report_id: str
    request_fingerprint: str
    run_id: str
    todo_id: str
    verification_report_id: str
    candidate_diff_sha256: str
    analysis: CriticAnalysis
    model_id: str
    model_tag: str
    model_attempts: int
    raw_turn_artifact_id: str
    created_at: str

    def __post_init__(self) -> None:
        for name in ("report_id", "request_fingerprint", "run_id", "todo_id", "verification_report_id", "candidate_diff_sha256", "model_id", "model_tag", "raw_turn_artifact_id", "created_at"):
            _text(getattr(self, name), name)
        if not isinstance(self.analysis, CriticAnalysis):
            raise ValueError("analysis must be CriticAnalysis")
        if not isinstance(self.model_attempts, int) or isinstance(self.model_attempts, bool) or self.model_attempts <= 0:
            raise ValueError("model_attempts must be positive int")

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "request_fingerprint": self.request_fingerprint,
            "run_id": self.run_id,
            "todo_id": self.todo_id,
            "verification_report_id": self.verification_report_id,
            "candidate_diff_sha256": self.candidate_diff_sha256,
            "analysis": self.analysis.to_dict(),
            "model_id": self.model_id,
            "model_tag": self.model_tag,
            "model_attempts": self.model_attempts,
            "raw_turn_artifact_id": self.raw_turn_artifact_id,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CriticReport":
        return cls(
            str(payload["report_id"]), str(payload["request_fingerprint"]), str(payload["run_id"]), str(payload["todo_id"]),
            str(payload["verification_report_id"]), str(payload["candidate_diff_sha256"]), CriticAnalysis.from_dict(payload["analysis"]),
            str(payload["model_id"]), str(payload["model_tag"]), int(payload["model_attempts"]), str(payload["raw_turn_artifact_id"]), str(payload["created_at"]),
        )


@dataclass(frozen=True, slots=True)
class CriticCoordinatorResult:
    report: CriticReport
    reused_existing_report: bool
    model_calls: int
    reused_turns: int


def request_fingerprint(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
