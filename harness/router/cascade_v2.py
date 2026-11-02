'Failure-aware deterministic cascade decisions for advanced routing.'
from __future__ import annotations

from dataclasses import dataclass

from harness.observability.fault_attribution import FaultAttribution, FaultCode, FaultDomain
from harness.router.policy_models import CascadeStepV2, FailureSignalV2, RouteAction, RouterDecisionV2


@dataclass(frozen=True, slots=True)
class CascadeCursorV2:
    selected_profile_id: str
    attempted_profile_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.selected_profile_id, str) or not self.selected_profile_id.strip():
            raise ValueError("selected_profile_id must be non-empty string")
        if not isinstance(self.attempted_profile_ids, tuple) or any(
            not isinstance(item, str) or not item.strip() for item in self.attempted_profile_ids
        ):
            raise ValueError("attempted_profile_ids must be tuple[str, ...]")
        if len(self.attempted_profile_ids) != len(set(self.attempted_profile_ids)):
            raise ValueError("attempted_profile_ids must be unique")


class CascadeControllerV2:
    """Map classified failures to the next orchestration action.

    Router V2 never executes a model itself. It returns an action/profile for the
    orchestrator, preserving the frozen Control Plane -> Orchestrator ownership.
    """

    @staticmethod
    def _next_fallback(decision: RouterDecisionV2, attempted: tuple[str, ...]) -> CascadeStepV2:
        for step in decision.cascade:
            if step.action is RouteAction.PROFILE_ATTEMPT and step.profile_id not in attempted:
                return step
        for step in decision.cascade:
            if step.action is RouteAction.MINISTRAL_CLASSIFY:
                return step
        return CascadeStepV2(RouteAction.ORNITH_REPLAN, None, "no untried execution profile remains")

    def next_step(
        self,
        *,
        decision: RouterDecisionV2,
        cursor: CascadeCursorV2,
        failure: FailureSignalV2,
    ) -> CascadeStepV2:
        if not isinstance(decision, RouterDecisionV2):
            raise ValueError("decision must be RouterDecisionV2")
        if not isinstance(cursor, CascadeCursorV2):
            raise ValueError("cursor must be CascadeCursorV2")
        if not isinstance(failure, FailureSignalV2):
            raise ValueError("failure must be FailureSignalV2")

        if failure.infrastructure_invalid or failure.attribution is FaultAttribution.INFRASTRUCTURE or failure.domain is FaultDomain.INFRASTRUCTURE:
            return CascadeStepV2(
                RouteAction.RETRY_SAME_PROFILE,
                cursor.selected_profile_id,
                "infrastructure failure is neutral to model quality",
            )

        if failure.code is FaultCode.FINALIZATION_CANDIDATE:
            return CascadeStepV2(
                RouteAction.FINALIZATION_GRACE,
                cursor.selected_profile_id,
                "candidate implementation is complete enough for bounded FINAL-only grace",
            )

        if failure.code is FaultCode.TOOL_WRITE_CONFLICT:
            return CascadeStepV2(
                RouteAction.RETRY_SAME_PROFILE,
                cursor.selected_profile_id,
                "stale write requires fresh read/retry in the same profile",
            )

        if failure.domain is FaultDomain.CONTEXT or failure.code is FaultCode.CONTEXT_INSUFFICIENT:
            return CascadeStepV2(
                RouteAction.EXPAND_CONTEXT,
                cursor.selected_profile_id,
                "Phase 10 context expansion required before changing model profile",
            )

        if failure.domain in {
            FaultDomain.VERIFICATION,
            FaultDomain.IMPLEMENTATION,
            FaultDomain.PROTOCOL,
            FaultDomain.LOOP,
            FaultDomain.QUOTA,
            FaultDomain.TOOL_CONTRACT,
            FaultDomain.TOOL_POLICY,
        }:
            return self._next_fallback(decision, cursor.attempted_profile_ids)

        return self._next_fallback(decision, cursor.attempted_profile_ids)
