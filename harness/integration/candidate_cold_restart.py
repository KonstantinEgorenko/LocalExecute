"""RC-6 fail-closed authorization for a retained candidate after cold restart."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from collections.abc import Mapping

from ..context_manager import Artifact, ArtifactKind, ArtifactOrigin
from ..context_manager.integration import artifact_uri
from ..control_plane import Evidence, EvidenceStatus
from .candidate_continuation_policy import CandidateDispositionV1


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def contract_fingerprint(contract) -> str:
    return hashlib.sha256(_canonical(contract.to_dict()).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class CandidateProgressCheckpointV2:
    schema_version: int
    run_id: str
    todo_id: str
    lease_id: str
    branch_name: str
    base_commit: str
    source_head_commit: str
    diff_sha256: str
    changed_paths: tuple[str, ...]
    untracked_paths: tuple[str, ...]
    failure_kind: str
    candidate_fingerprint: str
    contract_fingerprint: str
    progress_artifact_id: str
    progress_evidence_fingerprint: str
    progress_class: str
    continuation_intent: str
    decision_artifact_id: str
    decision_fingerprint: str
    disposition: str
    slice_index: int
    worker_calls_used_total: int
    tool_calls_used_total: int
    quota_usage: Mapping[str, object]

    def __post_init__(self) -> None:
        if self.schema_version != 2:
            raise ValueError("checkpoint schema_version must be 2")
        required = (self.run_id, self.todo_id, self.lease_id, self.branch_name,
                    self.base_commit, self.source_head_commit, self.diff_sha256,
                    self.failure_kind, self.candidate_fingerprint, self.contract_fingerprint, self.progress_artifact_id,
                    self.progress_evidence_fingerprint, self.progress_class,
                    self.continuation_intent, self.decision_artifact_id,
                    self.decision_fingerprint, self.disposition)
        if any(not isinstance(value, str) or not value for value in required):
            raise ValueError("checkpoint identity must be non-empty")
        if self.disposition != CandidateDispositionV1.REUSE_CANDIDATE.value:
            raise ValueError("cold restart checkpoint requires RC-5 REUSE_CANDIDATE")
        if type(self.slice_index) is not int or self.slice_index < 1:
            raise ValueError("checkpoint slice_index must be positive")
        for value in (self.worker_calls_used_total, self.tool_calls_used_total):
            if type(value) is not int or value < 0:
                raise ValueError("checkpoint accounting must be non-negative exact integers")
        for paths in (self.changed_paths, self.untracked_paths):
            if tuple(sorted(set(paths))) != paths or any(not isinstance(p, str) or not p or p.startswith("/") or ".." in p.split("/") for p in paths):
                raise ValueError("checkpoint paths must be normalized")
        if not isinstance(self.quota_usage, Mapping):
            raise ValueError("checkpoint quota_usage must be an object")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version, "run_id": self.run_id, "todo_id": self.todo_id,
            "lease_id": self.lease_id, "branch_name": self.branch_name, "base_commit": self.base_commit,
            "source_head_commit": self.source_head_commit, "diff_sha256": self.diff_sha256,
            "changed_paths": list(self.changed_paths), "untracked_paths": list(self.untracked_paths),
            "failure_kind": self.failure_kind, "candidate_fingerprint": self.candidate_fingerprint,
            "contract_fingerprint": self.contract_fingerprint,
            "progress_artifact_id": self.progress_artifact_id,
            "progress_evidence_fingerprint": self.progress_evidence_fingerprint,
            "progress_class": self.progress_class, "continuation_intent": self.continuation_intent,
            "decision_artifact_id": self.decision_artifact_id, "decision_fingerprint": self.decision_fingerprint,
            "disposition": self.disposition, "slice_index": self.slice_index, "worker_calls_used_total": self.worker_calls_used_total, "tool_calls_used_total": self.tool_calls_used_total,
            "quota_usage": dict(self.quota_usage),
        }

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(_canonical(self.to_dict()).encode("utf-8")).hexdigest()

    @classmethod
    def from_dict(cls, payload: object) -> "CandidateProgressCheckpointV2":
        if not isinstance(payload, Mapping):
            raise ValueError("checkpoint payload must be an object")
        expected = set(cls.__dataclass_fields__)
        if set(payload) != expected:
            raise ValueError("checkpoint fields mismatch")
        paths = []
        for name in ("changed_paths", "untracked_paths"):
            value = payload[name]
            if not isinstance(value, list) or any(not isinstance(p, str) for p in value):
                raise ValueError("checkpoint paths must be string lists")
            paths.append(tuple(value))
        if not isinstance(payload["quota_usage"], Mapping):
            raise ValueError("checkpoint quota_usage must be object")
        values = dict(payload)
        values["changed_paths"], values["untracked_paths"] = paths
        return cls(**values)


@dataclass(frozen=True, slots=True)
class RestartAuthorizationV1:
    authorized: bool
    reason: str
    checkpoint_fingerprint: str | None = None


def _durable_tool_calls(run_store, run_id: str, todo_id: str) -> int:
    return sum(1 for event in run_store.list_events(run_id) if isinstance(event.to_dict().get("payload"), Mapping) and event.to_dict()["payload"].get("todo_id") == todo_id and event.to_dict()["payload"].get("evidence_type") == "TOOL_RESULT")

def persist_cold_restart_checkpoint(*, artifact_store, run_store, checkpoint, context, decision, progress_artifact_id: str, decision_artifact_id: str, contract) -> CandidateProgressCheckpointV2:
    """Persist only a fully bound, RC-5-authorized cold restart record."""
    if decision.disposition is not CandidateDispositionV1.REUSE_CANDIDATE:
        raise ValueError("cold restart is not authorized by this RC-5 decision")
    evidence_fp = context.progress_evidence_fingerprint
    if not isinstance(evidence_fp, str) or not evidence_fp or context.progress_class is None or context.continuation_intent is None:
        raise ValueError("cold restart requires exact RC-4 evidence")
    progress_raw = json.loads(artifact_store.get_artifact(progress_artifact_id).content)
    evidence = progress_raw.get("progress_evidence") if isinstance(progress_raw, Mapping) else None
    if not isinstance(evidence, Mapping) or hashlib.sha256(_canonical(dict(evidence)).encode("utf-8")).hexdigest() != evidence_fp:
        raise ValueError("progress artifact does not bind evidence fingerprint")
    run = run_store.get_run(checkpoint.run_id)
    value = CandidateProgressCheckpointV2(
        2, checkpoint.run_id, checkpoint.todo_id, checkpoint.lease_id, checkpoint.branch_name,
        checkpoint.base_commit, checkpoint.source_head_commit, checkpoint.diff_sha256,
        checkpoint.changed_paths, checkpoint.untracked_paths, checkpoint.failure_kind,
        checkpoint.fingerprint,
        contract_fingerprint(contract), progress_artifact_id, evidence_fp, context.progress_class.value,
        context.continuation_intent.value, decision_artifact_id, decision.fingerprint,
        decision.disposition.value, int(evidence["slice_index"]), run.quota_usage.worker_calls, _durable_tool_calls(run_store, checkpoint.run_id, checkpoint.todo_id), run.quota_usage.to_dict(),
    )
    artifact_id = f"candidate-cold-restart:{value.fingerprint[:32]}"
    content = _canonical(value.to_dict())
    artifact = Artifact.from_text(
        artifact_id=artifact_id, kind=ArtifactKind.CONTROL_PLANE_EVIDENCE,
        origin=ArtifactOrigin.CONTROL_PLANE, content=content, media_type="application/json",
        source="CandidateProgressCheckpointV2", created_at="1970-01-01T00:00:00+00:00",
        scope_tags=(value.run_id, value.todo_id),
        metadata={"production_role": "CANDIDATE_COLD_RESTART_CHECKPOINT", "fingerprint": value.fingerprint},
    )
    if artifact_store.has_artifact(artifact_id):
        if artifact_store.get_artifact(artifact_id).content != content:
            raise RuntimeError("cold restart checkpoint identity conflict")
    else:
        artifact_store.create_artifact(artifact)
    run_store.record_evidence(value.run_id, Evidence(
        evidence_id=f"{value.run_id}:candidate-cold-restart:{value.fingerprint[:16]}", todo_id=value.todo_id,
        evidence_type="CANDIDATE_COLD_RESTART_CHECKPOINT", source="CandidateColdRestartV2",
        status=EvidenceStatus.PASSED, payload=value.to_dict(), reference=artifact_uri(artifact_id),
    ))
    return value


def cold_restart_authorization(*, artifact_store, run_store, manager, run, lease, contract) -> RestartAuthorizationV1:
    """Validate one exact V2 checkpoint; never infer authorization from global latest evidence."""
    try:
        if artifact_store is None:
            return RestartAuthorizationV1(False, "artifact store unavailable")
        checkpoint = None
        envelope = None
        for event in reversed(run_store.list_events(run.run_id)):
            payload = event.to_dict().get("payload", {})
            if isinstance(payload, Mapping) and payload.get("evidence_type") == "CANDIDATE_COLD_RESTART_CHECKPOINT":
                checkpoint = CandidateProgressCheckpointV2.from_dict(payload.get("payload"))
                envelope = payload
                break
        if checkpoint is None or envelope is None:
            return RestartAuthorizationV1(False, "no V2 cold restart checkpoint")
        reference = envelope.get("reference")
        prefix = "artifact://"
        if not isinstance(reference, str) or not reference.startswith(prefix):
            return RestartAuthorizationV1(False, "V2 event artifact reference missing", checkpoint.fingerprint)
        artifact = artifact_store.get_artifact(reference[len(prefix):])
        if (
            artifact.content != _canonical(checkpoint.to_dict())
            or artifact.metadata.get("fingerprint") != checkpoint.fingerprint
            or artifact.metadata.get("production_role") != "CANDIDATE_COLD_RESTART_CHECKPOINT"
        ):
            return RestartAuthorizationV1(False, "V2 event artifact binding mismatch", checkpoint.fingerprint)
        if checkpoint.contract_fingerprint != contract_fingerprint(contract):
            return RestartAuthorizationV1(False, "contract fingerprint mismatch", checkpoint.fingerprint)
        if checkpoint.worker_calls_used_total != run.quota_usage.worker_calls:
            return RestartAuthorizationV1(False, "RC3_WORKER_ACCOUNTING_MISMATCH", checkpoint.fingerprint)
        if checkpoint.tool_calls_used_total != _durable_tool_calls(run_store, checkpoint.run_id, checkpoint.todo_id):
            return RestartAuthorizationV1(False, "RC3_TOOL_ACCOUNTING_MISMATCH", checkpoint.fingerprint)
        if checkpoint.quota_usage != run.quota_usage.to_dict():
            return RestartAuthorizationV1(False, "RC-3 accounting changed", checkpoint.fingerprint)
        if (checkpoint.run_id, checkpoint.todo_id, checkpoint.lease_id, checkpoint.branch_name, checkpoint.base_commit) != (run.run_id, lease.todo_id, lease.lease_id, lease.branch_name, lease.base_commit):
            return RestartAuthorizationV1(False, "candidate identity mismatch", checkpoint.fingerprint)
        from .candidate_continuity import CandidateProgressCheckpointV1, validate_reuse
        legacy = CandidateProgressCheckpointV1(1, checkpoint.run_id, checkpoint.todo_id, checkpoint.lease_id, checkpoint.branch_name, checkpoint.base_commit, checkpoint.source_head_commit, checkpoint.diff_sha256, checkpoint.changed_paths, checkpoint.untracked_paths, 0, checkpoint.failure_kind)
        valid, reason = validate_reuse(manager=manager, lease=lease, checkpoint=legacy, contract=contract)
        if not valid:
            return RestartAuthorizationV1(False, reason, checkpoint.fingerprint)
        progress = json.loads(artifact_store.get_artifact(checkpoint.progress_artifact_id).content)
        evidence = progress.get("progress_evidence") if isinstance(progress, Mapping) else None
        continuation = progress.get("continuation_decision") if isinstance(progress, Mapping) else None
        if not isinstance(evidence, Mapping) or not isinstance(continuation, Mapping) or hashlib.sha256(_canonical(dict(evidence)).encode("utf-8")).hexdigest() != checkpoint.progress_evidence_fingerprint:
            return RestartAuthorizationV1(False, "RC-4 progress evidence mismatch", checkpoint.fingerprint)
        if (evidence.get("run_id"), evidence.get("todo_id"), evidence.get("lease_id"), evidence.get("slice_index"), evidence.get("progress_class"), continuation.get("intent"), continuation.get("evidence_fingerprint")) != (checkpoint.run_id, checkpoint.todo_id, checkpoint.lease_id, checkpoint.slice_index, checkpoint.progress_class, checkpoint.continuation_intent, checkpoint.progress_evidence_fingerprint):
            return RestartAuthorizationV1(False, "RC-4 progress identity mismatch", checkpoint.fingerprint)
        decision_doc = json.loads(artifact_store.get_artifact(checkpoint.decision_artifact_id).content)
        decision = decision_doc.get("decision") if isinstance(decision_doc, Mapping) else None
        if not isinstance(decision, Mapping) or hashlib.sha256(_canonical(dict(decision)).encode("utf-8")).hexdigest() != checkpoint.decision_fingerprint:
            return RestartAuthorizationV1(False, "RC-5 decision mismatch", checkpoint.fingerprint)
        if (decision.get("run_id"), decision.get("todo_id"), decision.get("lease_id"), decision.get("candidate_id"), decision.get("progress_evidence_fingerprint"), decision.get("disposition")) != (checkpoint.run_id, checkpoint.todo_id, checkpoint.lease_id, checkpoint.candidate_fingerprint, checkpoint.progress_evidence_fingerprint, CandidateDispositionV1.REUSE_CANDIDATE.value):
            return RestartAuthorizationV1(False, "RC-5 decision is not exact REUSE", checkpoint.fingerprint)
        return RestartAuthorizationV1(True, "authorized exact V2 candidate", checkpoint.fingerprint)
    except Exception as exc:
        return RestartAuthorizationV1(False, f"restart validation error: {type(exc).__name__}")
