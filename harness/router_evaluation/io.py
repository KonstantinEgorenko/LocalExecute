'Strict normalization of frozen advanced routing / Router evaluation live report records.'
from __future__ import annotations

from typing import Any, Mapping

from .models import AttemptObservation, CaseObservation


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be mapping")
    return value


def observation_from_live_record(
    raw: Mapping[str, Any],
    *,
    source: str,
    repetition: int,
    task_class: str,
    expected_selected_profile_id: str | None = None,
) -> CaseObservation:
    _mapping(raw, "raw")
    attempts = []
    for index, item in enumerate(raw.get("attempts", ())):
        attempt = _mapping(item, f"attempt[{index}]")
        result = _mapping(attempt.get("result"), f"attempt[{index}].result")
        attempts.append(
            AttemptObservation(
                profile_id=str(attempt["profile_id"]),
                verified=bool(attempt.get("verified", False)),
                latency_ms=int(attempt.get("latency_ms", 0)),
                model_calls=int(result.get("model_calls", 0)),
                tool_calls=int(result.get("tool_calls", 0)),
                protocol_failures=int(result.get("protocol_failures", 0)),
                tool_failures=int(result.get("tool_failures", 0)),
                finalization_calls=int(result.get("finalization_calls", 0)),
                outcome=str(result.get("outcome", "UNKNOWN")),
            )
        )
    selected = raw.get("selected_profile_id")
    selected_id = None if selected is None else str(selected)
    selection_match = (
        True
        if expected_selected_profile_id is None
        else selected_id == expected_selected_profile_id
    )
    case_id = str(raw["case_id"])
    return CaseObservation(
        run_key=f"{source}:{repetition}:{case_id}",
        source=source,
        repetition=repetition,
        case_id=case_id,
        task_class=task_class,
        selected_profile_id=selected_id,
        attempts=tuple(attempts),
        verified=bool(raw.get("verified", False)),
        latency_ms=int(raw.get("latency_ms", 0)),
        infrastructure_invalid=bool(raw.get("infrastructure_invalid", False)),
        router_selection_match=selection_match,
    )
