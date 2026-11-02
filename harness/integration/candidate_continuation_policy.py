"""RC-5 deterministic, same-process candidate continuation policy."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from collections.abc import Mapping
from typing import Any

from ..context_manager import Artifact, ArtifactKind, ArtifactOrigin, ArtifactStore
from ..context_manager.integration import artifact_uri
from ..control_plane import Evidence, EvidenceStatus, RunStore
from .failure_policy import FailureKindV1, RecoveryWorkspaceDispositionV1
from .progress_evidence import ContinuationIntentV1, ProgressClassV1


class CandidateDispositionV1(StrEnum):
    REUSE_CANDIDATE = "REUSE_CANDIDATE"
    FRESH_CANDIDATE = "FRESH_CANDIDATE"
    BLOCK = "BLOCK"


@dataclass(frozen=True, slots=True)
class CandidateContinuationContextV1:
    schema_version: int
    run_id: str
    todo_id: str
    lease_id: str
    candidate_id: str
    failure_kind: str
    base_disposition: RecoveryWorkspaceDispositionV1
    progress_evidence_fingerprint: str | None
    progress_class: ProgressClassV1 | None
    continuation_intent: ContinuationIntentV1 | None
    dirty: bool
    current_diff_sha256: str | None
    checkpoint_valid: bool
    candidate_identity_valid: bool
    contract_identity_valid: bool
    scope_safe: bool
    progress_binding_valid: bool
    review_recovery_cause: str | None = None
    accepted: bool = False
    rc3_hard_ceiling: bool = False

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("candidate continuation schema_version must be 1")
        for value in (self.run_id, self.todo_id, self.lease_id, self.candidate_id):
            if not isinstance(value, str) or not value:
                raise ValueError("candidate continuation identity must be non-empty")
        if not isinstance(self.base_disposition, RecoveryWorkspaceDispositionV1):
            raise ValueError("invalid base workspace disposition")
        for value in (
            self.dirty, self.checkpoint_valid, self.candidate_identity_valid,
            self.contract_identity_valid, self.scope_safe, self.progress_binding_valid,
            self.accepted, self.rc3_hard_ceiling,
        ):
            if not isinstance(value, bool):
                raise ValueError("candidate continuation safety fields must be bool")

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version, "run_id": self.run_id,
            "todo_id": self.todo_id, "lease_id": self.lease_id,
            "candidate_id": self.candidate_id, "failure_kind": self.failure_kind,
            "base_disposition": self.base_disposition.value,
            "progress_evidence_fingerprint": self.progress_evidence_fingerprint,
            "progress_class": None if self.progress_class is None else self.progress_class.value,
            "continuation_intent": None if self.continuation_intent is None else self.continuation_intent.value,
            "dirty": self.dirty, "current_diff_sha256": self.current_diff_sha256,
            "checkpoint_valid": self.checkpoint_valid,
            "candidate_identity_valid": self.candidate_identity_valid,
            "contract_identity_valid": self.contract_identity_valid,
            "scope_safe": self.scope_safe,
            "progress_binding_valid": self.progress_binding_valid,
            "review_recovery_cause": self.review_recovery_cause,
            "accepted": self.accepted, "rc3_hard_ceiling": self.rc3_hard_ceiling,
        }


@dataclass(frozen=True, slots=True)
class CandidateContinuationDecisionV1:
    schema_version: int
    run_id: str
    todo_id: str
    lease_id: str
    candidate_id: str
    context_fingerprint: str
    failure_kind: str
    progress_evidence_fingerprint: str | None
    continuation_intent: ContinuationIntentV1 | None
    base_disposition: RecoveryWorkspaceDispositionV1
    disposition: CandidateDispositionV1
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.schema_version != 1 or not all((self.run_id, self.todo_id, self.lease_id, self.candidate_id, self.context_fingerprint)):
            raise ValueError("invalid candidate continuation decision identity")
        if not isinstance(self.disposition, CandidateDispositionV1):
            raise ValueError("invalid candidate continuation disposition")
        if tuple(sorted(set(self.reason_codes))) != self.reason_codes or not self.reason_codes:
            raise ValueError("candidate continuation reason codes must be sorted and non-empty")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version, "run_id": self.run_id,
            "todo_id": self.todo_id, "lease_id": self.lease_id,
            "candidate_id": self.candidate_id, "context_fingerprint": self.context_fingerprint,
            "failure_kind": self.failure_kind,
            "progress_evidence_fingerprint": self.progress_evidence_fingerprint,
            "continuation_intent": None if self.continuation_intent is None else self.continuation_intent.value,
            "base_disposition": self.base_disposition.value,
            "disposition": self.disposition.value,
            "reason_codes": list(self.reason_codes),
        }

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class CandidateContinuationPolicy:
    """Policy-only RC-5 gate; it neither grants capacity nor mutates a candidate."""

    def evaluate(self, context: CandidateContinuationContextV1) -> CandidateContinuationDecisionV1:
        def decision(disposition: CandidateDispositionV1, *reasons: str) -> CandidateContinuationDecisionV1:
            return CandidateContinuationDecisionV1(
                1, context.run_id, context.todo_id, context.lease_id, context.candidate_id,
                context.fingerprint, context.failure_kind, context.progress_evidence_fingerprint,
                context.continuation_intent, context.base_disposition, disposition,
                tuple(sorted(set(reasons))),
            )

        known_failures = {item.value for item in FailureKindV1}
        if context.failure_kind not in known_failures or context.failure_kind == FailureKindV1.UNKNOWN.value:
            return decision(CandidateDispositionV1.BLOCK, "unknown_failure_kind")
        if not context.progress_binding_valid or context.progress_evidence_fingerprint is None:
            return decision(CandidateDispositionV1.BLOCK, "progress_evidence_binding_invalid")
        if not all((context.checkpoint_valid, context.candidate_identity_valid, context.contract_identity_valid, context.scope_safe)):
            return decision(CandidateDispositionV1.BLOCK, "candidate_integrity_or_scope_invalid")
        if context.base_disposition is RecoveryWorkspaceDispositionV1.BLOCK:
            return decision(CandidateDispositionV1.BLOCK, "base_failure_policy_block")
        if context.review_recovery_cause == "REVIEW_REPLAN":
            return decision(CandidateDispositionV1.FRESH_CANDIDATE, "semantic_replan_default_fresh_candidate")
        if context.accepted or context.continuation_intent is ContinuationIntentV1.COMPLETE:
            return decision(CandidateDispositionV1.BLOCK, "accepted_or_completed_todo_requires_no_reuse")
        if context.continuation_intent is ContinuationIntentV1.BLOCK:
            return decision(CandidateDispositionV1.BLOCK, "rc4_block")
        if context.continuation_intent is ContinuationIntentV1.VERIFY_NOW:
            if context.dirty and context.current_diff_sha256:
                return decision(CandidateDispositionV1.REUSE_CANDIDATE, "verify_exact_safe_candidate")
            return decision(CandidateDispositionV1.FRESH_CANDIDATE, "verify_without_reusable_dirty_diff")
        if context.continuation_intent is ContinuationIntentV1.REPLAN_OR_SPLIT:
            return decision(CandidateDispositionV1.FRESH_CANDIDATE, "semantic_replan_default_fresh_candidate")
        if context.continuation_intent is ContinuationIntentV1.CONTINUE_SAME:
            if context.progress_class is ProgressClassV1.STRONG_PROGRESS and context.dirty and context.current_diff_sha256:
                return decision(CandidateDispositionV1.REUSE_CANDIDATE, "strong_progress_same_candidate")
            return decision(CandidateDispositionV1.FRESH_CANDIDATE, "continuation_without_strong_recoverable_progress")
        if context.continuation_intent is ContinuationIntentV1.CONTINUE_WITH_CONTEXT:
            if context.progress_class is ProgressClassV1.STRONG_PROGRESS and context.dirty and context.current_diff_sha256:
                return decision(CandidateDispositionV1.REUSE_CANDIDATE, "strong_progress_context_continuation")
            return decision(CandidateDispositionV1.FRESH_CANDIDATE, "weak_or_nonmutating_context_progress")
        if context.continuation_intent is ContinuationIntentV1.STOP_FOR_RECOVERY:
            quota_failures = {
                FailureKindV1.WORKER_QUOTA.value,
                FailureKindV1.TOOL_CALL_QUOTA.value,
            }
            if (
                context.failure_kind in quota_failures
                and context.progress_class is ProgressClassV1.STRONG_PROGRESS
                and context.dirty
                and context.current_diff_sha256
            ):
                return decision(
                    CandidateDispositionV1.REUSE_CANDIDATE,
                    "quota_recovery_preserves_strong_progress_candidate",
                )
            return decision(
                CandidateDispositionV1.FRESH_CANDIDATE,
                "recovery_stop_or_no_progress",
            )
        return decision(CandidateDispositionV1.FRESH_CANDIDATE, "recovery_stop_or_no_progress")


def persist_candidate_continuation_decision(
    artifact_store: ArtifactStore, run_store: RunStore,
    context: CandidateContinuationContextV1, decision: CandidateContinuationDecisionV1,
) -> str:
    if (
        decision.run_id != context.run_id or decision.todo_id != context.todo_id
        or decision.lease_id != context.lease_id or decision.candidate_id != context.candidate_id
        or decision.context_fingerprint != context.fingerprint
        or decision.progress_evidence_fingerprint != context.progress_evidence_fingerprint
    ):
        raise ValueError("candidate continuation decision does not bind to context")
    artifact_id = f"candidate-continuation:{decision.fingerprint[:32]}"
    content = json.dumps({"context": context.to_dict(), "decision": decision.to_dict()}, sort_keys=True, separators=(",", ":"))
    artifact = Artifact.from_text(
        artifact_id=artifact_id, kind=ArtifactKind.CONTROL_PLANE_EVIDENCE,
        origin=ArtifactOrigin.CONTROL_PLANE, content=content, media_type="application/json",
        source="CandidateContinuationDecisionV1", created_at="1970-01-01T00:00:00+00:00",
        scope_tags=(context.run_id, context.todo_id),
        metadata={"production_role": "CANDIDATE_CONTINUATION_DECISION", "fingerprint": decision.fingerprint},
    )
    if artifact_store.has_artifact(artifact_id):
        if artifact_store.get_artifact(artifact_id).content != content:
            raise RuntimeError("candidate continuation decision identity conflict")
    else:
        artifact_store.create_artifact(artifact)
    run_store.record_evidence(context.run_id, Evidence(
        evidence_id=f"{context.run_id}:candidate-continuation:{decision.fingerprint[:16]}",
        todo_id=context.todo_id, evidence_type="CANDIDATE_CONTINUATION_DECISION",
        source="CandidateContinuationPolicy", status=EvidenceStatus.PASSED,
        payload={"schema_version": 1, "candidate_id": context.candidate_id,
                 "failure_kind": context.failure_kind,
                 "progress_evidence_fingerprint": context.progress_evidence_fingerprint,
                 "continuation_intent": None if context.continuation_intent is None else context.continuation_intent.value,
                 "base_disposition": context.base_disposition.value,
                 "disposition": decision.disposition.value,
                 "fingerprint": decision.fingerprint}, reference=artifact_uri(artifact_id),
    ))
    return artifact_id

def context_from_rc4_execution(
    *, artifact_store: ArtifactStore, execution: Any, run_id: str, todo_id: str,
    lease_id: str, candidate_id: str, failure_kind: str,
    base_disposition: RecoveryWorkspaceDispositionV1, candidate_valid: bool,
    review_recovery_cause: str | None = None,
) -> CandidateContinuationContextV1 | None:
    """Resolve only the RC-4 evidence artifact explicitly carried by execution."""
    raw = getattr(execution, "raw", {})
    if not isinstance(raw, Mapping):
        return None
    artifact_ids = raw.get("progress_evidence_artifacts")
    if not isinstance(artifact_ids, list) or len(artifact_ids) != 1 or not isinstance(artifact_ids[0], str):
        return None
    try:
        payload = json.loads(artifact_store.get_artifact(artifact_ids[0]).content)
        evidence = payload["progress_evidence"]
        continuation = payload["continuation_decision"]
        if not isinstance(evidence, Mapping) or not isinstance(continuation, Mapping):
            return None
        fingerprint = hashlib.sha256(json.dumps(dict(evidence), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()
        if (
            evidence.get("schema_version") != 1 or evidence.get("run_id") != run_id
            or evidence.get("todo_id") != todo_id or continuation.get("schema_version") != 1
            or continuation.get("run_id") != run_id or continuation.get("todo_id") != todo_id
            or continuation.get("evidence_fingerprint") != fingerprint
        ):
            return None
        progress = ProgressClassV1(str(evidence["progress_class"]))
        intent = ContinuationIntentV1(str(continuation["intent"]))
        if continuation.get("technical_failure_kind") not in {None, failure_kind}:
            return None
        return CandidateContinuationContextV1(
            1, run_id, todo_id, lease_id, candidate_id, failure_kind, base_disposition,
            fingerprint, progress, intent, True, str(evidence.get("current_diff_sha256") or "") or None,
            candidate_valid, candidate_valid, candidate_valid, candidate_valid, True,
            review_recovery_cause=review_recovery_cause, accepted=False,
            rc3_hard_ceiling=raw.get("grant_decision") == "REACHED_HARD_CEILING",
        )
    except Exception:
        return None
