"""Integration-only bounded production execution slices and quota headroom hygiene."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

from ..agent_loop import ControlPlaneCodingAgent
from ..context_manager import Artifact, ArtifactKind, ArtifactOrigin, ArtifactStore
from ..control_plane import EvidenceStatus, Run, RunStore, WorkContract
from .progress_budget import ProgressBudgetExecutor
from .aci_feedback import ProductionAciFeedbackDispatcher
from .d3_frozen_repair import ProductionSearchTextContractDispatcher
from .workspace_path_contract import ProductionWorkspacePathContractDispatcher
from .anti_loop_window import ProductionWindowedCodingAgentLoop


def _positive(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _nonnegative(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


class ExecutionSliceGrantDecision(StrEnum):
    GRANT_NEXT_SLICE = "GRANT_NEXT_SLICE"
    STOP_FOR_RECOVERY = "STOP_FOR_RECOVERY"
    REACHED_HARD_CEILING = "REACHED_HARD_CEILING"
    FINALIZED = "FINALIZED"


class FinalizationStage(StrEnum):
    DIRTY_UNVERIFIED = "DIRTY_UNVERIFIED"
    VERIFIED_READY_TO_FINALIZE = "VERIFIED_READY_TO_FINALIZE"
    FINALIZED = "FINALIZED"
    CLEAN_UNVERIFIED = "CLEAN_UNVERIFIED"


@dataclass(frozen=True, slots=True)
class ProductionExecutionSlicePolicy:
    max_worker_calls_per_slice: int = 6
    max_slices_per_todo: int = 3
    max_tool_calls_hard: int = 12

    def __post_init__(self) -> None:
        _positive(self.max_worker_calls_per_slice, "max_worker_calls_per_slice")
        _positive(self.max_slices_per_todo, "max_slices_per_todo")
        _positive(self.max_tool_calls_hard, "max_tool_calls_hard")

    @property
    def max_total_worker_calls_per_todo(self) -> int:
        return self.max_worker_calls_per_slice * self.max_slices_per_todo

    def cumulative_ceiling(self, slice_index: int) -> int:
        _positive(slice_index, "slice_index")
        if slice_index > self.max_slices_per_todo:
            raise ValueError("slice_index exceeds max_slices_per_todo")
        return self.max_worker_calls_per_slice * slice_index


def is_test_verification_command(tool_name: str, arguments: dict[str, Any] | None) -> bool:
    """Deterministically identify actual test/verification commands vs generic tools."""
    if not isinstance(arguments, dict):
        return False
    cmd = str(arguments.get("command", "") or arguments.get("cmd", "")).strip()
    if not cmd:
        return False
    parts = cmd.split()
    if not parts:
        return False
    first = parts[0].lower()

    if first in {"pytest", "pytest.exe", "unittest"}:
        return True
    if first in {"python", "python3", "python.exe", "python3.exe", "py"}:
        if len(parts) >= 3 and parts[1] == "-m" and parts[2].lower() in {"pytest", "unittest"}:
            return True
    if len(parts) >= 2 and first in {"cargo", "npm", "go", "yarn", "pnpm"} and parts[1].lower() == "test":
        return True
    return False


def evaluate_verification_freshness(
    run_store: RunStore,
    run_id: str,
    todo_id: str,
    current_diff_sha256: str | None = None,
) -> tuple[bool, bool, bool, str | None]:
    """Check whether a valid test verification occurred AFTER the latest mutation.

    Returns:
        (is_fresh, verification_attempted, verification_succeeded, last_test_diff_sha)
    """
    events = run_store.list_events(run_id)
    last_mutation_seq = -1
    last_test_seq = -1
    verification_attempted = False
    verification_succeeded = False
    last_test_diff_sha: str | None = None

    for event in events:
        if event.event_type.value != "EVIDENCE_RECORDED":
            continue
        payload = event.to_dict().get("payload", {})
        if payload.get("todo_id") != todo_id:
            continue
        ev_type = payload.get("evidence_type")
        detail = payload.get("payload", {})
        if not isinstance(detail, dict):
            continue

        if ev_type == "TOOL_RESULT":
            status_val = payload.get("status")
            tool_status = detail.get("tool_status")
            changed = detail.get("changed_paths", [])

            # Mutation event
            if bool(changed) and tool_status == "SUCCESS" and status_val == EvidenceStatus.PASSED.value:
                if event.sequence > last_mutation_seq:
                    last_mutation_seq = event.sequence

            # Test command event
            tool_name = detail.get("tool", "")
            arguments = detail.get("arguments", {})
            if is_test_verification_command(tool_name, arguments):
                verification_attempted = True
                meta = detail.get("metadata", {})
                exit_code = meta.get("exit_code", 0) if isinstance(meta, dict) else 0
                is_passed = (
                    status_val == EvidenceStatus.PASSED.value
                    and tool_status == "SUCCESS"
                    and exit_code == 0
                )
                if is_passed:
                    verification_succeeded = True
                    if event.sequence > last_test_seq:
                        last_test_seq = event.sequence
                        last_test_diff_sha = detail.get("diff_sha256") or detail.get("candidate_diff_sha256")

    is_fresh = (
        verification_succeeded
        and last_test_seq > last_mutation_seq
    )
    if is_fresh and current_diff_sha256 is not None and last_test_diff_sha is not None:
        if current_diff_sha256 != last_test_diff_sha:
            is_fresh = False

    return is_fresh, verification_attempted, verification_succeeded, last_test_diff_sha


@dataclass(frozen=True, slots=True)
class ExecutionSliceCheckpointV1:
    schema_version: int
    run_id: str
    todo_id: str
    slice_index: int
    max_slices: int
    worker_calls_before: int
    worker_calls_after: int
    slice_worker_calls: int
    tool_calls_total: int
    changed_paths: tuple[str, ...]
    last_model_id: str | None
    context_bundle_id: str
    status: str
    reason: str

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("schema_version must be exactly 1")
        for name in ("run_id", "todo_id", "context_bundle_id", "status", "reason"):
            _text(getattr(self, name), name)
        _positive(self.slice_index, "slice_index")
        _positive(self.max_slices, "max_slices")
        if self.slice_index >= self.max_slices:
            raise ValueError("CONTINUE checkpoint must precede the final slice")
        for name in ("worker_calls_before", "worker_calls_after", "slice_worker_calls", "tool_calls_total"):
            _nonnegative(getattr(self, name), name)
        if self.worker_calls_after < self.worker_calls_before:
            raise ValueError("worker calls cannot decrease")
        if self.slice_worker_calls != self.worker_calls_after - self.worker_calls_before:
            raise ValueError("slice_worker_calls must equal the observed worker-call delta")
        if self.slice_worker_calls <= 0:
            raise ValueError("continuation requires worker-call progress")
        if not isinstance(self.changed_paths, tuple) or any(not isinstance(path, str) or not path.strip() for path in self.changed_paths):
            raise ValueError("changed_paths must be a tuple of non-empty strings")
        if tuple(sorted(set(self.changed_paths))) != self.changed_paths:
            raise ValueError("changed_paths must be unique and sorted")
        if self.last_model_id is not None:
            _text(self.last_model_id, "last_model_id")
        if self.status != "CONTINUE":
            raise ValueError("V1 checkpoints only represent CONTINUE boundaries")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "todo_id": self.todo_id,
            "slice_index": self.slice_index,
            "max_slices": self.max_slices,
            "worker_calls_before": self.worker_calls_before,
            "worker_calls_after": self.worker_calls_after,
            "slice_worker_calls": self.slice_worker_calls,
            "tool_calls_total": self.tool_calls_total,
            "changed_paths": list(self.changed_paths),
            "last_model_id": self.last_model_id,
            "context_bundle_id": self.context_bundle_id,
            "status": self.status,
            "reason": self.reason,
        }

    def canonical_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ExecutionSliceCheckpointV2:
    schema_version: int
    run_id: str
    todo_id: str
    slice_index: int
    max_slices: int
    worker_calls_used_total: int
    tool_calls_used_total: int
    worker_soft_ceiling: int
    worker_hard_ceiling: int
    tool_hard_ceiling: int
    changed_paths: tuple[str, ...]
    dirty_paths: tuple[str, ...]
    diff_sha256: str | None
    verification_attempted: bool
    verification_succeeded: bool
    verification_diff_sha256: str | None
    finalized: bool
    grant_decision: str
    grant_reason: str
    last_model_id: str | None
    context_bundle_id: str
    status: str = "CONTINUE"
    reason: str = ""
    exploration_extension_used: bool = False

    def __post_init__(self) -> None:
        if self.schema_version != 2:
            raise ValueError("schema_version must be exactly 2")
        for name in ("run_id", "todo_id", "context_bundle_id", "grant_decision", "grant_reason"):
            _text(getattr(self, name), name)
        _positive(self.slice_index, "slice_index")
        _positive(self.max_slices, "max_slices")
        for name in (
            "worker_calls_used_total",
            "tool_calls_used_total",
            "worker_soft_ceiling",
            "worker_hard_ceiling",
            "tool_hard_ceiling",
        ):
            _nonnegative(getattr(self, name), name)
        if not isinstance(self.changed_paths, tuple) or any(not isinstance(path, str) or not path.strip() for path in self.changed_paths):
            raise ValueError("changed_paths must be a tuple of non-empty strings")
        if tuple(sorted(set(self.changed_paths))) != self.changed_paths:
            raise ValueError("changed_paths must be unique and sorted")
        if not isinstance(self.dirty_paths, tuple) or any(not isinstance(path, str) or not path.strip() for path in self.dirty_paths):
            raise ValueError("dirty_paths must be a tuple of non-empty strings")
        if self.last_model_id is not None:
            _text(self.last_model_id, "last_model_id")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "todo_id": self.todo_id,
            "slice_index": self.slice_index,
            "max_slices": self.max_slices,
            "worker_calls_used_total": self.worker_calls_used_total,
            "tool_calls_used_total": self.tool_calls_used_total,
            "worker_soft_ceiling": self.worker_soft_ceiling,
            "worker_hard_ceiling": self.worker_hard_ceiling,
            "tool_hard_ceiling": self.tool_hard_ceiling,
            "changed_paths": list(self.changed_paths),
            "dirty_paths": list(self.dirty_paths),
            "diff_sha256": self.diff_sha256,
            "verification_attempted": self.verification_attempted,
            "verification_succeeded": self.verification_succeeded,
            "verification_diff_sha256": self.verification_diff_sha256,
            "finalized": self.finalized,
            "grant_decision": self.grant_decision,
            "grant_reason": self.grant_reason,
            "last_model_id": self.last_model_id,
            "context_bundle_id": self.context_bundle_id,
            "status": self.status,
            "reason": self.reason or self.grant_reason,
            "exploration_extension_used": self.exploration_extension_used,
        }

    def canonical_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


class ExecutionSliceEvaluator:
    """Deterministic evaluation of execution slice continuation and tool headroom guards."""

    def __init__(self, policy: ProductionExecutionSlicePolicy | None = None) -> None:
        self.policy = policy or ProductionExecutionSlicePolicy()

    def evaluate(
        self,
        *,
        result: Any,
        run: Run,
        todo_id: str,
        slice_index: int,
        run_store: RunStore,
        dirty_paths: tuple[str, ...] = (),
        diff_sha256: str | None = None,
    ) -> tuple[ExecutionSliceGrantDecision, str, FinalizationStage, bool, bool, str | None]:
        outcome_val = getattr(getattr(result, "outcome", None), "value", str(getattr(result, "outcome", None)))
        is_finalized = outcome_val in {"SUCCESS", "ALREADY_FINALIZED"}

        is_fresh, verif_att, verif_succ, last_test_diff_sha = evaluate_verification_freshness(
            run_store,
            run.run_id,
            todo_id,
            diff_sha256,
        )

        worker_calls_used = run.quota_usage.worker_calls
        tool_calls_used = getattr(result, "tool_calls", 0)
        changed_paths = tuple(sorted(getattr(result, "changed_paths", ()) or ()))
        has_mutations = bool(changed_paths) or bool(dirty_paths)

        # 1. Finalized
        if is_finalized:
            return (
                ExecutionSliceGrantDecision.FINALIZED,
                "execution completed or already finalized",
                FinalizationStage.FINALIZED,
                verif_att,
                verif_succ,
                last_test_diff_sha,
            )

        # 2. Tool Loop
        failure_kind = getattr(result, "failure_kind", None)
        if outcome_val == "LOOP_DETECTED" or failure_kind == "TOOL_LOOP":
            stage = FinalizationStage.DIRTY_UNVERIFIED if has_mutations else FinalizationStage.CLEAN_UNVERIFIED
            return (
                ExecutionSliceGrantDecision.STOP_FOR_RECOVERY,
                "tool loop detected",
                stage,
                verif_att,
                verif_succ,
                last_test_diff_sha,
            )

        # 3. Tool Failure limit
        category = getattr(result, "category", None)
        if (outcome_val == "NEEDS_ATTENTION" and category == "tool") or failure_kind == "TOOL_FAILURE":
            stage = FinalizationStage.DIRTY_UNVERIFIED if has_mutations else FinalizationStage.CLEAN_UNVERIFIED
            return (
                ExecutionSliceGrantDecision.STOP_FOR_RECOVERY,
                "tool failure limit reached",
                stage,
                verif_att,
                verif_succ,
                last_test_diff_sha,
            )

        # 4. Actual 13th Tool attempted (Hard tool quota triggered inside CodingAgentLoop)
        quota_scope = getattr(result, "quota_scope", None)
        if outcome_val == "QUOTA_EXHAUSTED" and quota_scope == "TOOL":
            stage = FinalizationStage.DIRTY_UNVERIFIED if has_mutations else FinalizationStage.CLEAN_UNVERIFIED
            return (
                ExecutionSliceGrantDecision.REACHED_HARD_CEILING,
                "hard tool call quota exhausted (13th tool attempted)",
                stage,
                verif_att,
                verif_succ,
                last_test_diff_sha,
            )

        # 5. Hard worker ceiling reached
        if (
            worker_calls_used >= self.policy.max_total_worker_calls_per_todo
            or slice_index >= self.policy.max_slices_per_todo
        ):
            stage = FinalizationStage.DIRTY_UNVERIFIED if has_mutations else FinalizationStage.CLEAN_UNVERIFIED
            return (
                ExecutionSliceGrantDecision.REACHED_HARD_CEILING,
                f"hard worker call quota exhausted ({worker_calls_used}/{self.policy.max_total_worker_calls_per_todo})",
                stage,
                verif_att,
                verif_succ,
                last_test_diff_sha,
            )

        # 6. Non-quota execution stop (protocol failure, context overflow, etc.)
        is_worker_exhaustion = (
            (outcome_val == "QUOTA_EXHAUSTED" and quota_scope == "WORKER")
            or (outcome_val == "QUOTA_EXHAUSTED" and getattr(result, "reason", "") == "worker call quota exhausted")
        )
        if not is_worker_exhaustion:
            stage = FinalizationStage.DIRTY_UNVERIFIED if has_mutations else FinalizationStage.CLEAN_UNVERIFIED
            return (
                ExecutionSliceGrantDecision.STOP_FOR_RECOVERY,
                getattr(result, "reason", "non-quota execution stop"),
                stage,
                verif_att,
                verif_succ,
                last_test_diff_sha,
            )

        # 7. Worker soft boundary hit (6 or 12 calls) -> evaluate headroom and stage
        tool_remaining = max(0, self.policy.max_tool_calls_hard - tool_calls_used)

        if has_mutations and is_fresh:
            # Stage: VERIFIED_READY_TO_FINALIZE
            stage = FinalizationStage.VERIFIED_READY_TO_FINALIZE
            return (
                ExecutionSliceGrantDecision.GRANT_NEXT_SLICE,
                "verified ready to finalize; worker grant for FINAL only",
                stage,
                verif_att,
                verif_succ,
                last_test_diff_sha,
            )

        if has_mutations and not is_fresh:
            # Stage: DIRTY_UNVERIFIED
            stage = FinalizationStage.DIRTY_UNVERIFIED
            if tool_remaining >= 1:
                return (
                    ExecutionSliceGrantDecision.GRANT_NEXT_SLICE,
                    f"dirty unverified with tool headroom {tool_remaining} >= 1; granting next slice",
                    stage,
                    verif_att,
                    verif_succ,
                    last_test_diff_sha,
                )
            else:
                return (
                    ExecutionSliceGrantDecision.STOP_FOR_RECOVERY,
                    "dirty unverified but zero tool headroom remaining for verification test",
                    stage,
                    verif_att,
                    verif_succ,
                    last_test_diff_sha,
                )

        # Clean / Exploration only (0 mutations)
        stage = FinalizationStage.CLEAN_UNVERIFIED
        if slice_index == 1:
            return (
                ExecutionSliceGrantDecision.GRANT_NEXT_SLICE,
                "slice 1 exploration extension granted (0 mutations)",
                stage,
                verif_att,
                verif_succ,
                last_test_diff_sha,
            )
        else:
            return (
                ExecutionSliceGrantDecision.STOP_FOR_RECOVERY,
                "exploration limit reached (max 1 extension with 0 mutations)",
                stage,
                verif_att,
                verif_succ,
                last_test_diff_sha,
            )


def total_contract(contract: WorkContract, policy: ProductionExecutionSlicePolicy) -> WorkContract:
    return replace(contract, quota=replace(contract.quota, max_worker_calls=policy.max_total_worker_calls_per_todo))


def slice_contract(contract: WorkContract, policy: ProductionExecutionSlicePolicy, slice_index: int) -> WorkContract:
    total = total_contract(contract, policy)
    return replace(total, quota=replace(total.quota, max_worker_calls=policy.cumulative_ceiling(slice_index)))


def checkpoint_artifact_id(run_id: str, todo_id: str, slice_index: int) -> str:
    _text(run_id, "run_id")
    _text(todo_id, "todo_id")
    _positive(slice_index, "slice_index")
    identity = f"{run_id}\0{todo_id}\0{slice_index}".encode("utf-8")
    return f"production-execution-slice-{hashlib.sha256(identity).hexdigest()}"


def persist_checkpoint(
    artifact_store: ArtifactStore,
    checkpoint: ExecutionSliceCheckpointV1 | ExecutionSliceCheckpointV2,
) -> str:
    artifact_id = checkpoint_artifact_id(checkpoint.run_id, checkpoint.todo_id, checkpoint.slice_index)
    content = checkpoint.canonical_json()
    source_name = f"ExecutionSliceCheckpointV{checkpoint.schema_version}"
    artifact = Artifact.from_text(
        artifact_id=artifact_id,
        kind=ArtifactKind.CONTROL_PLANE_EVIDENCE,
        origin=ArtifactOrigin.CONTROL_PLANE,
        content=content,
        media_type="application/json",
        source=source_name,
        created_at="1970-01-01T00:00:00+00:00",
        scope_tags=(checkpoint.run_id, checkpoint.todo_id),
        metadata={
            "production_role": "EXECUTION_SLICE_CHECKPOINT",
            "fingerprint": checkpoint.fingerprint,
            "slice_index": checkpoint.slice_index,
            "schema_version": checkpoint.schema_version,
        },
    )
    if artifact_store.has_artifact(artifact_id):
        existing = artifact_store.get_artifact(artifact_id)
        if existing.content != content:
            raise RuntimeError("execution slice checkpoint identity conflict")
        return artifact_id
    artifact_store.create_artifact(artifact)
    return artifact_id


def latest_checkpoint(
    artifact_store: ArtifactStore | None,
    *,
    run_id: str,
    todo_id: str,
    policy: ProductionExecutionSlicePolicy,
) -> ExecutionSliceCheckpointV1 | ExecutionSliceCheckpointV2 | None:
    """Load the highest durable slice boundary supporting both V1 and V2 checkpoints."""
    if artifact_store is None:
        return None
    try:
        present = [
            index
            for index in range(1, policy.max_slices_per_todo + 1)
            if artifact_store.has_artifact(checkpoint_artifact_id(run_id, todo_id, index))
        ]
        if not present:
            return None
        index = max(present)
        artifact_id = checkpoint_artifact_id(run_id, todo_id, index)
        artifact = artifact_store.get_artifact(artifact_id)
        raw = json.loads(artifact.content)
        if not isinstance(raw, dict):
            return None

        version = raw.get("schema_version")
        if version == 1:
            expected = {
                "schema_version",
                "run_id",
                "todo_id",
                "slice_index",
                "max_slices",
                "worker_calls_before",
                "worker_calls_after",
                "slice_worker_calls",
                "tool_calls_total",
                "changed_paths",
                "last_model_id",
                "context_bundle_id",
                "status",
                "reason",
            }
            if set(raw) != expected or not isinstance(raw["changed_paths"], list):
                return None
            checkpoint_v1 = ExecutionSliceCheckpointV1(
                schema_version=1,
                run_id=raw["run_id"],
                todo_id=raw["todo_id"],
                slice_index=raw["slice_index"],
                max_slices=raw["max_slices"],
                worker_calls_before=raw["worker_calls_before"],
                worker_calls_after=raw["worker_calls_after"],
                slice_worker_calls=raw["slice_worker_calls"],
                tool_calls_total=raw["tool_calls_total"],
                changed_paths=tuple(raw["changed_paths"]),
                last_model_id=raw["last_model_id"],
                context_bundle_id=raw["context_bundle_id"],
                status=raw["status"],
                reason=raw["reason"],
            )
            if (
                checkpoint_v1.run_id != run_id
                or checkpoint_v1.todo_id != todo_id
                or checkpoint_v1.slice_index != index
                or checkpoint_v1.max_slices != policy.max_slices_per_todo
                or checkpoint_v1.canonical_json() != artifact.content
            ):
                return None
            return checkpoint_v1

        elif version == 2:
            checkpoint_v2 = ExecutionSliceCheckpointV2(
                schema_version=2,
                run_id=raw["run_id"],
                todo_id=raw["todo_id"],
                slice_index=raw["slice_index"],
                max_slices=raw["max_slices"],
                worker_calls_used_total=raw["worker_calls_used_total"],
                tool_calls_used_total=raw["tool_calls_used_total"],
                worker_soft_ceiling=raw["worker_soft_ceiling"],
                worker_hard_ceiling=raw["worker_hard_ceiling"],
                tool_hard_ceiling=raw["tool_hard_ceiling"],
                changed_paths=tuple(raw.get("changed_paths", [])),
                dirty_paths=tuple(raw.get("dirty_paths", [])),
                diff_sha256=raw.get("diff_sha256"),
                verification_attempted=bool(raw.get("verification_attempted", False)),
                verification_succeeded=bool(raw.get("verification_succeeded", False)),
                verification_diff_sha256=raw.get("verification_diff_sha256"),
                finalized=bool(raw.get("finalized", False)),
                grant_decision=raw["grant_decision"],
                grant_reason=raw["grant_reason"],
                last_model_id=raw.get("last_model_id"),
                context_bundle_id=raw["context_bundle_id"],
                status=raw.get("status", "CONTINUE"),
                reason=raw.get("reason", raw["grant_reason"]),
                exploration_extension_used=bool(raw.get("exploration_extension_used", False)),
            )
            if (
                checkpoint_v2.run_id != run_id
                or checkpoint_v2.todo_id != todo_id
                or checkpoint_v2.slice_index != index
                or checkpoint_v2.max_slices != policy.max_slices_per_todo
                or checkpoint_v2.canonical_json() != artifact.content
            ):
                return None
            return checkpoint_v2

        return None
    except Exception:
        return None


def restart_continuation_authorized(artifact_store, *, run, events, policy: ProductionExecutionSlicePolicy) -> bool:
    """Authorize a restart only at the exact latest worker-quota boundary."""
    try:
        todo_id = run.active_todo_id
        if not isinstance(todo_id, str) or not todo_id:
            return False
        checkpoint = latest_checkpoint(
            artifact_store,
            run_id=run.run_id,
            todo_id=todo_id,
            policy=policy,
        )
        if checkpoint is None or checkpoint.status != "CONTINUE":
            return False

        worker_after = (
            checkpoint.worker_calls_used_total
            if isinstance(checkpoint, ExecutionSliceCheckpointV2)
            else checkpoint.worker_calls_after
        )
        if worker_after != run.quota_usage.worker_calls:
            return False
        if worker_after != policy.cumulative_ceiling(checkpoint.slice_index):
            return False
        attention = [
            event.to_dict()["payload"]
            for event in events
            if event.event_type.value == "ATTENTION_RECORDED"
        ]
        if not attention:
            return False
        latest = attention[-1]
        return (
            latest.get("todo_id") == todo_id
            and latest.get("run_id") == run.run_id
            and latest.get("category") == "quota"
            and latest.get("reason") == "worker call quota exhausted"
        )
    except Exception:
        return False


def slice_aware_agent(
    agent: ControlPlaneCodingAgent,
    policy: ProductionExecutionSlicePolicy,
    *,
    candidate_lease_provider=None,
    tool_failure_request_provider=None,
) -> ControlPlaneCodingAgent:
    """Clone loop composition with a total 18-call integration clamp."""
    loop = agent.loop
    policies = dict(loop.policies)
    retry = dict(policies.get("retry_and_anti_loop", loop.retry_policy))
    retry["max_total_worker_calls_per_task"] = policy.max_total_worker_calls_per_todo
    policies["retry_and_anti_loop"] = retry
    return ControlPlaneCodingAgent(
        context_resolver=agent.context_resolver,
        workspace_resolver=agent.workspace_resolver,
        loop=ProductionWindowedCodingAgentLoop(
            run_store=loop.run_store,
            artifact_store=loop.artifact_store,
            router=loop.router,
            executor=ProgressBudgetExecutor(
                loop.executor,
                run_store=loop.run_store,
                artifact_store=loop.artifact_store,
                policy=policy,
            ),
            snapshot_provider=loop.snapshot_provider,
            dispatcher=ProductionWorkspacePathContractDispatcher(
                ProductionSearchTextContractDispatcher(
                    ProductionAciFeedbackDispatcher(
                        loop.dispatcher,
                        run_store=loop.run_store,
                        artifact_store=loop.artifact_store,
                    ),
                ),
            ),
            policies=policies,
            profile_factory=loop.profile_factory,
            observer=loop.observer,
            candidate_lease_provider=candidate_lease_provider,
            tool_failure_request_provider=tool_failure_request_provider,
        ),
    )
