"""RC-4 deterministic progress evidence and continuation intent."""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Iterable, Mapping

from ..context_manager import Artifact, ArtifactKind, ArtifactOrigin, ArtifactStore
from ..context_manager.integration import artifact_uri
from ..control_plane import Evidence, EvidenceStatus, RunStore
from .execution_slices import ExecutionSliceCheckpointV2, ExecutionSliceGrantDecision, is_test_verification_command
from .failure_policy import FailureKindV1


class ProgressClassV1(StrEnum):
    STRONG_PROGRESS = "STRONG_PROGRESS"
    WEAK_PROGRESS = "WEAK_PROGRESS"
    NO_PROGRESS = "NO_PROGRESS"
    REGRESSION = "REGRESSION"
    FINALIZED = "FINALIZED"


class ContinuationIntentV1(StrEnum):
    CONTINUE_SAME = "CONTINUE_SAME"
    CONTINUE_WITH_CONTEXT = "CONTINUE_WITH_CONTEXT"
    VERIFY_NOW = "VERIFY_NOW"
    REPLAN_OR_SPLIT = "REPLAN_OR_SPLIT"
    STOP_FOR_RECOVERY = "STOP_FOR_RECOVERY"
    BLOCK = "BLOCK"
    COMPLETE = "COMPLETE"

_FAILURE_KINDS = frozenset(item.value for item in FailureKindV1)


def _valid_failure_kind(value: object) -> bool:
    """Accept only RC-1's serialized FailureKindV1 values (or no failure)."""
    return value is None or (isinstance(value, str) and value in _FAILURE_KINDS)


@dataclass(frozen=True, slots=True)
class ProgressEvidenceV1:
    schema_version: int
    run_id: str
    todo_id: str
    lease_id: str | None
    slice_index: int
    previous_checkpoint_id: str | None
    previous_slice_index: int | None
    current_checkpoint_id: str | None
    previous_diff_sha256: str | None
    current_diff_sha256: str | None
    diff_changed: bool | None
    dirty_paths_before: tuple[str, ...]
    dirty_paths_after: tuple[str, ...]
    new_dirty_paths: tuple[str, ...]
    successful_mutations_before: int | None
    successful_mutations_after: int | None
    successful_mutations_delta: int | None
    successful_tool_calls_before: int | None
    successful_tool_calls_after: int | None
    successful_tool_calls_delta: int | None
    failed_tool_calls_before: int | None
    failed_tool_calls_after: int | None
    failed_tool_calls_delta: int | None
    unique_read_targets_delta: tuple[str, ...] | None
    unique_search_targets_delta: tuple[str, ...] | None
    repeated_call_fingerprints: tuple[str, ...]
    repeated_error_fingerprints: tuple[str, ...]
    tests_run_delta: int | None
    tests_passed_delta: int | None
    tests_failed_delta: int | None
    verification_attempted: bool | None
    verification_succeeded: bool | None
    verification_is_fresh: bool | None
    verification_became_fresh: bool | None
    verification_regressed: bool | None
    worker_calls_delta: int | None
    tool_calls_delta: int | None
    technical_failure_kind: str | None
    finalized: bool
    progress_class: ProgressClassV1
    progress_reasons: tuple[str, ...]
    evidence_valid: bool = True

    def __post_init__(self) -> None:
        if self.schema_version != 1 or not self.run_id or not self.todo_id or self.slice_index < 1:
            raise ValueError("invalid progress evidence identity")
        if self.previous_slice_index is not None and self.previous_slice_index >= self.slice_index:
            raise ValueError("previous slice must precede current slice")
        if not isinstance(self.progress_class, ProgressClassV1):
            raise ValueError("invalid progress class")
        for values in (self.dirty_paths_before, self.dirty_paths_after, self.new_dirty_paths, self.repeated_call_fingerprints, self.repeated_error_fingerprints, self.progress_reasons):
            if tuple(sorted(set(values))) != values or any(not isinstance(value, str) or not value for value in values):
                raise ValueError("progress collections must be sorted unique text")

    def to_dict(self) -> dict[str, Any]:
        result = {}
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            result[name] = value.value if isinstance(value, StrEnum) else list(value) if isinstance(value, tuple) else value
        return result

    def canonical_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ContinuationDecisionV1:
    schema_version: int
    run_id: str
    todo_id: str
    slice_index: int
    evidence_fingerprint: str
    intent: ContinuationIntentV1
    reason: str
    technical_failure_kind: str | None

    def __post_init__(self) -> None:
        if self.schema_version != 1 or not self.run_id or not self.todo_id or self.slice_index < 1:
            raise ValueError("invalid continuation decision identity")
        if not isinstance(self.evidence_fingerprint, str) or not self.evidence_fingerprint:
            raise ValueError("continuation decision requires evidence fingerprint")
        if not isinstance(self.intent, ContinuationIntentV1) or not isinstance(self.reason, str) or not self.reason:
            raise ValueError("invalid continuation decision")

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "run_id": self.run_id, "todo_id": self.todo_id, "slice_index": self.slice_index, "evidence_fingerprint": self.evidence_fingerprint, "intent": self.intent.value, "reason": self.reason, "technical_failure_kind": self.technical_failure_kind}


