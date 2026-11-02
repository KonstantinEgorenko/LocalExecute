"""Additive bridge from Phase 22 human requests to existing Swarm control state.

The bridge does not add or modify frozen Phase 15/16/17 enums.  A pending
``HumanRequestV1`` is the typed durable authority; existing Control Plane
``RunStatus.NEEDS_ATTENTION`` and ``AttentionItem`` records are projections for
legacy/runtime visibility.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Callable, Mapping

from ..control_plane.models import AttentionItem, AttentionSeverity, AttentionStatus, Run, RunStatus
from .classifier import HumanBlockerObservationV1
from .models import (
    CapabilityGateDecisionV1,
    HumanRequestKindV1,
    HumanRequestStatusV1,
    HumanRequestV1,
    stable_sha256,
)
from .policy import HumanEscalationPolicyV1
from .store import HumanRequestNotFoundError, JsonHumanRequestStore


class HumanEscalationBridgeError(RuntimeError):
    pass


class HumanWaitProjectionV1(StrEnum):
    READY = "READY"
    WAITING_FOR_INPUT = "WAITING_FOR_INPUT"
    WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
    WAITING_FOR_CAPABILITY = "WAITING_FOR_CAPABILITY"
    WAITING_FOR_RESOURCE = "WAITING_FOR_RESOURCE"
    DENIED = "DENIED"


@dataclass(frozen=True, slots=True)
class HumanEscalationProjectionV1:
    mission_id: str
    state: HumanWaitProjectionV1
    decision: CapabilityGateDecisionV1
    reason: str
    request_id: str | None = None
    run_id: str | None = None
    run_resumed: bool = False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _wait_state(kind: HumanRequestKindV1) -> HumanWaitProjectionV1:
    return {
        HumanRequestKindV1.MISSING_INFORMATION: HumanWaitProjectionV1.WAITING_FOR_INPUT,
        HumanRequestKindV1.APPROVAL_REQUIRED: HumanWaitProjectionV1.WAITING_FOR_APPROVAL,
        HumanRequestKindV1.CAPABILITY_REQUIRED: HumanWaitProjectionV1.WAITING_FOR_CAPABILITY,
        HumanRequestKindV1.RESOURCE_REQUIRED: HumanWaitProjectionV1.WAITING_FOR_RESOURCE,
    }[kind]


def request_authorizes_resume(request: HumanRequestV1) -> bool:
    if request.kind is HumanRequestKindV1.MISSING_INFORMATION:
        return request.status is HumanRequestStatusV1.ANSWERED
    return request.status is HumanRequestStatusV1.APPROVED


class HumanEscalationBridgeV1:
    """Materialize asks, project waiting state, and authorize deterministic resume."""

    def __init__(
        self,
        *,
        request_store: JsonHumanRequestStore,
        run_store: Any | None = None,
        clock: Callable[[], str] = _now,
    ) -> None:
        self.request_store = request_store
        self.run_store = run_store
        self.clock = clock

    @staticmethod
    def _request_id(observation: HumanBlockerObservationV1, kind: HumanRequestKindV1) -> str:
        digest = stable_sha256(
            {
                "mission_id": observation.mission_id,
                "goal_id": observation.goal_id,
                "todo_id": observation.todo_id,
                "run_id": observation.run_id,
                "campaign_id": observation.campaign_id,
                "experiment_id": observation.experiment_id,
                "kind": kind.value,
                "question": observation.question,
                "required_fields": observation.required_fields,
                "blocked_action": observation.blocked_action,
                "capability": observation.capability,
                "resource": observation.resource,
            }
        )
        return f"hr-{digest[:24]}"

    def _build_request(
        self,
        observation: HumanBlockerObservationV1,
        kind: HumanRequestKindV1,
    ) -> HumanRequestV1:
        return HumanRequestV1(
            request_id=self._request_id(observation, kind),
            mission_id=observation.mission_id,
            goal_id=observation.goal_id,
            todo_id=observation.todo_id,
            run_id=observation.run_id,
            campaign_id=observation.campaign_id,
            experiment_id=observation.experiment_id,
            kind=kind,
            question=observation.question,
            required_fields=observation.required_fields,
            blocked_action=observation.blocked_action,
            capability=observation.capability,
            resource=observation.resource,
            attempted_autonomous_acquisition=observation.attempted_autonomous_acquisition,
            risk_summary=observation.risk_summary,
            created_at=self.clock(),
        )

    @staticmethod
    def _event_exists(run_store: Any, run_id: str, entity_id: str) -> bool:
        try:
            events = run_store.list_events(run_id)
        except Exception:
            return False
        return any(getattr(event, "entity_id", None) == entity_id for event in events)

    def _project_pending_run(self, request: HumanRequestV1) -> None:
        if self.run_store is None or request.run_id is None:
            return
        run = self.run_store.get_run(request.run_id)
        if request.goal_id is not None and run.goal_id != request.goal_id:
            raise HumanEscalationBridgeError("human request goal_id does not match Run")
        if request.todo_id is not None and run.active_todo_id != request.todo_id:
            raise HumanEscalationBridgeError("human request todo_id does not match active Run Todo")
        if run.status in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}:
            raise HumanEscalationBridgeError(f"cannot project pending human request onto terminal Run: {run.status.value}")
        if run.status is not RunStatus.NEEDS_ATTENTION:
            self.run_store.update_run(Run(run.run_id, run.goal_id, run.active_todo_id, RunStatus.NEEDS_ATTENTION, run.quota_usage))

        attention_id = f"{run.run_id}:human-request:{request.request_id}"
        if not self._event_exists(self.run_store, run.run_id, attention_id):
            self.run_store.record_attention(
                run.run_id,
                AttentionItem(
                    attention_id,
                    f"HumanRequest {request.request_id}: {request.question}",
                    AttentionSeverity.HIGH,
                    AttentionStatus.OPEN,
                    todo_id=request.todo_id,
                    run_id=run.run_id,
                    category=f"human_request:{request.kind.value}",
                ),
            )

    def evaluate_and_apply(
        self,
        observation: HumanBlockerObservationV1,
        policy: HumanEscalationPolicyV1,
    ) -> HumanEscalationProjectionV1:
        gate = policy.evaluate(observation)
        if gate.decision is CapabilityGateDecisionV1.ALLOW:
            return HumanEscalationProjectionV1(
                observation.mission_id,
                HumanWaitProjectionV1.READY,
                gate.decision,
                gate.reason,
                run_id=observation.run_id,
            )
        if gate.decision is CapabilityGateDecisionV1.DENY:
            return HumanEscalationProjectionV1(
                observation.mission_id,
                HumanWaitProjectionV1.DENIED,
                gate.decision,
                gate.reason,
                run_id=observation.run_id,
            )

        assert gate.request_kind is not None
        request = self.request_store.create(self._build_request(observation, gate.request_kind))
        self._project_pending_run(request)
        return HumanEscalationProjectionV1(
            observation.mission_id,
            _wait_state(request.kind),
            CapabilityGateDecisionV1.ASK,
            gate.reason,
            request.request_id,
            request.run_id,
        )

    @staticmethod
    def _validate_resolution(
        request: HumanRequestV1,
        status: HumanRequestStatusV1,
        payload: Mapping[str, Any] | None,
    ) -> None:
        resolution = {} if payload is None else dict(payload)
        if status is HumanRequestStatusV1.ANSWERED:
            if request.kind is not HumanRequestKindV1.MISSING_INFORMATION:
                raise HumanEscalationBridgeError("ANSWERED only resolves MISSING_INFORMATION")
            missing = [field for field in request.required_fields if field not in resolution or resolution[field] is None]
            if missing:
                raise HumanEscalationBridgeError(f"resolution missing required fields: {', '.join(missing)}")
        if status is HumanRequestStatusV1.APPROVED and request.kind is HumanRequestKindV1.MISSING_INFORMATION:
            raise HumanEscalationBridgeError("MISSING_INFORMATION requires ANSWERED, not APPROVED")
        if status is HumanRequestStatusV1.UNAVAILABLE and request.kind not in {
            HumanRequestKindV1.CAPABILITY_REQUIRED,
            HumanRequestKindV1.RESOURCE_REQUIRED,
        }:
            raise HumanEscalationBridgeError("UNAVAILABLE only resolves capability/resource request")

    def _record_resolution_attention(self, request: HumanRequestV1) -> None:
        if self.run_store is None or request.run_id is None:
            return
        attention_id = f"{request.run_id}:human-resolution:{request.request_id}"
        if self._event_exists(self.run_store, request.run_id, attention_id):
            return
        self.run_store.record_attention(
            request.run_id,
            AttentionItem(
                attention_id,
                f"HumanRequest {request.request_id} resolved as {request.status.value}",
                AttentionSeverity.MEDIUM,
                AttentionStatus.RESOLVED,
                todo_id=request.todo_id,
                run_id=request.run_id,
                category=f"human_request_resolution:{request.kind.value}",
            ),
        )

    def _resume_run_if_possible(self, request: HumanRequestV1) -> bool:
        if not request_authorizes_resume(request) or self.run_store is None or request.run_id is None:
            return False
        if self.request_store.list_pending(request.mission_id):
            return False
        run = self.run_store.get_run(request.run_id)
        if run.status is RunStatus.RUNNING:
            return False
        if run.status is not RunStatus.NEEDS_ATTENTION:
            raise HumanEscalationBridgeError(f"resolved human request cannot resume Run from {run.status.value}")
        self.run_store.update_run(Run(run.run_id, run.goal_id, run.active_todo_id, RunStatus.RUNNING, run.quota_usage))
        return True

    def resolve(
        self,
        mission_id: str,
        request_id: str,
        *,
        status: HumanRequestStatusV1,
        resolution_payload: Mapping[str, Any] | None = None,
        resolved_at: str | None = None,
    ) -> HumanEscalationProjectionV1:
        current = self.request_store.get(mission_id, request_id)
        self._validate_resolution(current, status, resolution_payload)
        resolved = self.request_store.resolve(
            mission_id,
            request_id,
            status=status,
            resolved_at=resolved_at or self.clock(),
            resolution_payload=resolution_payload,
        )
        self._record_resolution_attention(resolved)
        resumed = self._resume_run_if_possible(resolved)

        if not request_authorizes_resume(resolved):
            return HumanEscalationProjectionV1(
                mission_id,
                HumanWaitProjectionV1.DENIED,
                CapabilityGateDecisionV1.DENY,
                f"human request resolved as {resolved.status.value}",
                request_id,
                resolved.run_id,
                False,
            )

        pending = self.request_store.list_pending(mission_id)
        if pending:
            next_request = pending[0]
            return HumanEscalationProjectionV1(
                mission_id,
                _wait_state(next_request.kind),
                CapabilityGateDecisionV1.ASK,
                "another human request remains pending",
                next_request.request_id,
                next_request.run_id,
                False,
            )
        return HumanEscalationProjectionV1(
            mission_id,
            HumanWaitProjectionV1.READY,
            CapabilityGateDecisionV1.ALLOW,
            "human request resolved; durable execution may resume",
            request_id,
            resolved.run_id,
            resumed,
        )

    def resume_gate(self, mission_id: str) -> HumanEscalationProjectionV1:
        """Project durable request state before re-entering the same mission.

        A new bridge instance can call this after process restart.  Pending
        requests block re-entry; negative terminal resolutions stay fail-closed;
        only a ledger containing exclusively resume-authorizing terminal requests
        projects ``READY``.
        """

        try:
            requests = self.request_store.list_requests(mission_id)
        except HumanRequestNotFoundError:
            return HumanEscalationProjectionV1(
                mission_id,
                HumanWaitProjectionV1.READY,
                CapabilityGateDecisionV1.ALLOW,
                "no human request ledger for mission",
            )
        pending = tuple(item for item in requests if not item.is_terminal)
        if pending:
            item = pending[0]
            return HumanEscalationProjectionV1(
                mission_id,
                _wait_state(item.kind),
                CapabilityGateDecisionV1.ASK,
                "human request remains pending",
                item.request_id,
                item.run_id,
            )
        denied = tuple(item for item in requests if not request_authorizes_resume(item))
        if denied:
            item = denied[-1]
            return HumanEscalationProjectionV1(
                mission_id,
                HumanWaitProjectionV1.DENIED,
                CapabilityGateDecisionV1.DENY,
                f"human request terminal resolution blocks resume: {item.status.value}",
                item.request_id,
                item.run_id,
            )
        return HumanEscalationProjectionV1(
            mission_id,
            HumanWaitProjectionV1.READY,
            CapabilityGateDecisionV1.ALLOW,
            "all human requests authorize resume",
        )
