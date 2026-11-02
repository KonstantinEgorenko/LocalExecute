"""Typed review recovery cause model and deterministic resolver."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping


class ReviewRecoveryCauseKindV1(StrEnum):
    REVIEW_REPLAN = "REVIEW_REPLAN"
    REVIEW_RETRY = "REVIEW_RETRY"
    REVIEW_ESCALATE = "REVIEW_ESCALATE"
    REVIEW_HUMAN_REVIEW = "REVIEW_HUMAN_REVIEW"
    REVIEW_BLOCKED = "REVIEW_BLOCKED"


@dataclass(frozen=True, slots=True)
class ReviewRecoveryCauseV1:
    schema_version: int
    kind: ReviewRecoveryCauseKindV1
    run_id: str
    todo_id: str
    judge_report_id: str
    judge_decision: str
    next_action: str
    reason: str
    source_event_sequence: int | None = None
    verification_report_id: str | None = None
    candidate_diff_sha256: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("ReviewRecoveryCauseV1 schema_version must be 1")
        if not isinstance(self.kind, ReviewRecoveryCauseKindV1):
            raise ValueError("kind must be ReviewRecoveryCauseKindV1")
        for name in ("run_id", "todo_id", "judge_report_id", "judge_decision", "next_action", "reason"):
            val = getattr(self, name)
            if not isinstance(val, str) or not val.strip():
                raise ValueError(f"{name} must be non-empty text")
        if self.source_event_sequence is not None:
            if not isinstance(self.source_event_sequence, int) or isinstance(self.source_event_sequence, bool) or self.source_event_sequence < 0:
                raise ValueError("source_event_sequence must be non-negative int")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "kind": self.kind.value,
            "run_id": self.run_id,
            "todo_id": self.todo_id,
            "judge_report_id": self.judge_report_id,
            "judge_decision": self.judge_decision,
            "next_action": self.next_action,
            "reason": self.reason,
            "source_event_sequence": self.source_event_sequence,
            "verification_report_id": self.verification_report_id,
            "candidate_diff_sha256": self.candidate_diff_sha256,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ReviewRecoveryCauseV1:
        if not isinstance(data, Mapping):
            raise ValueError("data must be mapping")
        return cls(
            schema_version=int(data.get("schema_version", 1)),
            kind=ReviewRecoveryCauseKindV1(str(data["kind"])),
            run_id=str(data["run_id"]),
            todo_id=str(data["todo_id"]),
            judge_report_id=str(data["judge_report_id"]),
            judge_decision=str(data["judge_decision"]),
            next_action=str(data["next_action"]),
            reason=str(data.get("reason", "")),
            source_event_sequence=data.get("source_event_sequence"),
            verification_report_id=data.get("verification_report_id"),
            candidate_diff_sha256=data.get("candidate_diff_sha256"),
        )


def resolve_review_recovery_cause(
    *,
    run_store: Any,
    run_id: str,
    todo_id: str,
    verification: Any | None = None,
) -> ReviewRecoveryCauseV1 | None:
    """Deterministically resolve the exact durable JudgeReport for this recovery cycle.

    Ambiguity Rule:
    - If 0 valid JudgeReport records match the verification anchor, return None (fail closed).
    - If >1 ambiguous records match, return None (fail closed).
    """
    if run_store is None or not hasattr(run_store, "list_events"):
        return None
    try:
        events = run_store.list_events(run_id)
    except Exception:
        return None

    judge_events = []
    for ev in events:
        etype = getattr(ev, "event_type", None)
        if str(etype) not in {"EVIDENCE_RECORDED", "ControlPlaneEventType.EVIDENCE_RECORDED"}:
            continue
        payload = getattr(ev, "payload", {})
        if not isinstance(payload, Mapping):
            continue
        if payload.get("evidence_type") != "JUDGE_REPORT":
            continue
        if payload.get("todo_id") != todo_id:
            continue
        judge_events.append(ev)

    if not judge_events:
        return None

    verif_report_id = getattr(verification, "report_id", None) if verification is not None else None

    candidates = []
    if verif_report_id:
        for ev in judge_events:
            pl = ev.payload.get("payload", {})
            if isinstance(pl, Mapping) and pl.get("verification_report_id") == verif_report_id:
                candidates.append(ev)
    else:
        candidates = judge_events

    if len(candidates) != 1:
        return None

    matched_ev = candidates[0]
    pl = matched_ev.payload.get("payload", {})
    if not isinstance(pl, Mapping):
        return None

    decision = str(pl.get("decision", "")).strip().upper()
    next_action = str(pl.get("next_action", "")).strip().upper()
    report_id = str(pl.get("report_id", getattr(matched_ev, "entity_id", "") or ""))
    diff_sha = pl.get("candidate_diff_sha256")
    v_id = pl.get("verification_report_id")

    decision_map = {
        "REPLAN": ReviewRecoveryCauseKindV1.REVIEW_REPLAN,
        "RETRY": ReviewRecoveryCauseKindV1.REVIEW_RETRY,
        "ESCALATE": ReviewRecoveryCauseKindV1.REVIEW_ESCALATE,
        "HUMAN_REVIEW": ReviewRecoveryCauseKindV1.REVIEW_HUMAN_REVIEW,
        "BLOCKED": ReviewRecoveryCauseKindV1.REVIEW_BLOCKED,
    }
    cause_kind = decision_map.get(decision)
    if cause_kind is None:
        return None

    return ReviewRecoveryCauseV1(
        schema_version=1,
        kind=cause_kind,
        run_id=run_id,
        todo_id=todo_id,
        judge_report_id=report_id,
        judge_decision=decision,
        next_action=next_action,
        reason=f"Judge review requested: {decision}",
        source_event_sequence=getattr(matched_ev, "sequence", None),
        verification_report_id=v_id if isinstance(v_id, str) else None,
        candidate_diff_sha256=diff_sha if isinstance(diff_sha, str) else None,
    )


class ReviewFailureKindV1(StrEnum):
    JUDGE_PROTOCOL_EXHAUSTED = "JUDGE_PROTOCOL_EXHAUSTED"
    JUDGE_TRANSPORT_EXHAUSTED = "JUDGE_TRANSPORT_EXHAUSTED"
    JUDGE_PROTOCOL_INVALID = "JUDGE_PROTOCOL_INVALID"
    JUDGE_STATE_ERROR = "JUDGE_STATE_ERROR"
    JUDGE_EXHAUSTED_UNCLASSIFIED = "JUDGE_EXHAUSTED_UNCLASSIFIED"

@dataclass(frozen=True, slots=True)
class ReviewFailureReceiptV1:
    kind: ReviewFailureKindV1
    run_id: str
    todo_id: str
    verification_report_id: str
    exception_type: str
    message: str
    request_id: str | None = None
    attempts: int | None = None
    stage: str = "JUDGE"
    schema_version: int = 1

    def __post_init__(self):
        if type(self.schema_version) is not int or self.schema_version != 1 or self.stage != "JUDGE":
            raise ValueError("invalid review failure receipt schema/stage")
        if not isinstance(self.kind,ReviewFailureKindV1):raise ValueError("invalid review failure kind")
        for name in ("run_id","todo_id","verification_report_id","exception_type","message"):
            if type(getattr(self,name)) is not str or not getattr(self,name):raise ValueError("missing failure receipt anchor")
        if len(self.message.encode("utf-8"))>2048:raise ValueError("failure message exceeds bound")
        if self.attempts is not None and (type(self.attempts) is not int or self.attempts<0):raise ValueError("invalid attempts")
    def to_dict(self):
        from dataclasses import asdict
        row=asdict(self);row["kind"]=self.kind.value;return row
    @classmethod
    def from_dict(cls,row):
        return cls(**{**row,"kind":ReviewFailureKindV1(row["kind"])})

def _review_failure_id(run_id,todo_id,verification_report_id):
    import hashlib,json
    return "review-failure:"+hashlib.sha256(json.dumps([run_id,todo_id,verification_report_id]).encode()).hexdigest()

def publish_review_failure(*,artifact_store,run_id,todo_id,verification_report_id,exc):
    from ..judge.errors import JudgeExhaustedError,JudgeProtocolError,JudgeStateError
    from ..context_manager.models import Artifact,ArtifactKind,ArtifactOrigin
    from datetime import datetime,timezone
    import json
    if isinstance(exc,JudgeExhaustedError):
        value=getattr(exc,"failure_kind","UNKNOWN")
        kind=ReviewFailureKindV1(value) if value in ("JUDGE_PROTOCOL_EXHAUSTED","JUDGE_TRANSPORT_EXHAUSTED") else ReviewFailureKindV1.JUDGE_EXHAUSTED_UNCLASSIFIED
    elif isinstance(exc,JudgeProtocolError):kind=ReviewFailureKindV1.JUDGE_PROTOCOL_INVALID
    elif isinstance(exc,JudgeStateError):kind=ReviewFailureKindV1.JUDGE_STATE_ERROR
    else:raise ValueError("unknown review exception; do not infer cause from prose")
    row=ReviewFailureReceiptV1(kind,run_id,todo_id,verification_report_id,type(exc).__name__,
        (str(exc) or type(exc).__name__).encode("utf-8")[:2048].decode("utf-8","ignore"),
        getattr(exc,"request_id",None),getattr(exc,"attempts",None))
    key=_review_failure_id(run_id,todo_id,verification_report_id)
    content=json.dumps(row.to_dict(),sort_keys=True)
    if artifact_store.has_artifact(key):
        if artifact_store.get_artifact(key).content!=content:raise ValueError("review failure receipt conflicts")
    else:
        artifact_store.create_artifact(Artifact.from_text(artifact_id=key,kind=ArtifactKind.OTHER,
            origin=ArtifactOrigin.CONTROL_PLANE,content=content,media_type="application/json",
            source="ReviewFailureReceipt",created_at=datetime.now(timezone.utc).isoformat(),
            metadata={"run_id":run_id,"todo_id":todo_id,"verification_report_id":verification_report_id}))
    return row

def resolve_review_failure(*,artifact_store,run_id,todo_id,verification_report_id):
    import json
    if artifact_store is None or type(verification_report_id) is not str:return None
    key=_review_failure_id(run_id,todo_id,verification_report_id)
    if not artifact_store.has_artifact(key):return None
    artifact=artifact_store.get_artifact(key)
    if artifact.source!="ReviewFailureReceipt":raise ValueError("review failure receipt source mismatch")
    row=ReviewFailureReceiptV1.from_dict(json.loads(artifact.content))
    if (row.run_id,row.todo_id,row.verification_report_id)!=(run_id,todo_id,verification_report_id):raise ValueError("review failure receipt anchor mismatch")
    return row


def record_review_failure_block(*,run_store,receipt):
    """Idempotent terminal observation, anchored to this verification cycle."""
    from ..control_plane.models import Evidence,EvidenceStatus
    key=_review_failure_id(receipt.run_id,receipt.todo_id,receipt.verification_report_id)+":blocked"
    evidence=Evidence(evidence_id=key,todo_id=receipt.todo_id,evidence_type="REVIEW_PROTOCOL_BLOCKED",
        source="ReviewFailureReceipt",status=EvidenceStatus.FAILED,
        payload={"failure_kind":receipt.kind.value,"disposition":"BLOCK","review_failure":receipt.to_dict()})
    for event in run_store.list_events(receipt.run_id):
        if event.entity_id==key:
            if event.payload!=evidence.to_dict():raise ValueError("review block evidence conflicts")
            return
    run_store.record_evidence(receipt.run_id,evidence)
