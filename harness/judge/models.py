'Immutable contracts for judge Ornith Judge / final synthesis.'
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping

from ..critic.models import CriticReport, CriticVerdict
from ..verification.models import VerificationOutcome, VerificationReport
from ..verification.acceptance import AcceptanceObligationsV1, AcceptanceEvidenceV1


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _unique_text(values: object, name: str) -> tuple[str, ...]:
    if not isinstance(values, (tuple, list)):
        raise ValueError(f"{name} must be list or tuple")
    result = tuple(_text(item, name) for item in values)
    if len(set(result)) != len(result):
        raise ValueError(f"{name} must not contain duplicates")
    return result


def _confidence(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("confidence must be numeric")
    result = float(value)
    if not math.isfinite(result) or not 0.0 <= result <= 1.0:
        raise ValueError("confidence must be finite in [0, 1]")
    return result


class JudgeDecision(StrEnum):
    ACCEPT = "ACCEPT"
    RETRY = "RETRY"
    ESCALATE = "ESCALATE"
    REPLAN = "REPLAN"
    BLOCKED = "BLOCKED"
    HUMAN_REVIEW = "HUMAN_REVIEW"


class JudgeAction(StrEnum):
    NONE = "NONE"
    RETRY_SAME_WORKER = "RETRY_SAME_WORKER"
    RETRY_WITH_CONTEXT = "RETRY_WITH_CONTEXT"
    ESCALATE_WORKER = "ESCALATE_WORKER"
    REPLAN = "REPLAN"
    HUMAN_REVIEW = "HUMAN_REVIEW"
    STOP = "STOP"


class JudgeTurnStatus(StrEnum):
    RESPONSE = "RESPONSE"
    EXECUTION_ERROR = "EXECUTION_ERROR"


JUDGE_ALLOWED_ACTIONS = {
    JudgeDecision.ACCEPT: {JudgeAction.NONE},
    JudgeDecision.RETRY: {JudgeAction.RETRY_SAME_WORKER, JudgeAction.RETRY_WITH_CONTEXT},
    JudgeDecision.ESCALATE: {JudgeAction.ESCALATE_WORKER},
    JudgeDecision.REPLAN: {JudgeAction.REPLAN},
    JudgeDecision.BLOCKED: {JudgeAction.STOP},
    JudgeDecision.HUMAN_REVIEW: {JudgeAction.HUMAN_REVIEW},
}

@dataclass(frozen=True, slots=True)
class JudgePolicy:
    model_id: str = "ornith_lead"
    max_model_attempts: int = 2
    max_diff_bytes: int = 131072
    max_prompt_bytes: int = 262144
    max_response_bytes: int = 65536
    timeout_seconds: float = 180.0
    output_tokens: int = 4096
    enforce_no_workspace_mutation: bool = True

    def __post_init__(self) -> None:
        _text(self.model_id, "model_id")
        if self.model_id != "ornith_lead":
            raise ValueError("Phase 9 judge model_id must be ornith_lead")
        for name in ("max_model_attempts", "max_diff_bytes", "max_prompt_bytes", "max_response_bytes", "output_tokens"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be positive integer")
        if isinstance(self.timeout_seconds, bool) or not isinstance(self.timeout_seconds, (int, float)) or not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite positive numeric")
        if self.enforce_no_workspace_mutation is not True:
            raise ValueError("Phase 9 judge must enforce no workspace mutation")

    @classmethod
    def from_policies(cls, policies: Mapping[str, Any]) -> "JudgePolicy":
        raw = policies.get("judge", {})
        if not isinstance(raw, Mapping):
            raise ValueError("policies.judge must be a mapping")
        return cls(
            model_id=str(raw.get("model", "ornith_lead")),
            max_model_attempts=int(raw.get("max_model_attempts", 2)),
            max_diff_bytes=int(raw.get("max_diff_bytes", 131072)),
            max_prompt_bytes=int(raw.get("max_prompt_bytes", 262144)),
            max_response_bytes=int(raw.get("max_response_bytes", 65536)),
            timeout_seconds=float(raw.get("timeout_seconds", 180)),
            output_tokens=int(raw.get("output_tokens", 4096)),
            enforce_no_workspace_mutation=bool(raw.get("enforce_no_workspace_mutation", True)),
        )


@dataclass(frozen=True, slots=True)
class JudgeEvidenceBundle:
    request_id: str
    request_fingerprint: str
    run_id: str
    goal_id: str
    todo_id: str
    objective: str
    verification_report: VerificationReport
    critic_report: CriticReport
    candidate_head: str
    candidate_diff_sha256: str
    candidate_diff_text: str
    dirty_paths: tuple[str, ...]
    work_contract: Mapping[str, Any]
    allowed_evidence_ids: frozenset[str]
    acceptance_obligations: AcceptanceObligationsV1 | None = None
    generated_test_diff_text: str = ''
    explorer_hypotheses: tuple[str, ...] = ()
    semantic_assessment: tuple[AcceptanceEvidenceV1, ...] = ()
    public_witness_observations: tuple = ()

    def __post_init__(self) -> None:
        for name in ("request_id", "request_fingerprint", "run_id", "goal_id", "todo_id", "objective", "candidate_head", "candidate_diff_sha256"):
            _text(getattr(self, name), name)
        if not isinstance(self.verification_report, VerificationReport):
            raise ValueError("verification_report must be VerificationReport")
        if not isinstance(self.critic_report, CriticReport):
            raise ValueError("critic_report must be CriticReport")
        if not isinstance(self.candidate_diff_text, str):
            raise ValueError("candidate_diff_text must be string")
        object.__setattr__(self, "dirty_paths", _unique_text(self.dirty_paths, "dirty_paths"))
        object.__setattr__(self, "work_contract", MappingProxyType(dict(self.work_contract)))
        if not isinstance(self.allowed_evidence_ids, frozenset) or not self.allowed_evidence_ids:
            raise ValueError("allowed_evidence_ids must be non-empty frozenset")
        if any(not isinstance(item, str) or not item for item in self.allowed_evidence_ids):
            raise ValueError("allowed_evidence_ids must contain non-empty strings")

    @property
    def deterministic_outcome(self) -> VerificationOutcome:
        return self.verification_report.outcome

    @property
    def critic_verdict(self) -> CriticVerdict:
        return self.critic_report.analysis.verdict


@dataclass(frozen=True, slots=True)
class JudgeAnalysis:
    deterministic_outcome: VerificationOutcome
    decision: JudgeDecision
    confidence: float
    summary: str
    evidence: tuple[str, ...]
    next_action: JudgeAction
    handoff_guidance: tuple[str, ...]
    can_override_deterministic: bool = False
    acceptance_evidence: tuple[AcceptanceEvidenceV1, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.deterministic_outcome, VerificationOutcome):
            raise ValueError("deterministic_outcome must be VerificationOutcome")
        if not isinstance(self.decision, JudgeDecision) or not isinstance(self.next_action, JudgeAction):
            raise ValueError("judge enum fields are invalid")
        object.__setattr__(self, "confidence", _confidence(self.confidence))
        _text(self.summary, "summary")
        object.__setattr__(self, "evidence", _unique_text(self.evidence, "evidence"))
        object.__setattr__(self, "handoff_guidance", _unique_text(self.handoff_guidance, "handoff_guidance"))
        if self.can_override_deterministic is not False:
            raise ValueError("judge cannot override deterministic verification")
        if self.deterministic_outcome is VerificationOutcome.FAILED and self.decision is JudgeDecision.ACCEPT:
            raise ValueError("judge cannot ACCEPT a deterministically failed candidate")
        if self.next_action not in JUDGE_ALLOWED_ACTIONS[self.decision]:
            raise ValueError("judge decision/next_action combination is invalid")
        if self.deterministic_outcome is VerificationOutcome.FAILED and self.next_action is JudgeAction.NONE:
            raise ValueError("failed verification requires a non-NONE next action")
        if type(self.acceptance_evidence) is not tuple or any(not isinstance(e, AcceptanceEvidenceV1) for e in self.acceptance_evidence):
            raise ValueError('invalid acceptance evidence')
        if self.decision is JudgeDecision.ACCEPT and any(e.status != 'SATISFIED' for e in self.acceptance_evidence):
            raise ValueError('cannot ACCEPT unresolved/violated semantic obligation')

    def to_dict(self) -> dict[str, Any]:
        return {
            "deterministic_outcome": self.deterministic_outcome.value,
            "decision": self.decision.value,
            "confidence": self.confidence,
            "summary": self.summary,
            "evidence": list(self.evidence),
            "next_action": self.next_action.value,
            "handoff_guidance": list(self.handoff_guidance),
            "can_override_deterministic": False,
            **({'acceptance_evidence':[e.to_dict() for e in self.acceptance_evidence]} if self.acceptance_evidence else {}),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "JudgeAnalysis":
        return cls(
            VerificationOutcome(str(payload["deterministic_outcome"])),
            JudgeDecision(str(payload["decision"])),
            payload["confidence"],
            str(payload["summary"]),
            tuple(payload["evidence"]),
            JudgeAction(str(payload["next_action"])),
            tuple(payload["handoff_guidance"]),
            payload["can_override_deterministic"],
            tuple(AcceptanceEvidenceV1(**{**e,'evidence_refs':tuple(e['evidence_refs']),
                'expectation_basis':tuple(e['expectation_basis'])}) for e in payload.get('acceptance_evidence', ())),
        )


@dataclass(frozen=True, slots=True)
class JudgeTurnRecord:
    request_id: str
    attempt_index: int
    status: JudgeTurnStatus
    model_id: str | None
    model_tag: str | None
    content: str | None
    error: str | None

    def __post_init__(self) -> None:
        _text(self.request_id, "request_id")
        if not isinstance(self.attempt_index, int) or isinstance(self.attempt_index, bool) or self.attempt_index < 0:
            raise ValueError("attempt_index must be non-negative int")
        if not isinstance(self.status, JudgeTurnStatus):
            raise ValueError("status must be JudgeTurnStatus")
        if self.status is JudgeTurnStatus.RESPONSE:
            _text(self.model_id, "model_id")
            _text(self.model_tag, "model_tag")
            if not isinstance(self.content, str):
                raise ValueError("response turn requires content")
            if self.error is not None:
                raise ValueError("response turn cannot have error")
        else:
            _text(self.error, "error")
            if self.content is not None:
                raise ValueError("execution error turn cannot have content")

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "attempt_index": self.attempt_index,
            "status": self.status.value,
            "model_id": self.model_id,
            "model_tag": self.model_tag,
            "content": self.content,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "JudgeTurnRecord":
        return cls(
            str(payload["request_id"]), int(payload["attempt_index"]), JudgeTurnStatus(str(payload["status"])),
            payload.get("model_id"), payload.get("model_tag"), payload.get("content"), payload.get("error"),
        )


@dataclass(frozen=True, slots=True)
class JudgeReport:
    report_id: str
    request_fingerprint: str
    run_id: str
    todo_id: str
    verification_report_id: str
    critic_report_id: str
    candidate_diff_sha256: str
    analysis: JudgeAnalysis
    model_id: str
    model_tag: str
    model_attempts: int
    raw_turn_artifact_id: str
    created_at: str

    def __post_init__(self) -> None:
        for name in ("report_id", "request_fingerprint", "run_id", "todo_id", "verification_report_id", "critic_report_id", "candidate_diff_sha256", "model_id", "model_tag", "raw_turn_artifact_id", "created_at"):
            _text(getattr(self, name), name)
        if not isinstance(self.analysis, JudgeAnalysis):
            raise ValueError("analysis must be JudgeAnalysis")
        if not isinstance(self.model_attempts, int) or isinstance(self.model_attempts, bool) or self.model_attempts <= 0:
            raise ValueError("model_attempts must be positive int")

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "request_fingerprint": self.request_fingerprint,
            "run_id": self.run_id,
            "todo_id": self.todo_id,
            "verification_report_id": self.verification_report_id,
            "critic_report_id": self.critic_report_id,
            "candidate_diff_sha256": self.candidate_diff_sha256,
            "analysis": self.analysis.to_dict(),
            "model_id": self.model_id,
            "model_tag": self.model_tag,
            "model_attempts": self.model_attempts,
            "raw_turn_artifact_id": self.raw_turn_artifact_id,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "JudgeReport":
        return cls(
            str(payload["report_id"]), str(payload["request_fingerprint"]), str(payload["run_id"]), str(payload["todo_id"]),
            str(payload["verification_report_id"]), str(payload["critic_report_id"]), str(payload["candidate_diff_sha256"]),
            JudgeAnalysis.from_dict(payload["analysis"]), str(payload["model_id"]), str(payload["model_tag"]),
            int(payload["model_attempts"]), str(payload["raw_turn_artifact_id"]), str(payload["created_at"]),
        )


@dataclass(frozen=True, slots=True)
class JudgeCoordinatorResult:
    report: JudgeReport
    reused_existing_report: bool
    model_calls: int
    reused_turns: int


def request_fingerprint(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
