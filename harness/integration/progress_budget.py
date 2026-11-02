"""Durable read-only progress observation windows for production slices."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from typing import Any, TYPE_CHECKING

from ..context_manager import ArtifactKind, ArtifactOrigin
from ..context_manager.integration import ControlPlaneArtifactBridge
from ..control_plane import EvidenceStatus
from ..executor.models import ExecutionMessage

if TYPE_CHECKING:
    from .execution_slices import ProductionExecutionSlicePolicy


PROGRESS_BUDGET_V1 = """PROGRESS_BUDGET_V1
Bounded execution progress notice.
You have already inspected enough repository state to make progress.
Do not repeat identical reads or reassurance checks.
If the available evidence is sufficient, mutate the workspace now.
If one critical fact is still missing, perform one targeted read for that fact and then act.
Do not invent changes without evidence."""

FINALIZATION_HEADROOM_HINT_V1 = """FINALIZATION_HEADROOM_V1
Workspace mutations are confirmed. Worker call headroom for this slice is running low.
Prioritize running deterministic test verification and submitting FINAL over open-ended exploration."""

FINAL_ONLY_HINT_V1 = """FINAL_ONLY_HINT_V1
Verification testing is complete and successful for current workspace state.
Tool budget is minimal or exhausted. Submit FINAL summary now without requesting additional tools."""


@dataclass(frozen=True, slots=True)
class ProgressWindowBaselineV1:
    schema_version: int
    run_id: str
    todo_id: str
    request_id: str
    slice_index: int
    baseline_event_sequence: int

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("schema_version must be 1")

        for name in ("run_id", "todo_id", "request_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be non-empty text")

        if type(self.slice_index) is not int or self.slice_index <= 0:
            raise ValueError("slice_index must be a positive integer")

        if (
            type(self.baseline_event_sequence) is not int
            or self.baseline_event_sequence < -1
        ):
            raise ValueError(
                "baseline_event_sequence must be an integer >= -1"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "todo_id": self.todo_id,
            "request_id": self.request_id,
            "slice_index": self.slice_index,
            "baseline_event_sequence": self.baseline_event_sequence,
        }

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(
            json.dumps(
                self.to_dict(),
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()


class ProgressBudgetExecutor:
    """Adds a message to an existing request; it never invokes a second model call."""

    def __init__(
        self,
        inner: Any,
        *,
        run_store,
        artifact_store,
        policy: ProductionExecutionSlicePolicy,
    ) -> None:
        self.inner = inner
        self.run_store = run_store
        self.artifact_store = artifact_store
        self.policy = policy
        self._bridge = ControlPlaneArtifactBridge(
            run_store,
            artifact_store,
        )

    @staticmethod
    def _identity(
        run_id: str,
        todo_id: str,
        request_id: str,
    ) -> str:
        return hashlib.sha256(
            f"{run_id}\0{todo_id}\0{request_id}".encode("utf-8")
        ).hexdigest()[:32]

    def _baseline(
        self,
        run_id: str,
        todo_id: str,
        request_id: str,
        slice_index: int,
    ) -> ProgressWindowBaselineV1:
        evidence_id = (
            f"{run_id}:progress-window:"
            f"{self._identity(run_id, todo_id, request_id)}"
        )

        for event in self.run_store.list_events(run_id):
            if event.event_type.value != "EVIDENCE_RECORDED":
                continue

            payload = event.to_dict()["payload"]

            if payload.get("evidence_id") != evidence_id:
                continue

            data = payload.get("payload")

            if not isinstance(data, dict):
                raise RuntimeError(
                    "malformed durable ProgressWindowBaselineV1"
                )

            try:
                baseline = ProgressWindowBaselineV1(**data)
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    "malformed durable ProgressWindowBaselineV1"
                ) from exc

            if (
                baseline.run_id != run_id
                or baseline.todo_id != todo_id
                or baseline.request_id != request_id
                or baseline.slice_index != slice_index
            ):
                raise RuntimeError(
                    "ProgressWindowBaselineV1 identity mismatch"
                )

            return baseline

        events = self.run_store.list_events(run_id)

        baseline = ProgressWindowBaselineV1(
            schema_version=1,
            run_id=run_id,
            todo_id=todo_id,
            request_id=request_id,
            slice_index=slice_index,
            baseline_event_sequence=(
                events[-1].sequence if events else -1
            ),
        )

        self._bridge.record_text_evidence(
            run_id,
            artifact_id=(
                f"{run_id}:progress-window:{baseline.fingerprint}"
            ),
            todo_id=todo_id,
            evidence_id=evidence_id,
            evidence_type="PROGRESS_WINDOW_BASELINE",
            source="ProgressWindowBaselineV1",
            content=json.dumps(
                baseline.to_dict(),
                sort_keys=True,
            ),
            kind=ArtifactKind.CONTROL_PLANE_EVIDENCE,
            origin=ArtifactOrigin.CONTROL_PLANE,
            media_type="application/json",
            status=EvidenceStatus.PASSED,
            evidence_payload=baseline.to_dict(),
            created_at="1970-01-01T00:00:00+00:00",
        )

        return baseline

    def _slice_state(
        self,
        run_id: str,
        todo_id: str,
        baseline: ProgressWindowBaselineV1,
    ):
        rows = []
        emitted = False

        for event in self.run_store.list_events(run_id):
            if event.event_type.value != "EVIDENCE_RECORDED":
                continue

            payload = event.to_dict()["payload"]

            if payload.get("todo_id") != todo_id:
                continue

            data = payload.get("payload", {})

            # ProgressBudget emission is bounded by cumulative execution
            # slice, not by the current workflow request/profile window.
            # Therefore search the entire durable Run history.
            if (
                payload.get("evidence_type")
                == "PROGRESS_BUDGET_EMITTED"
                and payload.get("status")
                == EvidenceStatus.PASSED.value
                and isinstance(data, dict)
                and data.get("slice_index")
                == baseline.slice_index
            ):
                emitted = True

            # Tool observations, unlike emission identity, belong only
            # to this workflow request's durable observation window.
            if event.sequence <= baseline.baseline_event_sequence:
                continue

            if (
                payload.get("evidence_type") == "TOOL_RESULT"
                and payload.get("status")
                == EvidenceStatus.PASSED.value
                and isinstance(data, dict)
                and data.get("tool_status") == "SUCCESS"
            ):
                rows.append(data)

        changed = tuple(
            sorted(
                {
                    path
                    for row in rows
                    for path in row.get("changed_paths", [])
                    if isinstance(path, str)
                }
            )
        )

        reads = tuple(
            sorted(
                {
                    row["arguments"]["path"]
                    for row in rows
                    if row.get("tool") == "READ_TEXT"
                    and isinstance(row.get("arguments"), dict)
                    and isinstance(
                        row["arguments"].get("path"),
                        str,
                    )
                }
            )
        )

        non_mutating = sum(
            1
            for row in rows
            if not row.get("changed_paths")
        )

        return emitted, non_mutating, reads, changed

    def _emit(
        self,
        run_id,
        todo_id,
        index,
        used,
        non_mutating,
        reads,
        changed,
    ) -> None:
        remaining = (
            self.policy.cumulative_ceiling(index) - used
        )

        payload = {
            "run_id": run_id,
            "todo_id": todo_id,
            "slice_index": index,
            "worker_calls_used": used,
            "worker_calls_remaining": remaining,
            "non_mutating_tool_calls": non_mutating,
            "successfully_read_paths": list(reads),
            "changed_paths": list(changed),
        }

        payload["fingerprint"] = hashlib.sha256(
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

        self._bridge.record_text_evidence(
            run_id,
            artifact_id=f"{run_id}:progress-budget:{index}",
            todo_id=todo_id,
            evidence_id=f"{run_id}:progress-budget:{index}",
            evidence_type="PROGRESS_BUDGET_EMITTED",
            source="ProgressBudgetV1",
            content=json.dumps(
                payload,
                sort_keys=True,
            ),
            kind=ArtifactKind.CONTROL_PLANE_EVIDENCE,
            origin=ArtifactOrigin.CONTROL_PLANE,
            media_type="application/json",
            status=EvidenceStatus.PASSED,
            evidence_payload=payload,
            created_at="1970-01-01T00:00:00+00:00",
        )

    def _candidate_delta_message(self, run_id: str, todo_id: str) -> str | None:
        """Present one durable, bounded UNVERIFIED candidate handoff to the next activation."""
        summaries = []
        presented = set()
        for event in self.run_store.list_events(run_id):
            if event.event_type.value != "EVIDENCE_RECORDED":
                continue
            envelope = event.to_dict()["payload"]
            if envelope.get("todo_id") != todo_id:
                continue
            payload = envelope.get("payload")
            if envelope.get("evidence_type") == "CANDIDATE_DELTA_SUMMARY":
                if not isinstance(payload, dict) or payload.get("verification_state") != "UNVERIFIED":
                    raise RuntimeError("malformed durable CandidateDeltaSummaryV1")
                summaries.append(payload)
            elif envelope.get("evidence_type") == "CANDIDATE_DELTA_SUMMARY_PRESENTED" and isinstance(payload, dict):
                fingerprint = payload.get("summary_fingerprint")
                if isinstance(fingerprint, str):
                    presented.add(fingerprint)
        for summary in reversed(summaries):
            fingerprint = summary.get("fingerprint")
            paths = summary.get("changed_paths")
            if not isinstance(fingerprint, str) or not isinstance(paths, list) or fingerprint in presented:
                continue
            shown = ", ".join(path for path in paths[:12] if isinstance(path, str))
            message = (
                "CANDIDATE_DELTA_SUMMARY_V1\\n"
                f"Existing UNVERIFIED candidate changes: {shown or '(none listed)'}.\\n"
                f"Base commit: {summary.get('base_commit')}; diff: {summary.get('diff_sha256')}.\\n"
                f"Previous recoverable failure: {summary.get('previous_failure_kind')}. "
                "Continue from this candidate; do not blindly reimplement already-applied work. "
                "Inspect a modified file only for a specific unresolved fact. Only configured deterministic verification may accept the Todo."
            )
            proof = {"schema_version": 1, "summary_fingerprint": fingerprint}
            self._bridge.record_text_evidence(
                run_id, artifact_id=f"{run_id}:candidate-delta-presented:{fingerprint}",
                todo_id=todo_id, evidence_id=f"{run_id}:candidate-delta-presented:{fingerprint}",
                evidence_type="CANDIDATE_DELTA_SUMMARY_PRESENTED", source="CandidateDeltaSummaryV1",
                content=json.dumps(proof, sort_keys=True), kind=ArtifactKind.CONTROL_PLANE_EVIDENCE,
                origin=ArtifactOrigin.CONTROL_PLANE, media_type="application/json",
                status=EvidenceStatus.PASSED, evidence_payload=proof,
                created_at="1970-01-01T00:00:00+00:00",
            )
            return message[:1536]
        return None

    def execute(self, request):
        request_id = request.request_id
        todo_id = request.task_id

        if (
            not isinstance(request_id, str)
            or not isinstance(todo_id, str)
            or ":phase16:" not in request_id
        ):
            return self.inner.execute(request)

        run_id = request_id.split(":phase16:", 1)[0]

        candidate_delta = self._candidate_delta_message(run_id, todo_id)
        if candidate_delta is not None:
            request = replace(request, messages=(*request.messages, ExecutionMessage("user", candidate_delta)))

        try:
            index = int(
                request_id.rsplit(":slice:", 1)[1]
            )
        except (ValueError, IndexError):
            return self.inner.execute(request)

        baseline = self._baseline(
            run_id,
            todo_id,
            request_id,
            index,
        )

        emitted, non_mutating, reads, changed = (
            self._slice_state(
                run_id,
                todo_id,
                baseline,
            )
        )

        used = (
            self.run_store
            .get_run(run_id)
            .quota_usage
            .worker_calls
        )

        remaining = (
            self.policy.cumulative_ceiling(index)
            - used
        )

        should_emit = (
            not emitted
            and not changed
            and (
                non_mutating >= 3
                or remaining <= 2
            )
        )

        if should_emit:
            self._emit(
                run_id,
                todo_id,
                index,
                used,
                non_mutating,
                reads,
                changed,
            )

            request = replace(
                request,
                messages=(
                    *request.messages,
                    ExecutionMessage(
                        "user",
                        PROGRESS_BUDGET_V1,
                    ),
                ),
            )

        return self.inner.execute(request)