'Immutable execution evidence fault taxonomy contracts.'
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class SampleValidity(StrEnum):
    VALID = "VALID"
    INVALID_INFRASTRUCTURE = "INVALID_INFRASTRUCTURE"


class FaultAttribution(StrEnum):
    NONE = "NONE"
    MODEL = "MODEL"
    HARNESS_INTERFACE = "HARNESS_INTERFACE"
    INFRASTRUCTURE = "INFRASTRUCTURE"
    CANDIDATE_IMPLEMENTATION = "CANDIDATE_IMPLEMENTATION"
    ROUTER = "ROUTER"
    MIXED = "MIXED"
    UNKNOWN = "UNKNOWN"


class FaultDomain(StrEnum):
    NONE = "NONE"
    INFRASTRUCTURE = "INFRASTRUCTURE"
    PROTOCOL = "PROTOCOL"
    TOOL_CONTRACT = "TOOL_CONTRACT"
    TOOL_POLICY = "TOOL_POLICY"
    LOOP = "LOOP"
    QUOTA = "QUOTA"
    CONTEXT = "CONTEXT"
    VERIFICATION = "VERIFICATION"
    IMPLEMENTATION = "IMPLEMENTATION"
    ROUTING = "ROUTING"
    UNKNOWN = "UNKNOWN"


class FaultCode(StrEnum):
    NONE = "NONE"
    VERIFIED_SUCCESS = "VERIFIED_SUCCESS"

    SERVER_UNAVAILABLE = "SERVER_UNAVAILABLE"
    MODEL_NOT_RESIDENT = "MODEL_NOT_RESIDENT"

    AGENT_PROTOCOL_FAILURE = "AGENT_PROTOCOL_FAILURE"
    INVALID_FINAL = "INVALID_FINAL"

    TOOL_ARGUMENT_INVALID = "TOOL_ARGUMENT_INVALID"
    TOOL_WRITE_CONFLICT = "TOOL_WRITE_CONFLICT"
    TOOL_DENIED = "TOOL_DENIED"
    TOOL_FAILURE_LIMIT_REACHED = "TOOL_FAILURE_LIMIT_REACHED"

    REPEATED_IDENTICAL_TOOL = "REPEATED_IDENTICAL_TOOL"

    WORKER_CALL_QUOTA_EXHAUSTED = "WORKER_CALL_QUOTA_EXHAUSTED"
    FINALIZATION_CANDIDATE = "FINALIZATION_CANDIDATE"

    DETERMINISTIC_GATE_FAILED = "DETERMINISTIC_GATE_FAILED"

    CONTEXT_INSUFFICIENT = "CONTEXT_INSUFFICIENT"

    UNKNOWN_FAILURE = "UNKNOWN_FAILURE"


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-empty string")
    return value.strip()


