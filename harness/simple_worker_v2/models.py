"""Immutable contracts for the Phase 24F SimpleWorkerV2."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from harness.workspace_aci_v2 import AciActionV2
from harness.integration.failure_policy import FailureKindV1


class SimpleWorkerActionKindV2(StrEnum):
    VIEW = "VIEW"
    SEARCH = "SEARCH"
    EDIT = "EDIT"
    RUN = "RUN"
    FINISH = "FINISH"


class SimpleWorkerOutcomeV2(StrEnum):
    SUCCESS = "SUCCESS"
    NEEDS_ATTENTION = "NEEDS_ATTENTION"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"


@dataclass(frozen=True, slots=True)
class SimpleWorkerActionV2:
    schema_version: int
    action: SimpleWorkerActionKindV2
    aci_action: AciActionV2 | None
    summary: str | None

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("schema_version must be exactly 1")
        if not isinstance(self.action, SimpleWorkerActionKindV2):
            raise ValueError("action must be SimpleWorkerActionKindV2")
        if self.action is SimpleWorkerActionKindV2.FINISH:
            if self.aci_action is not None:
                raise ValueError("FINISH cannot contain an ACI action")
            if not isinstance(self.summary, str) or not self.summary.strip():
                raise ValueError("FINISH requires a non-empty summary")
        else:
            if not isinstance(self.aci_action, AciActionV2):
                raise ValueError("non-FINISH action requires AciActionV2")
            if self.summary is not None:
                raise ValueError("non-FINISH action requires summary=null")


@dataclass(frozen=True, slots=True)
class SimpleWorkerPolicyV2:
    max_model_turns: int = 12
    max_aci_calls: int = 10
    max_protocol_failures: int = 2
    max_invalid_file_rewrites: int = 2
    max_message_bytes: int = 120 * 1024
    max_observation_bytes: int = 64 * 1024

    def __post_init__(self) -> None:
        for field_name in (
            "max_model_turns",
            "max_aci_calls",
            "max_protocol_failures",
            "max_invalid_file_rewrites",
            "max_message_bytes",
            "max_observation_bytes",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{field_name} must be an integer > 0")


@dataclass(frozen=True, slots=True)
class SimpleWorkerResultV2:
    outcome: SimpleWorkerOutcomeV2
    final_summary: str | None
    model_turns: int
    aci_calls: int
    changed_paths: tuple[str, ...]
    model_id: str | None
    reason: str | None = None
    failure_kind: FailureKindV1 | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, SimpleWorkerOutcomeV2):
            raise ValueError("outcome must be SimpleWorkerOutcomeV2")
        if self.failure_kind is not None and not isinstance(self.failure_kind, FailureKindV1):
            raise ValueError("failure_kind must use the production FailureKindV1 contract")
        if self.outcome is SimpleWorkerOutcomeV2.SUCCESS and self.failure_kind is not None:
            raise ValueError("successful worker cannot carry a failure_kind")
        if self.final_summary is not None and (
            not isinstance(self.final_summary, str) or not self.final_summary.strip()
        ):
            raise ValueError("final_summary must be null or non-empty")
        for field_name in ("model_turns", "aci_calls"):
            value = getattr(self, field_name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{field_name} must be an integer >= 0")
        object.__setattr__(self, "changed_paths", tuple(self.changed_paths))
        if len(set(self.changed_paths)) != len(self.changed_paths):
            raise ValueError("changed_paths must not contain duplicates")
        if self.reason is not None and (
            not isinstance(self.reason, str) or not self.reason.strip()
        ):
            raise ValueError("reason must be null or non-empty")

    def to_dict(self) -> dict[str, object]:
        return {
            "outcome": self.outcome.value,
            "final_summary": self.final_summary,
            "model_turns": self.model_turns,
            "aci_calls": self.aci_calls,
            "changed_paths": list(self.changed_paths),
            "model_id": self.model_id,
            "reason": self.reason,
            "failure_kind": self.failure_kind.value if self.failure_kind is not None else None,
        }
