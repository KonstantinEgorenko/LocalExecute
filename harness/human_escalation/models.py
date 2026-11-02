'research campaign.R0 immutable human-escalation domain contracts.\n\nThis module deliberately does not modify recovery/16/17 status enums.  A\n``HumanRequestV1`` is a durable, typed projection describing *why* execution is\nwaiting for an owner response.  Existing ``NEEDS_ATTENTION`` remains the\ncontrol-plane stop state until the research campaign bridge is added.\n'
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class HumanRequestKindV1(StrEnum):
    """Reason autonomous execution cannot continue without owner input."""

    MISSING_INFORMATION = "MISSING_INFORMATION"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    CAPABILITY_REQUIRED = "CAPABILITY_REQUIRED"
    RESOURCE_REQUIRED = "RESOURCE_REQUIRED"


class HumanRequestStatusV1(StrEnum):
    """Lifecycle of a durable human request."""

    PENDING = "PENDING"
    ANSWERED = "ANSWERED"
    APPROVED = "APPROVED"
    DENIED = "DENIED"
    UNAVAILABLE = "UNAVAILABLE"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


class CapabilityGateDecisionV1(StrEnum):
    """Out-of-band policy decision for a requested action/capability."""

    ALLOW = "ALLOW"
    ASK = "ASK"
    DENY = "DENY"