def _non_negative_int(value: object, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{field_name} must be integer >= 0")
    return value


@dataclass(frozen=True, slots=True)
class FaultAnalysis:
    invocation_id: str
    case_id: str
    task_class: str
    repetition: int
    model_id: str
    router_selected: bool

    sample_validity: SampleValidity

    primary_domain: FaultDomain
    primary_code: FaultCode
    attribution: FaultAttribution
    secondary_codes: tuple[FaultCode, ...]

    verified_success: bool
    verification_executed: bool
    finalization_candidate: bool

    changed_paths: tuple[str, ...]

    model_calls: int
    tool_calls: int
    protocol_failures: int
    latency_ms: int

    evidence_refs: tuple[str, ...]
    reason: str | None

    def __post_init__(self) -> None:
        for field_name in ("invocation_id", "case_id", "task_class", "model_id"):
            _required_text(getattr(self, field_name), field_name)

        for field_name in (
            "repetition",
            "model_calls",
            "tool_calls",
            "protocol_failures",
            "latency_ms",
        ):
            _non_negative_int(getattr(self, field_name), field_name)

        if not isinstance(self.router_selected, bool):
            raise ValueError("router_selected must be bool")
        if not isinstance(self.sample_validity, SampleValidity):
            raise ValueError("sample_validity must be SampleValidity")
        if not isinstance(self.primary_domain, FaultDomain):
            raise ValueError("primary_domain must be FaultDomain")
        if not isinstance(self.primary_code, FaultCode):
            raise ValueError("primary_code must be FaultCode")
        if not isinstance(self.attribution, FaultAttribution):
            raise ValueError("attribution must be FaultAttribution")

        if (
            not isinstance(self.secondary_codes, tuple)
            or any(not isinstance(code, FaultCode) for code in self.secondary_codes)
        ):
            raise ValueError("secondary_codes must be tuple[FaultCode, ...]")
        if len(self.secondary_codes) != len(set(self.secondary_codes)):
            raise ValueError("secondary_codes must not contain duplicates")
        if self.primary_code in self.secondary_codes:
            raise ValueError("primary_code must not be duplicated in secondary_codes")

        for field_name in (
            "verified_success",
            "verification_executed",
            "finalization_candidate",
        ):
            if not isinstance(getattr(self, field_name), bool):
                raise ValueError(f"{field_name} must be bool")

        for field_name in ("changed_paths", "evidence_refs"):
            value = getattr(self, field_name)
            if (
                not isinstance(value, tuple)
                or any(not isinstance(item, str) or not item.strip() for item in value)
            ):
                raise ValueError(f"{field_name} must be tuple[str, ...]")
            if len(value) != len(set(value)):
                raise ValueError(f"{field_name} must not contain duplicates")

        if self.reason is not None and not isinstance(self.reason, str):
            raise ValueError("reason must be str|None")

        if self.verified_success:
            if self.primary_domain is not FaultDomain.NONE:
                raise ValueError("verified success requires FaultDomain.NONE")
            if self.primary_code is not FaultCode.VERIFIED_SUCCESS:
                raise ValueError("verified success requires VERIFIED_SUCCESS")
            if self.attribution is not FaultAttribution.NONE:
                raise ValueError("verified success requires attribution NONE")

        if self.primary_domain is FaultDomain.NONE and not self.verified_success:
            raise ValueError("FaultDomain.NONE only valid for verified success")

        if (
            self.sample_validity is SampleValidity.INVALID_INFRASTRUCTURE
            and self.attribution is not FaultAttribution.INFRASTRUCTURE
        ):
            raise ValueError(
                "INVALID_INFRASTRUCTURE requires INFRASTRUCTURE attribution"
            )

        if self.finalization_candidate:
            if self.primary_domain is not FaultDomain.QUOTA:
                raise ValueError(
                    "finalization_candidate requires primary QUOTA fault"
                )
            if FaultCode.FINALIZATION_CANDIDATE not in self.secondary_codes:
                raise ValueError(
                    "finalization_candidate requires FINALIZATION_CANDIDATE code"
                )

    @property
    def model_quality_eligible(self) -> bool:
        return self.sample_validity is SampleValidity.VALID

    @property
    def router_quality_eligible(self) -> bool:
        return self.sample_validity is SampleValidity.VALID

    def to_dict(self) -> dict[str, object]:
        return {
            "invocation_id": self.invocation_id,
            "case_id": self.case_id,
            "task_class": self.task_class,
            "repetition": self.repetition,
            "model_id": self.model_id,
            "router_selected": self.router_selected,
            "sample_validity": self.sample_validity.value,
            "primary_domain": self.primary_domain.value,
            "primary_code": self.primary_code.value,
            "attribution": self.attribution.value,
            "secondary_codes": [code.value for code in self.secondary_codes],
            "verified_success": self.verified_success,
            "verification_executed": self.verification_executed,
            "finalization_candidate": self.finalization_candidate,
            "changed_paths": list(self.changed_paths),
            "model_calls": self.model_calls,
            "tool_calls": self.tool_calls,
            "protocol_failures": self.protocol_failures,
            "latency_ms": self.latency_ms,
            "evidence_refs": list(self.evidence_refs),
            "reason": self.reason,
            "model_quality_eligible": self.model_quality_eligible,
            "router_quality_eligible": self.router_quality_eligible,
        }
