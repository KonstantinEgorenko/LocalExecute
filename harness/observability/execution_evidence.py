'execution evidence diagnostic normalization and deterministic fault classification.'
from __future__ import annotations

import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from harness.evaluation.models import BenchmarkRecord
from harness.workspace.models import ToolName, ToolStatus
from harness.observability.fault_attribution import FaultAnalysis, FaultAttribution, FaultCode, FaultDomain, SampleValidity


_MUTATION_TOOLS = frozenset(
    {ToolName.WRITE_TEXT, ToolName.REPLACE_TEXT, ToolName.DELETE_FILE}
)


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty string")
    return value.strip()


def _non_negative_int(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be integer >= 0")
    return value


def _freeze_mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a mapping")
    try:
        plain = json.loads(
            json.dumps(dict(value), sort_keys=True, ensure_ascii=False, allow_nan=False)
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be JSON-serializable") from exc
    return MappingProxyType(plain)


def _unique_texts(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{name} must be list/tuple")
    result = tuple(_required_text(item, name) for item in value)
    if len(result) != len(set(result)):
        raise ValueError(f"{name} must not contain duplicates")
    return result


@dataclass(frozen=True, slots=True)
class NormalizedToolEvent:
    index: int
    tool: ToolName
    status: ToolStatus
    arguments: Mapping[str, Any]
    changed_paths: tuple[str, ...]
    reference: str

    def __post_init__(self) -> None:
        _non_negative_int(self.index, "index")
        if not isinstance(self.tool, ToolName):
            raise ValueError("tool must be ToolName")
        if not isinstance(self.status, ToolStatus):
            raise ValueError("status must be ToolStatus")
        object.__setattr__(self, "arguments", _freeze_mapping(self.arguments, "arguments"))
        object.__setattr__(
            self, "changed_paths", _unique_texts(self.changed_paths, "changed_paths")
        )
        _required_text(self.reference, "reference")


@dataclass(frozen=True, slots=True)
class NormalizedVerificationGate:
    gate: str
    outcome: str
    detail: str

    def __post_init__(self) -> None:
        _required_text(self.gate, "gate")
        _required_text(self.outcome, "outcome")
        if not isinstance(self.detail, str):
            raise ValueError("detail must be string")


@dataclass(frozen=True, slots=True)
class NormalizedInvocationEvidence:
    invocation_id: str
    case_id: str
    model_id: str

    agent_outcome: str
    agent_reason: str | None

    changed_paths: tuple[str, ...]

    model_calls: int
    tool_calls: int
    protocol_failures: int

    tool_events: tuple[NormalizedToolEvent, ...]

    verification_executed: bool
    verification_results: tuple[NormalizedVerificationGate, ...]

    worker_contents: tuple[str, ...]
    run_status_after_agent: str

    def __post_init__(self) -> None:
        for name in (
            "invocation_id",
            "case_id",
            "model_id",
            "agent_outcome",
            "run_status_after_agent",
        ):
            _required_text(getattr(self, name), name)

        if self.agent_reason is not None and not isinstance(self.agent_reason, str):
            raise ValueError("agent_reason must be str|None")

        object.__setattr__(
            self, "changed_paths", _unique_texts(self.changed_paths, "changed_paths")
        )

        for name in ("model_calls", "tool_calls", "protocol_failures"):
            _non_negative_int(getattr(self, name), name)

        if not isinstance(self.tool_events, tuple) or any(
            not isinstance(item, NormalizedToolEvent) for item in self.tool_events
        ):
            raise ValueError("tool_events must be tuple[NormalizedToolEvent, ...]")

        if not isinstance(self.verification_executed, bool):
            raise ValueError("verification_executed must be bool")

        if not isinstance(self.verification_results, tuple) or any(
            not isinstance(item, NormalizedVerificationGate)
            for item in self.verification_results
        ):
            raise ValueError(
                "verification_results must be tuple[NormalizedVerificationGate, ...]"
            )

        if not isinstance(self.worker_contents, tuple) or any(
            not isinstance(item, str) for item in self.worker_contents
        ):
            raise ValueError("worker_contents must be tuple[str, ...]")

    @property
    def evidence_refs(self) -> tuple[str, ...]:
        return tuple(event.reference for event in self.tool_events)


def normalize_diagnostic(raw: Mapping[str, Any]) -> NormalizedInvocationEvidence:
    if not isinstance(raw, Mapping):
        raise ValueError("diagnostic must be a mapping")

    agent = raw.get("agent_result")
    if not isinstance(agent, Mapping):
        raise ValueError("diagnostic.agent_result must be mapping")

    tool_events: list[NormalizedToolEvent] = []
    for item in raw.get("evidence_events", ()):
        if not isinstance(item, Mapping) or item.get("evidence_type") != "TOOL_RESULT":
            continue
        payload = item.get("payload")
        if not isinstance(payload, Mapping):
            raise ValueError("TOOL_RESULT payload must be mapping")
        tool_events.append(
            NormalizedToolEvent(
                index=len(tool_events),
                tool=ToolName(str(payload["tool"])),
                status=ToolStatus(str(payload["tool_status"])),
                arguments=payload.get("arguments", {}),
                changed_paths=tuple(str(x) for x in payload.get("changed_paths", ())),
                reference=_required_text(item.get("reference"), "evidence reference"),
            )
        )

    verification_results: list[NormalizedVerificationGate] = []
    for item in raw.get("verification_results", ()):
        if not isinstance(item, Mapping):
            raise ValueError("verification result must be mapping")
        verification_results.append(
            NormalizedVerificationGate(
                gate=str(item["gate"]),
                outcome=str(item["outcome"]),
                detail=str(item.get("detail", "")),
            )
        )

    worker_contents = tuple(
        str(item.get("content", ""))
        for item in raw.get("worker_results", ())
        if isinstance(item, Mapping)
    )

    return NormalizedInvocationEvidence(
        invocation_id=str(raw["invocation_id"]),
        case_id=str(raw["case_id"]),
        model_id=str(raw["model_id"]),
        agent_outcome=str(agent["outcome"]),
        agent_reason=(
            None if agent.get("reason") is None else str(agent.get("reason"))
        ),
        changed_paths=tuple(str(x) for x in agent.get("changed_paths", ())),
        model_calls=int(agent.get("model_turns", 0)),
        tool_calls=int(agent.get("tool_calls", 0)),
        protocol_failures=int(raw.get("protocol_failures", 0)),
        tool_events=tuple(tool_events),
        verification_executed=bool(raw.get("verification_executed", False)),
        verification_results=tuple(verification_results),
        worker_contents=worker_contents,
        run_status_after_agent=str(raw["run_status_after_agent"]),
    )


def _reason_has(reason: str | None, fragment: str) -> bool:
    return reason is not None and fragment.casefold() in reason.casefold()


def _has_invalid_final(contents: tuple[str, ...]) -> bool:
    for raw in contents:
        try:
            parsed = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue
        if (
            isinstance(parsed, Mapping)
            and str(parsed.get("action", "")).upper() == "FINAL"
            and parsed.get("final") is None
        ):
            return True
    return False


def _secondary(*codes: FaultCode | None) -> tuple[FaultCode, ...]:
    result: list[FaultCode] = []
    for code in codes:
        if code is not None and code not in result:
            result.append(code)
    return tuple(result)


def _quota_secondary(reason: str | None) -> FaultCode | None:
    if _reason_has(reason, "worker call quota exhausted"):
        return FaultCode.WORKER_CALL_QUOTA_EXHAUSTED
    return None


def _tool_failure_secondary(reason: str | None) -> FaultCode | None:
    if _reason_has(reason, "tool failure limit reached"):
        return FaultCode.TOOL_FAILURE_LIMIT_REACHED
    return None


def classify_invocation(
    record: BenchmarkRecord,
    evidence: NormalizedInvocationEvidence,
) -> FaultAnalysis:
    if not isinstance(record, BenchmarkRecord):
        raise ValueError("record must be BenchmarkRecord")
    if not isinstance(evidence, NormalizedInvocationEvidence):
        raise ValueError("evidence must be NormalizedInvocationEvidence")
    if record.invocation_id != evidence.invocation_id:
        raise ValueError("record/evidence invocation_id mismatch")
    if record.case_id != evidence.case_id:
        raise ValueError("record/evidence case_id mismatch")
    if record.model_id != evidence.model_id:
        raise ValueError("record/evidence model_id mismatch")

    reason = evidence.agent_reason
    secondary: tuple[FaultCode, ...] = ()
    validity = SampleValidity.VALID
    finalization_candidate = False

    # P0: deterministic verified success dominates recoverable intermediate faults.
    if record.verified_success:
        domain = FaultDomain.NONE
        code = FaultCode.VERIFIED_SUCCESS
        attribution = FaultAttribution.NONE

    # P1: invalid infrastructure samples do not count against model quality.
    elif _reason_has(reason, "not preloaded"):
        domain = FaultDomain.INFRASTRUCTURE
        code = FaultCode.MODEL_NOT_RESIDENT
        attribution = FaultAttribution.INFRASTRUCTURE
        validity = SampleValidity.INVALID_INFRASTRUCTURE
    elif any(
        _reason_has(reason, fragment)
        for fragment in ("server unavailable", "cannot reach", "connection refused")
    ):
        domain = FaultDomain.INFRASTRUCTURE
        code = FaultCode.SERVER_UNAVAILABLE
        attribution = FaultAttribution.INFRASTRUCTURE
        validity = SampleValidity.INVALID_INFRASTRUCTURE

    # P2: verification deterministic failure is implementation evidence.
    elif evidence.verification_executed and any(
        gate.outcome.upper() == "FAILED" for gate in evidence.verification_results
    ):
        domain = FaultDomain.VERIFICATION
        code = FaultCode.DETERMINISTIC_GATE_FAILED
        attribution = FaultAttribution.CANDIDATE_IMPLEMENTATION

    # P3: strict agent protocol failure.
    elif _reason_has(reason, "agent protocol failure"):
        domain = FaultDomain.PROTOCOL
        code = FaultCode.AGENT_PROTOCOL_FAILURE
        attribution = FaultAttribution.MODEL
        secondary = _secondary(
            FaultCode.INVALID_FINAL if _has_invalid_final(evidence.worker_contents) else None
        )

    else:
        denied = tuple(
            event for event in evidence.tool_events if event.status is ToolStatus.DENIED
        )
        mutation_contract_faults = tuple(
            event
            for event in evidence.tool_events
            if event.tool in _MUTATION_TOOLS
            and event.status in {ToolStatus.INVALID, ToolStatus.CONFLICT}
        )

        # P4: V1 exposed tools that execution policy could later deny.
        if denied:
            domain = FaultDomain.TOOL_POLICY
            code = FaultCode.TOOL_DENIED
            attribution = FaultAttribution.HARNESS_INTERFACE
            secondary = _secondary(
                _tool_failure_secondary(reason),
                _quota_secondary(reason),
                (
                    FaultCode.REPEATED_IDENTICAL_TOOL
                    if _reason_has(reason, "repeated identical tool call")
                    else None
                ),
            )

        # P5: mutation API/optimistic-write contract friction.
        elif mutation_contract_faults:
            last_fault = mutation_contract_faults[-1]
            domain = FaultDomain.TOOL_CONTRACT
            code = (
                FaultCode.TOOL_WRITE_CONFLICT
                if last_fault.status is ToolStatus.CONFLICT
                else FaultCode.TOOL_ARGUMENT_INVALID
            )
            attribution = FaultAttribution.HARNESS_INTERFACE
            secondary = _secondary(
                (
                    FaultCode.REPEATED_IDENTICAL_TOOL
                    if _reason_has(reason, "repeated identical tool call")
                    else None
                ),
                _tool_failure_secondary(reason),
                _quota_secondary(reason),
            )

        # P6: genuine repeated tool trajectory with no stronger causal tool fault.
        elif _reason_has(reason, "repeated identical tool call"):
            domain = FaultDomain.LOOP
            code = FaultCode.REPEATED_IDENTICAL_TOOL
            attribution = FaultAttribution.MODEL

        # P7: productive worker-call budget.
        elif _reason_has(reason, "worker call quota exhausted"):
            domain = FaultDomain.QUOTA
            code = FaultCode.WORKER_CALL_QUOTA_EXHAUSTED
            attribution = FaultAttribution.MODEL
            finalization_candidate = bool(
                evidence.changed_paths
                and evidence.tool_events
                and evidence.tool_events[-1].status is ToolStatus.SUCCESS
            )
            secondary = _secondary(
                FaultCode.FINALIZATION_CANDIDATE
                if finalization_candidate
                else None
            )

        # Retain explicit tool-failure terminal state even if a more specific
        # DENIED/INVALID/CONFLICT event was not present in evidence.
        elif _reason_has(reason, "tool failure limit reached"):
            domain = FaultDomain.TOOL_CONTRACT
            code = FaultCode.TOOL_FAILURE_LIMIT_REACHED
            attribution = FaultAttribution.HARNESS_INTERFACE

        elif any(
            token in (reason or "").casefold()
            for token in ("context too", "context insufficient", "context window")
        ):
            domain = FaultDomain.CONTEXT
            code = FaultCode.CONTEXT_INSUFFICIENT
            attribution = FaultAttribution.MODEL

        else:
            domain = FaultDomain.UNKNOWN
            code = FaultCode.UNKNOWN_FAILURE
            attribution = FaultAttribution.UNKNOWN

    return FaultAnalysis(
        invocation_id=record.invocation_id,
        case_id=record.case_id,
        task_class=record.task_class.value,
        repetition=record.repetition,
        model_id=record.model_id,
        router_selected=record.router_selected,
        sample_validity=validity,
        primary_domain=domain,
        primary_code=code,
        attribution=attribution,
        secondary_codes=secondary,
        verified_success=record.verified_success,
        verification_executed=evidence.verification_executed,
        finalization_candidate=finalization_candidate,
        changed_paths=evidence.changed_paths,
        model_calls=record.measurement.model_calls,
        tool_calls=record.measurement.tool_calls,
        protocol_failures=record.measurement.protocol_failures,
        latency_ms=record.measurement.latency_ms,
        evidence_refs=evidence.evidence_refs,
        reason=reason,
    )
