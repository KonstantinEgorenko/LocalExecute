"""Typed, compact model-invocation lineage projected into mission traces.

This module is observability-only. It owns no execution, routing, retry, budget,
research, or durability authority. Raw prompts/responses are intentionally not
part of the manifest.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ModelInvocationKindV1(str, Enum):
    LEAD_PLAN = "LEAD_PLAN"
    WORKER_EXECUTION = "WORKER_EXECUTION"
    CRITIC_REVIEW = "CRITIC_REVIEW"
    JUDGE_REVIEW = "JUDGE_REVIEW"
    PUBLIC_AUTHORITY_RESOLUTION = "PUBLIC_AUTHORITY_RESOLUTION"
    PUBLIC_WITNESS_SYNTHESIS = "PUBLIC_WITNESS_SYNTHESIS"
    POST_REJECTION_RESYNTHESIS = "POST_REJECTION_RESYNTHESIS"
    RECOVERY_DIAGNOSIS = "RECOVERY_DIAGNOSIS"
    RECOVERY_REPLAN = "RECOVERY_REPLAN"
    RESEARCH_PROPOSAL = "RESEARCH_PROPOSAL"


class ModelInvocationStatusV1(str, Enum):
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


def _plain(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _plain(value.to_dict())
    return value


def stable_fingerprint(value: Any) -> str:
    """Canonical SHA-256 without persisting the input value."""
    encoded = json.dumps(
        _plain(value), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def runtime_fingerprint(runtime: Mapping[str, Any]) -> str:
    """Fingerprint the effective model/runtime configuration supplied by a caller."""
    if not isinstance(runtime, Mapping) or not runtime:
        raise ValueError("runtime fingerprint input must be a non-empty mapping")
    return stable_fingerprint(runtime)


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")
    return value


def _optional_text(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, name)


def _sha256(value: object, name: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 hex digest")
    return value


def _non_negative(value: object, name: str, *, optional: bool = False) -> int | None:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _budget(value: Mapping[str, int] | None, name: str) -> Mapping[str, int] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a mapping or None")
    normalized: dict[str, int] = {}
    for key, item in value.items():
        key_text = _required_text(key, f"{name} key")
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            raise ValueError(f"{name}[{key_text!r}] must be a non-negative integer")
        normalized[key_text] = item
    return MappingProxyType(dict(sorted(normalized.items())))


@dataclass(frozen=True, slots=True)
class ModelInvocationManifestV1:
    schema_version: int
    invocation_id: str
    request_kind: ModelInvocationKindV1
    mission_id: str
    goal_id: str
    logical_model_id: str
    runtime_kind: str
    runtime_fingerprint: str
    input_fingerprint: str
    duration_ms: int
    status: ModelInvocationStatusV1
    todo_id: str | None = None
    run_id: str | None = None
    campaign_id: str | None = None
    experiment_id: str | None = None
    proposal_cohort_id: str | None = None
    request_id: str | None = None
    attempt_index: int | None = None
    slice_index: int | None = None
    model_tag: str | None = None
    context_bundle_id: str | None = None
    evidence_packet_fingerprint: str | None = None
    output_fingerprint: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    requested_context_length: int | None = None
    observed_context_length: int | None = None
    budget_before: Mapping[str, int] | None = None
    budget_after: Mapping[str, int] | None = None
    diagnostic: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("ModelInvocationManifestV1 schema_version must be 1")
        _required_text(self.invocation_id, "invocation_id")
        if not isinstance(self.request_kind, ModelInvocationKindV1):
            raise ValueError("request_kind must be ModelInvocationKindV1")
        _required_text(self.mission_id, "mission_id")
        _required_text(self.goal_id, "goal_id")
        _required_text(self.logical_model_id, "logical_model_id")
        _required_text(self.runtime_kind, "runtime_kind")
        _sha256(self.runtime_fingerprint, "runtime_fingerprint")
        _sha256(self.input_fingerprint, "input_fingerprint")
        _non_negative(self.duration_ms, "duration_ms")
        if not isinstance(self.status, ModelInvocationStatusV1):
            raise ValueError("status must be ModelInvocationStatusV1")
        for name in (
            "todo_id", "run_id", "campaign_id", "experiment_id",
            "proposal_cohort_id", "request_id", "model_tag", "context_bundle_id",
        ):
            _optional_text(getattr(self, name), name)
        _non_negative(self.attempt_index, "attempt_index", optional=True)
        _non_negative(self.slice_index, "slice_index", optional=True)
        _sha256(self.evidence_packet_fingerprint, "evidence_packet_fingerprint", optional=True)
        _sha256(self.output_fingerprint, "output_fingerprint", optional=True)
        for name in (
            "prompt_tokens", "completion_tokens",
            "requested_context_length", "observed_context_length",
        ):
            _non_negative(getattr(self, name), name, optional=True)
        if self.context_bundle_id is not None and self.evidence_packet_fingerprint is not None:
            raise ValueError("context_bundle_id and evidence_packet_fingerprint are mutually exclusive")
        object.__setattr__(self, "budget_before", _budget(self.budget_before, "budget_before"))
        object.__setattr__(self, "budget_after", _budget(self.budget_after, "budget_after"))
        if self.status is ModelInvocationStatusV1.SUCCESS:
            if self.output_fingerprint is None:
                raise ValueError("successful invocation requires output_fingerprint")
            if self.diagnostic is not None:
                raise ValueError("successful invocation cannot contain diagnostic")
        else:
            _required_text(self.diagnostic, "diagnostic")

    @property
    def fingerprint(self) -> str:
        return stable_fingerprint(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "invocation_id": self.invocation_id,
            "request_kind": self.request_kind.value,
            "mission_id": self.mission_id,
            "goal_id": self.goal_id,
            "todo_id": self.todo_id,
            "run_id": self.run_id,
            "campaign_id": self.campaign_id,
            "experiment_id": self.experiment_id,
            "proposal_cohort_id": self.proposal_cohort_id,
            "request_id": self.request_id,
            "attempt_index": self.attempt_index,
            "slice_index": self.slice_index,
            "logical_model_id": self.logical_model_id,
            "model_tag": self.model_tag,
            "runtime_kind": self.runtime_kind,
            "runtime_fingerprint": self.runtime_fingerprint,
            "context_bundle_id": self.context_bundle_id,
            "evidence_packet_fingerprint": self.evidence_packet_fingerprint,
            "input_fingerprint": self.input_fingerprint,
            "output_fingerprint": self.output_fingerprint,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "requested_context_length": self.requested_context_length,
            "observed_context_length": self.observed_context_length,
            "budget_before": None if self.budget_before is None else dict(self.budget_before),
            "budget_after": None if self.budget_after is None else dict(self.budget_after),
            "duration_ms": self.duration_ms,
            "status": self.status.value,
            "diagnostic": self.diagnostic,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ModelInvocationManifestV1":
        expected = {
            "schema_version", "invocation_id", "request_kind", "mission_id", "goal_id",
            "todo_id", "run_id", "campaign_id", "experiment_id", "proposal_cohort_id",
            "request_id", "attempt_index", "slice_index", "logical_model_id", "model_tag",
            "runtime_kind", "runtime_fingerprint", "context_bundle_id",
            "evidence_packet_fingerprint", "input_fingerprint", "output_fingerprint",
            "prompt_tokens", "completion_tokens", "requested_context_length",
            "observed_context_length", "budget_before", "budget_after", "duration_ms",
            "status", "diagnostic",
        }
        if set(raw) != expected:
            raise ValueError("ModelInvocationManifestV1 has missing or unknown fields")
        return cls(
            schema_version=int(raw["schema_version"]),
            invocation_id=str(raw["invocation_id"]),
            request_kind=ModelInvocationKindV1(str(raw["request_kind"])),
            mission_id=str(raw["mission_id"]),
            goal_id=str(raw["goal_id"]),
            todo_id=None if raw["todo_id"] is None else str(raw["todo_id"]),
            run_id=None if raw["run_id"] is None else str(raw["run_id"]),
            campaign_id=None if raw["campaign_id"] is None else str(raw["campaign_id"]),
            experiment_id=None if raw["experiment_id"] is None else str(raw["experiment_id"]),
            proposal_cohort_id=None if raw["proposal_cohort_id"] is None else str(raw["proposal_cohort_id"]),
            request_id=None if raw["request_id"] is None else str(raw["request_id"]),
            attempt_index=raw["attempt_index"],
            slice_index=raw["slice_index"],
            logical_model_id=str(raw["logical_model_id"]),
            model_tag=None if raw["model_tag"] is None else str(raw["model_tag"]),
            runtime_kind=str(raw["runtime_kind"]),
            runtime_fingerprint=str(raw["runtime_fingerprint"]),
            context_bundle_id=None if raw["context_bundle_id"] is None else str(raw["context_bundle_id"]),
            evidence_packet_fingerprint=None if raw["evidence_packet_fingerprint"] is None else str(raw["evidence_packet_fingerprint"]),
            input_fingerprint=str(raw["input_fingerprint"]),
            output_fingerprint=None if raw["output_fingerprint"] is None else str(raw["output_fingerprint"]),
            prompt_tokens=raw["prompt_tokens"],
            completion_tokens=raw["completion_tokens"],
            requested_context_length=raw["requested_context_length"],
            observed_context_length=raw["observed_context_length"],
            budget_before=raw["budget_before"],
            budget_after=raw["budget_after"],
            duration_ms=raw["duration_ms"],
            status=ModelInvocationStatusV1(str(raw["status"])),
            diagnostic=None if raw["diagnostic"] is None else str(raw["diagnostic"]),
        )


__all__ = [
    "ModelInvocationKindV1",
    "ModelInvocationManifestV1",
    "ModelInvocationStatusV1",
    "runtime_fingerprint",
    "stable_fingerprint",
]
