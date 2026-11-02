'research diagnostics.2 typed mission routing contracts above the frozen mission runtime.'
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping

from ..mission.models import MissionSpecV1
from .serialization import safe_id


class MissionModeV1(StrEnum):
    EXECUTION = "EXECUTION"
    RESEARCH_CAMPAIGN = "RESEARCH_CAMPAIGN"


def get_mission_mode(spec: MissionSpecV1) -> MissionModeV1:
    raw = spec.global_constraints.get("mission_mode")
    if raw is None:
        return MissionModeV1.EXECUTION
    if not isinstance(raw, str):
        raise ValueError("mission_mode must be string when present")
    try:
        return MissionModeV1(raw)
    except ValueError as exc:
        raise ValueError(f"unsupported mission mode: {raw!r}") from exc


def get_research_profile_id(spec: MissionSpecV1) -> str:
    raw = spec.global_constraints.get("research_profile_id")
    if raw is None:
        raise ValueError("RESEARCH_CAMPAIGN requires global_constraints.research_profile_id")
    return safe_id(raw, "research_profile_id")


def get_proposal_generator_identity(spec: MissionSpecV1) -> str:
    raw = spec.global_constraints.get("proposal_generator_identity", "deterministic")
    return safe_id(raw, "proposal_generator_identity")


@dataclass(frozen=True, slots=True)
class MissionDispatchResultV1:
    'Tagged result of the additive research diagnostics dispatcher.'

    mode: MissionModeV1
    execution_result: Any | None = None
    research_run_result: Any | None = None
    research_summary: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.mode, MissionModeV1):
            raise ValueError("mode must be MissionModeV1")
        if self.mode is MissionModeV1.EXECUTION:
            if self.execution_result is None:
                raise ValueError("EXECUTION dispatch requires execution_result")
            if self.research_run_result is not None or self.research_summary is not None:
                raise ValueError("EXECUTION dispatch cannot contain research result fields")
        else:
            if self.execution_result is not None:
                raise ValueError("RESEARCH_CAMPAIGN dispatch cannot contain execution_result")
            if self.research_run_result is None or self.research_summary is None:
                raise ValueError("RESEARCH_CAMPAIGN dispatch requires research run result and summary")
