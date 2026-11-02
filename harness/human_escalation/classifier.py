'Structured blocker classification for research campaign.R0 human escalation.\n\nThe classifier consumes harness-observable facts only.  It does not inspect\nfree-form model prose to infer permission and it does not own hard-deny policy;\nthat authority lives in :mod:`harness.human_escalation.policy`.\n'
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .models import HumanRequestKindV1, _optional_safe_id, _optional_text, _safe_id, _text, _unique_text


@dataclass(frozen=True, slots=True)
class HumanBlockerObservationV1:
    """Typed facts describing why autonomous work cannot proceed.

    Exactly zero or one request-producing blocker category may be present.
    Zero means there is no human blocker.  Multiple simultaneous categories are
    rejected instead of picking one from model prose; the caller must split or
    normalize the blocker first.
    """

    mission_id: str
    question: str
    goal_id: str | None = None
    todo_id: str | None = None
    run_id: str | None = None
    campaign_id: str | None = None
    experiment_id: str | None = None
    action_id: str | None = None
    blocked_action: str | None = None
    required_fields: tuple[str, ...] = ()
    approval_required: bool = False
    capability: str | None = None
    capability_available: bool | None = None
    resource: str | None = None
    resource_available: bool | None = None
    autonomous_acquisition_available: bool = False
    attempted_autonomous_acquisition: tuple[str, ...] = ()
    risk_summary: str | None = None

    def __post_init__(self) -> None:
        _safe_id(self.mission_id, "mission_id")
        _text(self.question, "question", max_length=32_000)
        for name in ("goal_id", "todo_id", "run_id", "campaign_id", "experiment_id"):
            object.__setattr__(self, name, _optional_safe_id(getattr(self, name), name))
        object.__setattr__(self, "action_id", _optional_text(self.action_id, "action_id", max_length=1_024))
        object.__setattr__(self, "blocked_action", _optional_text(self.blocked_action, "blocked_action", max_length=16_000))
        object.__setattr__(self, "capability", _optional_text(self.capability, "capability", max_length=4_000))
        object.__setattr__(self, "resource", _optional_text(self.resource, "resource", max_length=4_000))
        object.__setattr__(self, "risk_summary", _optional_text(self.risk_summary, "risk_summary", max_length=16_000))
        object.__setattr__(self, "required_fields", _unique_text(self.required_fields, "required_fields", maximum=64))
        object.__setattr__(
            self,
            "attempted_autonomous_acquisition",
            _unique_text(self.attempted_autonomous_acquisition, "attempted_autonomous_acquisition", maximum=64),
        )
        for name in ("approval_required", "autonomous_acquisition_available"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be bool")
        for name in ("capability_available", "resource_available"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, bool):
                raise ValueError(f"{name} must be bool|None")
        if self.approval_required and self.blocked_action is None:
            raise ValueError("approval_required requires blocked_action")
        if self.capability_available is not None and self.capability is None:
            raise ValueError("capability_available requires capability")
        if self.resource_available is not None and self.resource is None:
            raise ValueError("resource_available requires resource")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "mission_id": self.mission_id,
            "goal_id": self.goal_id,
            "todo_id": self.todo_id,
            "run_id": self.run_id,
            "campaign_id": self.campaign_id,
            "experiment_id": self.experiment_id,
            "action_id": self.action_id,
            "blocked_action": self.blocked_action,
            "question": self.question,
            "required_fields": list(self.required_fields),
            "approval_required": self.approval_required,
            "capability": self.capability,
            "capability_available": self.capability_available,
            "resource": self.resource,
            "resource_available": self.resource_available,
            "autonomous_acquisition_available": self.autonomous_acquisition_available,
            "attempted_autonomous_acquisition": list(self.attempted_autonomous_acquisition),
            "risk_summary": self.risk_summary,
        }


def classify_human_request_kind(observation: HumanBlockerObservationV1) -> HumanRequestKindV1 | None:
    """Return the single typed human-request category, or ``None``.

    Classification is intentionally strict.  A capability/resource explicitly
    marked available is not a blocker.  Conflicting blocker categories fail
    closed rather than relying on precedence inferred from natural language.
    """

    if not isinstance(observation, HumanBlockerObservationV1):
        raise ValueError("observation must be HumanBlockerObservationV1")

    categories: list[HumanRequestKindV1] = []
    if observation.required_fields:
        categories.append(HumanRequestKindV1.MISSING_INFORMATION)
    if observation.approval_required:
        categories.append(HumanRequestKindV1.APPROVAL_REQUIRED)
    if observation.capability is not None and observation.capability_available is False:
        categories.append(HumanRequestKindV1.CAPABILITY_REQUIRED)
    if observation.resource is not None and observation.resource_available is False:
        categories.append(HumanRequestKindV1.RESOURCE_REQUIRED)

    if len(categories) > 1:
        raise ValueError("blocker observation contains multiple human-request categories")
    return categories[0] if categories else None
