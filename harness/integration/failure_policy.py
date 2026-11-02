"""Deterministic production classification of execution failures."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping


class RecoveryWorkspaceDispositionV1(StrEnum):
    REUSE_CANDIDATE = "REUSE_CANDIDATE"
    FRESH_CANDIDATE = "FRESH_CANDIDATE"
    BLOCK = "BLOCK"


class FailureKindV1(StrEnum):
    TOOL_LOOP = "TOOL_LOOP"
    TOOL_FAILURE = "TOOL_FAILURE"
    PROTOCOL_INVALID = "PROTOCOL_INVALID"
    WORKER_QUOTA = "WORKER_QUOTA"
    TOOL_CALL_QUOTA = "TOOL_CALL_QUOTA"
    CONTEXT_BUDGET = "CONTEXT_BUDGET"
    CONTEXT_INSUFFICIENT = "CONTEXT_INSUFFICIENT"
    RESOURCE_INSUFFICIENT = "RESOURCE_INSUFFICIENT"
    MODEL_TIMEOUT = "MODEL_TIMEOUT"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class FailureRecordV1:
    schema_version: int
    failure_kind: str
    execution_outcome: str
    reason: str
    context_expansion_allowed: bool
    worker_calls: int
    tool_calls: int
    changed_paths: tuple[str, ...]
    context_profile_id: str | None
    last_worker_model_id: str | None
    evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("FailureRecordV1 schema_version must be 1")
        if self.failure_kind not in {item.value for item in FailureKindV1}:
            raise ValueError("unknown deterministic failure kind")
        if not isinstance(self.execution_outcome, str) or not isinstance(self.reason, str):
            raise ValueError("failure outcome and reason must be text")
        if not isinstance(self.context_expansion_allowed, bool):
            raise ValueError("context_expansion_allowed must be bool")
        for value in (self.worker_calls, self.tool_calls):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError("failure counters must be non-negative integers")
        if any(not isinstance(path, str) or not path for path in self.changed_paths):
            raise ValueError("changed_paths must contain text")
        if any(not isinstance(item, str) or not item for item in self.evidence_ids):
            raise ValueError("evidence_ids must contain text")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "failure_kind": self.failure_kind,
            "execution_outcome": self.execution_outcome,
            "reason": self.reason,
            "context_expansion_allowed": self.context_expansion_allowed,
            "worker_calls": self.worker_calls,
            "tool_calls": self.tool_calls,
            "changed_paths": list(self.changed_paths),
            "context_profile_id": self.context_profile_id,
            "last_worker_model_id": self.last_worker_model_id,
            "evidence_ids": list(self.evidence_ids),
        }


def recovery_workspace_disposition(failure_kind: str) -> RecoveryWorkspaceDispositionV1:
    """Strict allowlist: only repairable, observed failures may retain a candidate."""
    if failure_kind in {FailureKindV1.TOOL_LOOP.value, FailureKindV1.VERIFICATION_FAILED.value}:
        return RecoveryWorkspaceDispositionV1.REUSE_CANDIDATE
    if failure_kind == FailureKindV1.UNKNOWN.value:
        return RecoveryWorkspaceDispositionV1.BLOCK
    return RecoveryWorkspaceDispositionV1.FRESH_CANDIDATE


_RECOGNIZED_OUTCOMES = frozenset({
    *(item.value for item in FailureKindV1),
    "SUCCESS", "WAITING_EVIDENCE", "NEEDS_ATTENTION", "QUOTA_EXHAUSTED", "LOOP_DETECTED", "ALREADY_FINALIZED",
    "REPLAN_REQUIRED", "EXECUTION_FAILURE", "CONTEXT INSUFFICIENT", "LOOP DETECTED", "", "NONE",
})


def classify_failure(*, execution: Any, verification: Any | None = None) -> FailureRecordV1:
    """Classify harness-observable facts; typed structured signals take precedence."""
    raw = getattr(execution, "raw", {})
    raw = dict(raw) if isinstance(raw, (dict, Mapping)) else {}
    agent = raw.get("agent_result")
    agent = dict(agent) if isinstance(agent, (dict, Mapping)) else {}

    raw_outcome = getattr(execution, "outcome", getattr(execution, "execution_outcome", "UNKNOWN"))
    outcome = getattr(raw_outcome, "value", str(raw_outcome)) if raw_outcome is not None else "UNKNOWN"
    reason = str(getattr(execution, "summary", ""))

    declared_kind = raw.get("failure_kind") or getattr(execution, "failure_kind", None)
    attention_category = raw.get("attention_category")
    agent_outcome = str(agent.get("outcome", "")).strip().upper()
    agent_reason = str(agent.get("reason", "")).strip()

    kind = FailureKindV1.UNKNOWN
    allowed = False

    outcome_upper = outcome.strip().upper()

    # 1. Primary: Authoritative typed failure_kind / category / outcome
    if declared_kind is not None:
        if declared_kind in {item.value for item in FailureKindV1}:
            kind = FailureKindV1(declared_kind)
            allowed = (kind is FailureKindV1.CONTEXT_INSUFFICIENT)
        else:
            kind = FailureKindV1.UNKNOWN
    elif outcome_upper in {item.value for item in FailureKindV1}:
        kind = FailureKindV1(outcome_upper)
        allowed = (kind is FailureKindV1.CONTEXT_INSUFFICIENT)
    elif outcome_upper == "LOOP_DETECTED" or agent_outcome == "LOOP_DETECTED":
        kind = FailureKindV1.TOOL_LOOP
    elif outcome_upper == "QUOTA_EXHAUSTED" or agent_outcome == "QUOTA_EXHAUSTED":
        check_reason = (agent_reason or reason).casefold()
        if "tool call quota exhausted" in check_reason:
            kind = FailureKindV1.TOOL_CALL_QUOTA
        else:
            kind = FailureKindV1.WORKER_QUOTA
    elif attention_category == "tool":
        kind = FailureKindV1.TOOL_FAILURE
    elif attention_category == "agent_protocol":
        kind = FailureKindV1.PROTOCOL_INVALID
    elif attention_category == "context":
        kind = FailureKindV1.CONTEXT_BUDGET
    elif agent_outcome in {item.value for item in FailureKindV1}:
        kind = FailureKindV1(agent_outcome)
        allowed = (kind is FailureKindV1.CONTEXT_INSUFFICIENT)

    # 2. Secondary: If no typed signal matched, check compatibility fallback on recognized legacy outcomes
    if kind is FailureKindV1.UNKNOWN and outcome_upper in _RECOGNIZED_OUTCOMES:
        normalized_outcome = outcome.casefold().replace("_", " ").replace("-", " ")
        folded = f"{normalized_outcome} {reason.casefold().replace('_', ' ').replace('-', ' ')}"
        if normalized_outcome == "loop detected" or "repeated identical tool call" in folded:
            kind = FailureKindV1.TOOL_LOOP
        elif "tool failure limit reached" in folded:
            kind = FailureKindV1.TOOL_FAILURE
        elif "protocol failure limit" in folded or "agent protocol" in folded:
            kind = FailureKindV1.PROTOCOL_INVALID
        elif "worker call quota exhausted" in folded:
            kind = FailureKindV1.WORKER_QUOTA
        elif "tool call quota exhausted" in folded:
            kind = FailureKindV1.TOOL_CALL_QUOTA
        elif "contextbudgeterror" in folded or "required phase 16 context cannot fit" in folded:
            kind = FailureKindV1.CONTEXT_BUDGET
        elif normalized_outcome == "context insufficient" or "context insufficient" in folded:
            kind, allowed = FailureKindV1.CONTEXT_INSUFFICIENT, True
        elif "timeout" in folded or "timed out" in folded:
            kind = FailureKindV1.MODEL_TIMEOUT
        elif "resource" in folded or "vram" in folded:
            kind = FailureKindV1.RESOURCE_INSUFFICIENT
        elif verification is not None and not bool(getattr(verification, "passed", False)):
            kind = FailureKindV1.VERIFICATION_FAILED

    evidence = tuple(getattr(verification, "evidence_ids", ()) or ())
    last_model = agent.get("last_model_id")
    return FailureRecordV1(
        1, kind.value, outcome, reason, allowed,
        int(getattr(execution, "model_calls", getattr(execution, "model_turns", 0))), int(getattr(execution, "tool_calls", 0)),
        tuple(getattr(execution, "changed_paths", ()) or ()),
        getattr(execution, "profile_id", None), last_model if isinstance(last_model, str) else None,
        tuple(item for item in evidence if isinstance(item, str)),
    )


class DeterministicProductionRecoveryBridge:
    'Make recovery analysis durable evidence, never deterministic authority.'

    def __init__(self, bridge: Any, *, artifact_store: Any, max_context_expansions: int = 2) -> None:
        self.bridge, self.artifact_store = bridge, artifact_store
        self.max_context_expansions = max_context_expansions

    def _expansions_used(self, todo_id: str) -> int:
        count = 0
        for ref in self.artifact_store.list_refs():
            artifact = self.artifact_store.get_artifact(ref.artifact_id)
            if artifact.metadata.get("phase16_role") != "RECOVERY_CONTEXT" or artifact.metadata.get("todo_id") != todo_id:
                continue
            try:
                outer = __import__("json").loads(artifact.content)
                recovery = outer["recovery"]
            except (TypeError, KeyError, ValueError) as exc:
                raise RuntimeError(f"malformed matching durable recovery artifact: {artifact.artifact_id}") from exc
            if not isinstance(recovery, dict):
                raise RuntimeError(f"malformed matching durable recovery artifact: {artifact.artifact_id}")
            action = recovery.get("production_action", recovery.get("controller_action"))
            if action == "EXPAND_CONTEXT":
                count += 1
        return count

    def recover(self, todo, **kwargs):
        from ..workflow.models import RecoveryObservationV1
        try:
            expansions_used = self._expansions_used(todo.todo_id)
        except Exception as exc:
            return RecoveryObservationV1(False, reason="malformed expansion history fails closed", raw={"production_action": "STOP", "deterministic_failure_error": str(exc)})
        result = self.bridge.recover(todo, **kwargs)
        verif = kwargs.get("verification")
        verif_passed = verif is not None and bool(getattr(verif, "passed", False))
        failure = classify_failure(execution=kwargs["execution"], verification=kwargs.get("verification")) if not verif_passed else None
        raw = dict(result.raw)
        diagnosis_action = raw.get("controller_action")
        production_action = diagnosis_action or "STOP"
        continue_work = result.continue_work
        reason = result.reason
        denial = None
        if diagnosis_action == "EXPAND_CONTEXT":
            if not verif_passed and failure is not None and not failure.context_expansion_allowed:
                production_action, continue_work = "RETRY_EXECUTION", True
                reason = "deterministic failure requires bounded retry, not context expansion"
                denial = f"deterministic {failure.failure_kind} does not justify context expansion"
            elif expansions_used >= self.max_context_expansions:
                production_action, continue_work = "STOP", False
                reason = "max_context_expansions_per_todo exhausted"
                denial = "context expansion hard budget exhausted"
        if not verif_passed and failure is not None:
            raw.update({"deterministic_failure": failure.to_dict()})
        raw.update({
            "phase15_requested_action": diagnosis_action,
            "production_action": production_action,
            "context_expansions_used": expansions_used,
            "max_context_expansions_per_todo": self.max_context_expansions,
        })
        if denial is not None:
            raw["context_expansion_denied_reason"] = denial
        return RecoveryObservationV1(
            continue_work,
            revised_objective=kwargs["objective"] if production_action == "RETRY_EXECUTION" else (result.revised_objective if continue_work else None),
            context_artifact_ids=result.context_artifact_ids,
            reason=reason,
            routing_task_spec=None if production_action == "RETRY_EXECUTION" else (result.routing_task_spec if continue_work else None),
            expansion_queries=() if production_action == "RETRY_EXECUTION" else (result.expansion_queries if continue_work else ()),
            raw=raw,
        )