"""Observed-trajectory execution ablations and deterministic decision ablations."""
from __future__ import annotations

from dataclasses import replace
from typing import Iterable

from ..evaluation.models import BenchmarkSuite
from harness.router.advanced_router import RouterV2
from .metrics import aggregate_metrics
from .models import (
    AblationMode,
    AttemptObservation,
    CaseObservation,
    DecisionAblationResult,
    ExecutionAblationResult,
)


def _replay_case(
    record: CaseObservation,
    mode: AblationMode,
) -> CaseObservation:
    if mode not in {
        AblationMode.FULL,
        AblationMode.NO_CASCADE,
        AblationMode.NO_FINALIZATION_GRACE,
        AblationMode.NO_TARGETED_QWEN36,
    }:
        raise ValueError(f"unsupported execution ablation: {mode}")

    attempts: list[AttemptObservation] = []
    effective_verified = False
    latency = 0

    source_attempts = record.attempts[:1] if mode is AblationMode.NO_CASCADE else record.attempts
    for attempt in source_attempts:
        if (
            mode is AblationMode.NO_TARGETED_QWEN36
            and attempt.profile_id == "qwen36_heavy_quality"
        ):
            continue
        effective_attempt_verified = attempt.verified
        if (
            mode is AblationMode.NO_FINALIZATION_GRACE
            and attempt.finalization_calls > 0
        ):
            effective_attempt_verified = False

        effective_attempt = AttemptObservation(
            profile_id=attempt.profile_id,
            verified=effective_attempt_verified,
            latency_ms=attempt.latency_ms,
            model_calls=attempt.model_calls,
            tool_calls=attempt.tool_calls,
            protocol_failures=attempt.protocol_failures,
            tool_failures=attempt.tool_failures,
            finalization_calls=attempt.finalization_calls,
            outcome=attempt.outcome,
        )
        attempts.append(effective_attempt)
        latency += attempt.latency_ms
        if effective_attempt_verified:
            effective_verified = True
            break

    selected = attempts[0].profile_id if attempts else record.selected_profile_id
    return CaseObservation(
        run_key=f"{record.run_key}:{mode.value}",
        source=record.source,
        repetition=record.repetition,
        case_id=record.case_id,
        task_class=record.task_class,
        selected_profile_id=selected,
        attempts=tuple(attempts),
        verified=effective_verified,
        latency_ms=latency,
        infrastructure_invalid=record.infrastructure_invalid,
        router_selection_match=record.router_selection_match,
    )


def execution_ablations(
    records: tuple[CaseObservation, ...],
) -> tuple[ExecutionAblationResult, ...]:
    semantics = {
        AblationMode.FULL:
            "Observed Router V2 execution and recorded cascade outcomes.",
        AblationMode.NO_CASCADE:
            "Observed-trajectory ablation: only the first recorded profile attempt is retained.",
        AblationMode.NO_FINALIZATION_GRACE:
            "Strict observed-trajectory lower bound: a recorded attempt that consumed FINAL-only grace is treated as failed; unobserved alternative attempts are not invented.",
        AblationMode.NO_TARGETED_QWEN36:
            "Observed-trajectory ablation: qwen36_heavy_quality attempts are removed; no broad Qwen3.6 replacement is invented.",
    }
    results = []
    for mode in (
        AblationMode.FULL,
        AblationMode.NO_CASCADE,
        AblationMode.NO_FINALIZATION_GRACE,
        AblationMode.NO_TARGETED_QWEN36,
    ):
        replayed = tuple(_replay_case(record, mode) for record in records)
        results.append(
            ExecutionAblationResult(
                mode,
                semantics[mode],
                aggregate_metrics(replayed),
            )
        )
    return tuple(results)


def _router_for_mode(router: RouterV2, mode: AblationMode) -> RouterV2:
    policy = router.policy
    if mode is AblationMode.FULL:
        return router
    if mode is AblationMode.NO_EMPIRICAL_PRIORS:
        return RouterV2(
            router.registry,
            replace(policy, success_prior_weight=0.0),
        )
    if mode is AblationMode.NO_WARM_BONUS:
        return RouterV2(
            router.registry,
            replace(policy, warm_profile_bonus=0.0),
        )
    if mode is AblationMode.NO_FAILURE_PENALTY:
        return RouterV2(
            router.registry,
            replace(
                policy,
                failure_penalty_per_prior_failure=0.0,
                max_failure_penalty=0.0,
            ),
        )
    if mode is AblationMode.NO_LATENCY_LOAD_AWARENESS:
        return RouterV2(
            router.registry,
            replace(
                policy,
                latency_penalty_weight=0.0,
                load_penalty_weight=0.0,
            ),
        )
    raise ValueError(f"unsupported decision ablation: {mode}")


def decision_ablations(
    *,
    router: RouterV2,
    suite: BenchmarkSuite,
    task_factory,
) -> tuple[DecisionAblationResult, ...]:
    full_selections = {
        case.case_id: router.route(task_factory(case)).selected_profile_id
        for case in suite.cases
    }
    semantics = {
        AblationMode.FULL:
            "Frozen Router V2 scoring and eligibility.",
        AblationMode.NO_EMPIRICAL_PRIORS:
            "Decision-only ablation: empirical success-prior weight is zero; execution is not rerun.",
        AblationMode.NO_WARM_BONUS:
            "Decision-only ablation: warm-profile bonus is zero. Cold benchmark resources normally make this inactive.",
        AblationMode.NO_FAILURE_PENALTY:
            "Decision-only ablation: prior non-infrastructure failure penalty is zero.",
        AblationMode.NO_LATENCY_LOAD_AWARENESS:
            "Decision-only ablation: latency and load-cost weights are zero.",
    }
    result = []
    for mode in (
        AblationMode.FULL,
        AblationMode.NO_EMPIRICAL_PRIORS,
        AblationMode.NO_WARM_BONUS,
        AblationMode.NO_FAILURE_PENALTY,
        AblationMode.NO_LATENCY_LOAD_AWARENESS,
    ):
        variant = _router_for_mode(router, mode)
        selections = {
            case.case_id: variant.route(task_factory(case)).selected_profile_id
            for case in suite.cases
        }
        changes = sum(
            selections[case_id] != full_selections[case_id]
            for case_id in full_selections
        )
        result.append(
            DecisionAblationResult(
                mode=mode,
                semantics=semantics[mode],
                cases=len(suite.cases),
                selection_changes=changes,
                selections=selections,
            )
        )
    return tuple(result)