def _text(value: object, name: str, *, max_length: int | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty string")
    if max_length is not None and len(value) > max_length:
        raise ValueError(f"{name} exceeds max_length={max_length}")
    return value


def _optional_text(value: object, name: str, *, max_length: int | None = None) -> str | None:
    if value is None:
        return None
    return _text(value, name, max_length=max_length)


def _safe_id(value: object, name: str) -> str:
    value = _text(value, name)
    if not _ID_RE.fullmatch(value):
        raise ValueError(f"{name} must match {_ID_RE.pattern}")
    return value


def _optional_safe_id(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _safe_id(value, name)


def _unique_text(values: object, name: str, *, maximum: int | None = None) -> tuple[str, ...]:
    if not isinstance(values, (tuple, list)):
        raise ValueError(f"{name} must be list/tuple")
    result = tuple(_text(value, name, max_length=4_000) for value in values)
    if len(set(result)) != len(result):
        raise ValueError(f"{name} must not contain duplicates")
    if maximum is not None and len(result) > maximum:
        raise ValueError(f"{name} exceeds maximum={maximum}")
    return result


def _freeze_json(value: Any, name: str = "value") -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{name} must not contain non-finite float")
        return value
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{name} mapping keys must be strings")
            frozen[key] = _freeze_json(item, f"{name}.{key}")
        return MappingProxyType(frozen)
    if isinstance(value, (tuple, list)):
        return tuple(_freeze_json(item, name) for item in value)
    raise ValueError(f"{name} must be JSON-safe")


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    if isinstance(value, StrEnum):
        return value.value
    return value


def _timestamp(value: object, name: str) -> str:
    raw = _text(value, name, max_length=128)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must include timezone")
    return raw


def _optional_timestamp(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _timestamp(value, name)


def _timestamp_value(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def canonical_json(value: object) -> str:
    return json.dumps(_plain(value), sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def stable_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class CapabilityGateResultV1:
    """Deterministic policy result before any human request is materialized.

    ``ASK`` means a durable ``HumanRequestV1`` is allowed/required.  ``DENY`` is
    terminal policy authority and MUST NOT be converted into an approval request
    by a bridge or model.  ``ALLOW`` permits execution without owner input.
    """

    decision: CapabilityGateDecisionV1
    reason: str
    request_kind: HumanRequestKindV1 | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.decision, CapabilityGateDecisionV1):
            raise ValueError("decision must be CapabilityGateDecisionV1")
        _text(self.reason, "reason", max_length=16_000)
        if self.request_kind is not None and not isinstance(self.request_kind, HumanRequestKindV1):
            raise ValueError("request_kind must be HumanRequestKindV1")
        if self.decision is CapabilityGateDecisionV1.ASK and self.request_kind is None:
            raise ValueError("ASK requires request_kind")
        if self.decision is not CapabilityGateDecisionV1.ASK and self.request_kind is not None:
            raise ValueError("request_kind is only valid for ASK")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "decision": self.decision.value,
            "reason": self.reason,
            "request_kind": None if self.request_kind is None else self.request_kind.value,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CapabilityGateResultV1":
        if raw.get("schema_version") != 1:
            raise ValueError("CapabilityGateResultV1 schema_version must be 1")
        request_kind = raw.get("request_kind")
        return cls(
            CapabilityGateDecisionV1(str(raw["decision"])),
            str(raw["reason"]),
            None if request_kind is None else HumanRequestKindV1(str(request_kind)),
        )


@dataclass(frozen=True, slots=True)
class HumanRequestV1:
    """Durable typed request for information, approval, capability or resource.

    The model is deliberately self-contained so a future store can reconstruct
    pending requests after process restart without relying on model context.
    Resolution is represented by creating the next immutable version of the same
    ``request_id`` with a terminal status and resolution payload.
    """

    request_id: str
    mission_id: str
    kind: HumanRequestKindV1
    question: str
    created_at: str
    goal_id: str | None = None
    todo_id: str | None = None
    run_id: str | None = None
    campaign_id: str | None = None
    experiment_id: str | None = None
    required_fields: tuple[str, ...] = ()
    blocked_action: str | None = None
    capability: str | None = None
    resource: str | None = None
    attempted_autonomous_acquisition: tuple[str, ...] = ()
    risk_summary: str | None = None
    expires_at: str | None = None
    status: HumanRequestStatusV1 = HumanRequestStatusV1.PENDING
    resolution_payload: Mapping[str, Any] = field(default_factory=dict)
    resolved_at: str | None = None

    def __post_init__(self) -> None:
        _safe_id(self.request_id, "request_id")
        _safe_id(self.mission_id, "mission_id")
        if not isinstance(self.kind, HumanRequestKindV1):
            raise ValueError("kind must be HumanRequestKindV1")
        _text(self.question, "question", max_length=32_000)
        created_at = _timestamp(self.created_at, "created_at")
        object.__setattr__(self, "created_at", created_at)

        for name in ("goal_id", "todo_id", "run_id", "campaign_id", "experiment_id"):
            value = _optional_safe_id(getattr(self, name), name)
            object.__setattr__(self, name, value)

        required_fields = _unique_text(self.required_fields, "required_fields", maximum=64)
        attempted = _unique_text(
            self.attempted_autonomous_acquisition,
            "attempted_autonomous_acquisition",
            maximum=64,
        )
        object.__setattr__(self, "required_fields", required_fields)
        object.__setattr__(self, "attempted_autonomous_acquisition", attempted)

        blocked_action = _optional_text(self.blocked_action, "blocked_action", max_length=16_000)
        capability = _optional_text(self.capability, "capability", max_length=4_000)
        resource = _optional_text(self.resource, "resource", max_length=4_000)
        risk_summary = _optional_text(self.risk_summary, "risk_summary", max_length=16_000)
        expires_at = _optional_timestamp(self.expires_at, "expires_at")
        resolved_at = _optional_timestamp(self.resolved_at, "resolved_at")
        object.__setattr__(self, "blocked_action", blocked_action)
        object.__setattr__(self, "capability", capability)
        object.__setattr__(self, "resource", resource)
        object.__setattr__(self, "risk_summary", risk_summary)
        object.__setattr__(self, "expires_at", expires_at)
        object.__setattr__(self, "resolved_at", resolved_at)

        if expires_at is not None and _timestamp_value(expires_at) < _timestamp_value(created_at):
            raise ValueError("expires_at must not precede created_at")
        if resolved_at is not None and _timestamp_value(resolved_at) < _timestamp_value(created_at):
            raise ValueError("resolved_at must not precede created_at")

        if not isinstance(self.status, HumanRequestStatusV1):
            raise ValueError("status must be HumanRequestStatusV1")
        frozen_payload = _freeze_json(self.resolution_payload, "resolution_payload")
        if not isinstance(frozen_payload, Mapping):
            raise ValueError("resolution_payload must be mapping")
        object.__setattr__(self, "resolution_payload", frozen_payload)

        if self.kind is HumanRequestKindV1.MISSING_INFORMATION and not required_fields:
            raise ValueError("MISSING_INFORMATION requires required_fields")
        if self.kind is HumanRequestKindV1.APPROVAL_REQUIRED and blocked_action is None:
            raise ValueError("APPROVAL_REQUIRED requires blocked_action")
        if self.kind is HumanRequestKindV1.CAPABILITY_REQUIRED and capability is None:
            raise ValueError("CAPABILITY_REQUIRED requires capability")
        if self.kind is HumanRequestKindV1.RESOURCE_REQUIRED and resource is None:
            raise ValueError("RESOURCE_REQUIRED requires resource")

        pending = self.status is HumanRequestStatusV1.PENDING
        has_resolution = bool(frozen_payload) or resolved_at is not None
        if pending and has_resolution:
            raise ValueError("PENDING request cannot contain resolution")
        if not pending and resolved_at is None:
            raise ValueError("terminal request requires resolved_at")
        if self.status is HumanRequestStatusV1.ANSWERED and self.kind is not HumanRequestKindV1.MISSING_INFORMATION:
            raise ValueError("ANSWERED is only valid for MISSING_INFORMATION")
        if self.status is HumanRequestStatusV1.APPROVED and self.kind is HumanRequestKindV1.MISSING_INFORMATION:
            raise ValueError("APPROVED is not valid for MISSING_INFORMATION")
        if self.status is HumanRequestStatusV1.UNAVAILABLE and self.kind not in {
            HumanRequestKindV1.CAPABILITY_REQUIRED,
            HumanRequestKindV1.RESOURCE_REQUIRED,
        }:
            raise ValueError("UNAVAILABLE is only valid for capability/resource requests")

    @property
    def is_terminal(self) -> bool:
        return self.status is not HumanRequestStatusV1.PENDING

    @property
    def deduplication_fingerprint(self) -> str:
        """Stable identity for suppression of an identical unresolved blocker."""

        return stable_sha256(
            {
                "mission_id": self.mission_id,
                "goal_id": self.goal_id,
                "todo_id": self.todo_id,
                "run_id": self.run_id,
                "campaign_id": self.campaign_id,
                "experiment_id": self.experiment_id,
                "kind": self.kind.value,
                "question": self.question,
                "required_fields": self.required_fields,
                "blocked_action": self.blocked_action,
                "capability": self.capability,
                "resource": self.resource,
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "request_id": self.request_id,
            "mission_id": self.mission_id,
            "goal_id": self.goal_id,
            "todo_id": self.todo_id,
            "run_id": self.run_id,
            "campaign_id": self.campaign_id,
            "experiment_id": self.experiment_id,
            "kind": self.kind.value,
            "question": self.question,
            "required_fields": list(self.required_fields),
            "blocked_action": self.blocked_action,
            "capability": self.capability,
            "resource": self.resource,
            "attempted_autonomous_acquisition": list(self.attempted_autonomous_acquisition),
            "risk_summary": self.risk_summary,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "status": self.status.value,
            "resolution_payload": _plain(self.resolution_payload),
            "resolved_at": self.resolved_at,
            "deduplication_fingerprint": self.deduplication_fingerprint,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "HumanRequestV1":
        if raw.get("schema_version") != 1:
            raise ValueError("HumanRequestV1 schema_version must be 1")
        result = cls(
            request_id=str(raw["request_id"]),
            mission_id=str(raw["mission_id"]),
            goal_id=None if raw.get("goal_id") is None else str(raw["goal_id"]),
            todo_id=None if raw.get("todo_id") is None else str(raw["todo_id"]),
            run_id=None if raw.get("run_id") is None else str(raw["run_id"]),
            campaign_id=None if raw.get("campaign_id") is None else str(raw["campaign_id"]),
            experiment_id=None if raw.get("experiment_id") is None else str(raw["experiment_id"]),
            kind=HumanRequestKindV1(str(raw["kind"])),
            question=str(raw["question"]),
            required_fields=tuple(raw.get("required_fields", ())),
            blocked_action=None if raw.get("blocked_action") is None else str(raw["blocked_action"]),
            capability=None if raw.get("capability") is None else str(raw["capability"]),
            resource=None if raw.get("resource") is None else str(raw["resource"]),
            attempted_autonomous_acquisition=tuple(raw.get("attempted_autonomous_acquisition", ())),
            risk_summary=None if raw.get("risk_summary") is None else str(raw["risk_summary"]),
            created_at=str(raw["created_at"]),
            expires_at=None if raw.get("expires_at") is None else str(raw["expires_at"]),
            status=HumanRequestStatusV1(str(raw.get("status", HumanRequestStatusV1.PENDING.value))),
            resolution_payload=raw.get("resolution_payload", {}),
            resolved_at=None if raw.get("resolved_at") is None else str(raw["resolved_at"]),
        )
        expected = raw.get("deduplication_fingerprint")
        if expected is not None and expected != result.deduplication_fingerprint:
            raise ValueError("HumanRequestV1 deduplication_fingerprint mismatch")
        return result