def _events(run_store: RunStore, run_id: str, todo_id: str) -> list[Mapping[str, Any]]:
    values = []
    for event in run_store.list_events(run_id):
        if event.event_type.value != "EVIDENCE_RECORDED":
            continue
        payload = event.to_dict().get("payload", {})
        if isinstance(payload, Mapping) and payload.get("todo_id") == todo_id and payload.get("evidence_type") == "TOOL_RESULT":
            values.append(payload)
    return values


def _target(detail: Mapping[str, Any], keys: tuple[str, ...]) -> str | None:
    args = detail.get("arguments", {})
    if isinstance(args, Mapping):
        for key in keys:
            value = args.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _stats(events: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    values: dict[str, Any] = {"success": 0, "failed": 0, "mutations": 0, "reads": set(), "searches": set(), "tests": 0, "passed": 0, "test_failed": 0, "calls": [], "errors": []}
    for event in events:
        detail = event.get("payload", {})
        if not isinstance(detail, Mapping):
            continue
        succeeded = event.get("status") == EvidenceStatus.PASSED.value and detail.get("tool_status") == "SUCCESS"
        values["success" if succeeded else "failed"] += 1
        if succeeded and detail.get("changed_paths"):
            values["mutations"] += 1
        tool = str(detail.get("tool", "")).upper()
        if tool == "READ_TEXT":
            target = _target(detail, ("path", "file", "target"))
            if target is not None:
                values["reads"].add(target)
        if tool.startswith("SEARCH"):
            target = _target(detail, ("query", "pattern", "text"))
            if target is not None:
                values["searches"].add(target)
        fingerprint = detail.get("fingerprint")
        if isinstance(fingerprint, str) and fingerprint:
            values["calls"].append(fingerprint)
            if not succeeded:
                values["errors"].append(fingerprint)
        arguments = detail.get("arguments")
        if is_test_verification_command(str(detail.get("tool", "")), dict(arguments) if isinstance(arguments, Mapping) else None):
            values["tests"] += 1
            exit_code = detail.get("metadata", {}).get("exit_code") if isinstance(detail.get("metadata"), Mapping) else None
            passed = succeeded and isinstance(exit_code, int) and not isinstance(exit_code, bool) and exit_code == 0
            values["passed" if passed else "test_failed"] += 1
    return values


class ProgressEvidenceBuilder:
    def build(self, *, run_store: RunStore, run_id: str, todo_id: str, lease_id: str | None, slice_index: int, previous_checkpoint: ExecutionSliceCheckpointV2 | None, previous_checkpoint_id: str | None, current_checkpoint_id: str | None, worker_calls_after: int, tool_calls_after: int, dirty_paths_after: tuple[str, ...], current_diff_sha256: str | None, verification_attempted: bool, verification_succeeded: bool, verification_diff_sha256: str | None, finalized: bool, technical_failure_kind: str | None) -> ProgressEvidenceV1:
        if not run_id or not todo_id or slice_index < 1:
            raise ValueError("progress evidence requires valid run/todo/slice identity")
        events = _events(run_store, run_id, todo_id)
        previous_tools = previous_checkpoint.tool_calls_used_total if previous_checkpoint else 0
        valid = (
            isinstance(worker_calls_after, int) and not isinstance(worker_calls_after, bool)
            and isinstance(tool_calls_after, int) and not isinstance(tool_calls_after, bool)
            and worker_calls_after >= 0 and tool_calls_after >= 0
            and tool_calls_after <= len(events) and previous_tools <= tool_calls_after
            and (previous_checkpoint is None or (
                previous_checkpoint.run_id == run_id
                and previous_checkpoint.todo_id == todo_id
                and previous_checkpoint.slice_index + 1 == slice_index
                and worker_calls_after >= previous_checkpoint.worker_calls_used_total
                and tool_calls_after >= previous_checkpoint.tool_calls_used_total
            ))
        )
        current_events = events[:tool_calls_after] if valid else events
        before_events = current_events[:previous_tools] if valid else []
        before, after = _stats(before_events), _stats(current_events)
        previous_diff = previous_checkpoint.diff_sha256 if previous_checkpoint else None
        dirty_before = previous_checkpoint.dirty_paths if previous_checkpoint else ()
        diff_changed = previous_diff != current_diff_sha256 if previous_checkpoint else None
        new_dirty = tuple(sorted(set(dirty_paths_after) - set(dirty_before)))
        fresh_before = bool(previous_checkpoint and previous_checkpoint.verification_succeeded and previous_checkpoint.verification_diff_sha256 == previous_checkpoint.diff_sha256)
        fresh_now = bool(verification_succeeded and verification_diff_sha256 and current_diff_sha256 and verification_diff_sha256 == current_diff_sha256)
        regressed = bool(
            fresh_before and not diff_changed and (
                after["test_failed"] > before["test_failed"]
                or (verification_attempted and not verification_succeeded)
            )
        )
        repeated_calls = tuple(sorted(key for key, count in Counter(after["calls"]).items() if count > 1))
        repeated_errors = tuple(sorted(key for key, count in Counter(after["errors"]).items() if count > 1))
        mutation_delta, success_delta, failed_delta = after["mutations"] - before["mutations"], after["success"] - before["success"], after["failed"] - before["failed"]
        reads_delta = tuple(sorted(after["reads"] - before["reads"]))
        searches_delta = tuple(sorted(after["searches"] - before["searches"]))
        if finalized:
            progress, reasons = ProgressClassV1.FINALIZED, ("worker finalized",)
        elif not valid:
            progress, reasons = ProgressClassV1.NO_PROGRESS, ("durable checkpoint or counter accounting is invalid",)
        elif regressed:
            progress, reasons = ProgressClassV1.REGRESSION, ("same verified diff later has failed verification evidence",)
        elif mutation_delta > 0 or diff_changed or new_dirty or (not fresh_before and fresh_now):
            reasons_list = []
            if mutation_delta > 0: reasons_list.append("new successful mutation")
            if diff_changed: reasons_list.append("candidate diff changed")
            if new_dirty: reasons_list.append("new dirty path")
            if not fresh_before and fresh_now: reasons_list.append("verification became fresh")
            progress, reasons = ProgressClassV1.STRONG_PROGRESS, tuple(sorted(reasons_list))
        elif reads_delta or searches_delta:
            reasons_list = []
            if reads_delta: reasons_list.append("new distinct read target")
            if searches_delta: reasons_list.append("new distinct search target")
            progress, reasons = ProgressClassV1.WEAK_PROGRESS, tuple(sorted(reasons_list))
        else:
            reasons_list = ["no new durable mutation, diff, verification, read, or search evidence"]
            if repeated_calls: reasons_list.append("repeated tool request fingerprint")
            if repeated_errors: reasons_list.append("repeated failed tool request fingerprint")
            progress, reasons = ProgressClassV1.NO_PROGRESS, tuple(sorted(reasons_list))
        return ProgressEvidenceV1(1, run_id, todo_id, lease_id, slice_index, previous_checkpoint_id, previous_checkpoint.slice_index if previous_checkpoint else None, current_checkpoint_id, previous_diff, current_diff_sha256, diff_changed, tuple(sorted(dirty_before)), tuple(sorted(dirty_paths_after)), new_dirty, before["mutations"], after["mutations"], mutation_delta, before["success"], after["success"], success_delta, before["failed"], after["failed"], failed_delta, reads_delta, searches_delta, repeated_calls, repeated_errors, after["tests"] - before["tests"], after["passed"] - before["passed"], after["test_failed"] - before["test_failed"], verification_attempted, verification_succeeded, fresh_now, not fresh_before and fresh_now, regressed, worker_calls_after - (previous_checkpoint.worker_calls_used_total if previous_checkpoint else 0), tool_calls_after - previous_tools, technical_failure_kind, finalized, progress, reasons, valid)


class ContinuationEvaluator:
    def evaluate(self, evidence: ProgressEvidenceV1, *, capacity_decision: ExecutionSliceGrantDecision | str, semantic_replan: bool = False) -> ContinuationDecisionV1:
        capacity = capacity_decision.value if isinstance(capacity_decision, StrEnum) else str(capacity_decision)
        def result(intent: ContinuationIntentV1, reason: str) -> ContinuationDecisionV1:
            return ContinuationDecisionV1(1, evidence.run_id, evidence.todo_id, evidence.slice_index, evidence.fingerprint, intent, reason, evidence.technical_failure_kind)
        if not evidence.evidence_valid or not _valid_failure_kind(evidence.technical_failure_kind) or evidence.technical_failure_kind == FailureKindV1.UNKNOWN.value:
            return result(ContinuationIntentV1.BLOCK, "unknown or malformed durable technical evidence")
        if semantic_replan:
            return result(ContinuationIntentV1.REPLAN_OR_SPLIT, "typed Judge semantic replan takes precedence")
        if evidence.finalized or evidence.progress_class is ProgressClassV1.FINALIZED:
            return result(ContinuationIntentV1.COMPLETE, "worker finalized; verification remains authoritative")
        if capacity == ExecutionSliceGrantDecision.REACHED_HARD_CEILING.value:
            return result(ContinuationIntentV1.STOP_FOR_RECOVERY, "RC-3 hard capacity ceiling reached")
        if evidence.technical_failure_kind in {"TOOL_LOOP", "TOOL_FAILURE", "PROTOCOL_INVALID", "CONTEXT_BUDGET"}:
            return result(ContinuationIntentV1.STOP_FOR_RECOVERY, "typed technical failure requires existing recovery authority")
        if capacity != ExecutionSliceGrantDecision.GRANT_NEXT_SLICE.value:
            return result(ContinuationIntentV1.STOP_FOR_RECOVERY, "RC-3 did not grant usable continuation capacity")
        if evidence.dirty_paths_after and not evidence.verification_is_fresh:
            return result(ContinuationIntentV1.VERIFY_NOW, "dirty current diff is not freshly verified")
        if evidence.progress_class is ProgressClassV1.STRONG_PROGRESS:
            return result(ContinuationIntentV1.CONTINUE_SAME, "strong durable progress with RC-3 capacity")
        if evidence.progress_class is ProgressClassV1.WEAK_PROGRESS and not evidence.repeated_call_fingerprints:
            return result(ContinuationIntentV1.CONTINUE_WITH_CONTEXT, "bounded weak discovery; no new context consumer is created")
        return result(ContinuationIntentV1.STOP_FOR_RECOVERY, "no-progress or regression evidence does not justify continuation")


def persist_progress_evidence(artifact_store: ArtifactStore, run_store: RunStore, evidence: ProgressEvidenceV1, decision: ContinuationDecisionV1) -> str:
    if (
        decision.run_id != evidence.run_id
        or decision.todo_id != evidence.todo_id
        or decision.slice_index != evidence.slice_index
        or decision.evidence_fingerprint != evidence.fingerprint
    ):
        raise ValueError("continuation decision does not bind to progress evidence")
    content = json.dumps({"progress_evidence": evidence.to_dict(), "continuation_decision": decision.to_dict()}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    # The continuation decision is part of the durable record.  The same
    # progress snapshot may legitimately be evaluated under a different RC-3
    # capacity decision, so an identity derived from the evidence alone is not
    # sufficient to identify immutable artifact content.
    identity = hashlib.sha256(content.encode("utf-8")).hexdigest()
    artifact_id = f"progress-evidence:{identity}"
    artifact = Artifact.from_text(artifact_id=artifact_id, kind=ArtifactKind.CONTROL_PLANE_EVIDENCE, origin=ArtifactOrigin.CONTROL_PLANE, content=content, media_type="application/json", source="ProgressEvidenceV1", created_at="1970-01-01T00:00:00+00:00", scope_tags=(evidence.run_id, evidence.todo_id), metadata={"production_role": "PROGRESS_EVIDENCE", "fingerprint": evidence.fingerprint, "slice_index": evidence.slice_index})
    if artifact_store.has_artifact(artifact_id):
        if artifact_store.get_artifact(artifact_id).content != content:
            raise RuntimeError("progress evidence identity conflict")
    else:
        artifact_store.create_artifact(artifact)
    run_store.record_evidence(evidence.run_id, Evidence(evidence_id=f"{evidence.run_id}:progress-evidence:{evidence.slice_index}:{identity[:16]}", todo_id=evidence.todo_id, evidence_type="PROGRESS_EVIDENCE", source="ProgressEvidenceV1", status=EvidenceStatus.PASSED if evidence.evidence_valid else EvidenceStatus.FAILED, payload={"schema_version": 1, "slice_index": evidence.slice_index, "progress_class": evidence.progress_class.value, "continuation_intent": decision.intent.value, "technical_failure_kind": evidence.technical_failure_kind, "fingerprint": evidence.fingerprint}, reference=artifact_uri(artifact_id)))
    return artifact_id
