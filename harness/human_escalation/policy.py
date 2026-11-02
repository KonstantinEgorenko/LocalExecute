'Out-of-band capability and human-escalation policy for research campaign.R0.'
from __future__ import annotations

from dataclasses import dataclass

from .classifier import HumanBlockerObservationV1, classify_human_request_kind
from .models import CapabilityGateDecisionV1, CapabilityGateResultV1, HumanRequestKindV1, _text, _unique_text


@dataclass(frozen=True, slots=True)
class HumanEscalationPolicyV1:
    """Deterministic authority deciding ALLOW / ASK / DENY.

    ``hard_denied_*`` identifiers are exact, harness-owned policy identifiers.
    They are never converted into owner approval requests.  Missing information
    may first take a bounded autonomous-acquisition path; only after that path is
    unavailable/exhausted does the policy return ``ASK``.
    """

    max_autonomous_acquisition_attempts: int = 2
    hard_denied_action_ids: tuple[str, ...] = ()
    hard_denied_capabilities: tuple[str, ...] = ()
    allow_human_requests: bool = True

    def __post_init__(self) -> None:
        if (
            not isinstance(self.max_autonomous_acquisition_attempts, int)
            or isinstance(self.max_autonomous_acquisition_attempts, bool)
            or self.max_autonomous_acquisition_attempts < 0
        ):
            raise ValueError("max_autonomous_acquisition_attempts must be integer >= 0")
        object.__setattr__(
            self,
            "hard_denied_action_ids",
            _unique_text(self.hard_denied_action_ids, "hard_denied_action_ids", maximum=256),
        )
        object.__setattr__(
            self,
            "hard_denied_capabilities",
            _unique_text(self.hard_denied_capabilities, "hard_denied_capabilities", maximum=256),
        )
        if not isinstance(self.allow_human_requests, bool):
            raise ValueError("allow_human_requests must be bool")

    def evaluate(self, observation: HumanBlockerObservationV1) -> CapabilityGateResultV1:
        if not isinstance(observation, HumanBlockerObservationV1):
            raise ValueError("observation must be HumanBlockerObservationV1")

        if observation.action_id is not None and observation.action_id in self.hard_denied_action_ids:
            return CapabilityGateResultV1(
                CapabilityGateDecisionV1.DENY,
                f"hard policy deny for action_id={observation.action_id}",
            )
        if observation.capability is not None and observation.capability in self.hard_denied_capabilities:
            return CapabilityGateResultV1(
                CapabilityGateDecisionV1.DENY,
                f"hard policy deny for capability={observation.capability}",
            )

        kind = classify_human_request_kind(observation)
        if kind is None:
            return CapabilityGateResultV1(CapabilityGateDecisionV1.ALLOW, "no human-owned blocker")

        if (
            kind is HumanRequestKindV1.MISSING_INFORMATION
            and observation.autonomous_acquisition_available
            and len(observation.attempted_autonomous_acquisition) < self.max_autonomous_acquisition_attempts
        ):
            return CapabilityGateResultV1(
                CapabilityGateDecisionV1.ALLOW,
                "bounded autonomous acquisition must be attempted before asking owner",
            )

        if not self.allow_human_requests:
            return CapabilityGateResultV1(
                CapabilityGateDecisionV1.DENY,
                "human requests disabled by policy",
            )

        reasons = {
            HumanRequestKindV1.MISSING_INFORMATION: "required information unavailable after bounded autonomous acquisition",
            HumanRequestKindV1.APPROVAL_REQUIRED: "owner approval required by policy",
            HumanRequestKindV1.CAPABILITY_REQUIRED: "required capability is unavailable and cannot be assumed",
            HumanRequestKindV1.RESOURCE_REQUIRED: "required resource is unavailable and cannot be assumed",
        }
        return CapabilityGateResultV1(CapabilityGateDecisionV1.ASK, reasons[kind], kind)
