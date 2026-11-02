'Pure deterministic advanced routing Router V2 selecting ExecutionProfile IDs.'
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
import math
import yaml

from harness.errors import ConfigurationError
from harness.evaluation.models import BenchmarkDifficulty, BenchmarkTaskClass
from harness.executor.profiles import ExecutionProfile, ExecutionProfileRegistry
from harness.observability.fault_attribution import FaultAttribution, FaultDomain
from harness.router.policy_models import AmbiguityLevel, CascadeStepV2, NavigationNeed, ProfileScoreV2, RouteAction, RouterDecisionV2, TaskProfileV2


@dataclass(frozen=True, slots=True)
class RouterV2Policy:
    hard_vram_limit_gb: float
    minimum_free_vram_reserve_gb: float
    warm_profile_bonus: float
    maximum_warm_bonus_score_gap: float
    success_prior_weight: float
    task_fit_weight: float
    latency_penalty_weight: float
    load_penalty_weight: float
    failure_penalty_per_prior_failure: float
    max_failure_penalty: float
    expected_latency_ms: Mapping[str, int]
    initial_success_priors: Mapping[str, Mapping[str, float]]

    def __post_init__(self) -> None:
        for name in (
            "hard_vram_limit_gb", "minimum_free_vram_reserve_gb", "warm_profile_bonus",
            "maximum_warm_bonus_score_gap", "success_prior_weight", "task_fit_weight",
            "latency_penalty_weight", "load_penalty_weight", "failure_penalty_per_prior_failure",
            "max_failure_penalty",
        ):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)) or value < 0:
                raise ValueError(f"{name} must be finite numeric >= 0")
        if self.hard_vram_limit_gb <= 0:
            raise ValueError("hard_vram_limit_gb must be > 0")
        if not self.expected_latency_ms:
            raise ValueError("expected_latency_ms must be non-empty")

    @classmethod
    def from_project_root(cls, root: Path) -> "RouterV2Policy":
        path = Path(root) / "config" / "router_v2.yaml"
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ConfigurationError(f"cannot read Router V2 config: {exc}") from exc
        except yaml.YAMLError as exc:
            raise ConfigurationError(f"malformed Router V2 YAML: {exc}") from exc
        if not isinstance(raw, dict) or raw.get("schema_version") != 1:
            raise ConfigurationError("router_v2.yaml requires schema_version: 1")
        router = raw.get("router_v2")
        if not isinstance(router, dict):
            raise ConfigurationError("router_v2 section must be mapping")
        resource = router.get("resource_policy")
        scoring = router.get("scoring")
        latencies = router.get("expected_latency_ms")
        priors = router.get("initial_success_priors")
        if not all(isinstance(x, dict) for x in (resource, scoring, latencies, priors)):
            raise ConfigurationError("router_v2 config sections are malformed")
        return cls(
            float(resource["hard_vram_limit_gb"]),
            float(resource["minimum_free_vram_reserve_gb"]),
            float(resource["warm_profile_bonus"]),
            float(resource["maximum_warm_bonus_score_gap"]),
            float(scoring["success_prior_weight"]),
            float(scoring["task_fit_weight"]),
            float(scoring["latency_penalty_weight"]),
            float(scoring["load_penalty_weight"]),
            float(scoring["failure_penalty_per_prior_failure"]),
            float(scoring["max_failure_penalty"]),
            {str(k): int(v) for k, v in latencies.items()},
            {
                str(profile): {str(task): float(value) for task, value in values.items()}
                for profile, values in priors.items()
            },
        )


class RouterV2:
    PROFILE_ORDER = (
        "deepseek_micro",
        "iquest_repo",
        "qwen38_heavy_fast",
        "qwen36_heavy_quality",
    )

    def __init__(self, registry: ExecutionProfileRegistry, policy: RouterV2Policy) -> None:
        if not isinstance(registry, ExecutionProfileRegistry):
            raise ValueError("registry must be ExecutionProfileRegistry")
        if not isinstance(policy, RouterV2Policy):
            raise ValueError("policy must be RouterV2Policy")
        for profile_id in self.PROFILE_ORDER:
            registry.get(profile_id)
            if profile_id not in policy.expected_latency_ms or profile_id not in policy.initial_success_priors:
                raise ValueError(f"Router V2 policy missing profile {profile_id}")
        self.registry = registry
        self.policy = policy

    @classmethod
    def from_project_root(cls, root: Path) -> "RouterV2":
        return cls(
            ExecutionProfileRegistry.from_project_root(Path(root)),
            RouterV2Policy.from_project_root(Path(root)),
        )

    def _success_prior(self, profile_id: str, task: TaskProfileV2) -> float:
        values = self.policy.initial_success_priors[profile_id]
        return float(values.get(task.task_class.value, values.get("default", 0.5)))

    @staticmethod
    def _task_fit(profile_id: str, task: TaskProfileV2) -> float:
        tc = task.task_class
        if profile_id == "deepseek_micro":
            mapping = {
                BenchmarkTaskClass.SIMPLE_EDIT: 1.00,
                BenchmarkTaskClass.SINGLE_FILE_BUG: 0.92,
                BenchmarkTaskClass.FAILURE_RETRY: 0.70,
                BenchmarkTaskClass.PARALLEL_SAFE: 0.92,
                BenchmarkTaskClass.REFACTOR: 0.45,
            }
            fit = mapping.get(tc, 0.24)
            if not task.targeted_known_file_task or task.estimated_file_count > 1:
                fit -= 0.35
            if task.context_tokens > 4096:
                fit -= 0.20
            return max(0.0, min(1.0, fit))

        if profile_id == "iquest_repo":
            mapping = {
                BenchmarkTaskClass.REPO_NAVIGATION: 1.00,
                BenchmarkTaskClass.REFACTOR: 0.98,
                BenchmarkTaskClass.DEPENDENCY_DAG: 0.94,
                BenchmarkTaskClass.PARALLEL_SAFE: 0.94,
                BenchmarkTaskClass.MULTI_FILE_CHANGE: 0.90,
                BenchmarkTaskClass.FAILURE_RETRY: 0.90,
                BenchmarkTaskClass.SINGLE_FILE_BUG: 0.84,
                BenchmarkTaskClass.SIMPLE_EDIT: 0.72,
                BenchmarkTaskClass.HARD_DEBUGGING: 0.56,
                BenchmarkTaskClass.FAILURE_ESCALATION: 0.44,
            }
            fit = mapping.get(tc, 0.65)
            if task.navigation_need is NavigationNeed.HIGH:
                fit += 0.04
            return max(0.0, min(1.0, fit))

        if profile_id == "qwen38_heavy_fast":
            mapping = {
                BenchmarkTaskClass.HARD_DEBUGGING: 1.00,
                BenchmarkTaskClass.FAILURE_ESCALATION: 1.00,
                BenchmarkTaskClass.MULTI_FILE_CHANGE: 0.72,
                BenchmarkTaskClass.REFACTOR: 0.84,
                BenchmarkTaskClass.DEPENDENCY_DAG: 0.82,
                BenchmarkTaskClass.REPO_NAVIGATION: 0.68,
                BenchmarkTaskClass.FAILURE_RETRY: 0.78,
            }
            fit = mapping.get(tc, 0.48)
            if tc is BenchmarkTaskClass.MULTI_FILE_CHANGE and task.difficulty in {BenchmarkDifficulty.HARD, BenchmarkDifficulty.VERY_HARD, BenchmarkDifficulty.CRITICAL}:
                fit += 0.12
            if task.difficulty in {BenchmarkDifficulty.VERY_HARD, BenchmarkDifficulty.CRITICAL}:
                fit += 0.08
            return max(0.0, min(1.0, fit))

        if profile_id == "qwen36_heavy_quality":
            if not task.targeted_known_file_task:
                return 0.0
            mapping = {
                BenchmarkTaskClass.HARD_DEBUGGING: 1.00,
                BenchmarkTaskClass.FAILURE_ESCALATION: 1.00,
                BenchmarkTaskClass.MULTI_FILE_CHANGE: 0.96,
                BenchmarkTaskClass.REFACTOR: 0.94,
                BenchmarkTaskClass.DEPENDENCY_DAG: 0.92,
                BenchmarkTaskClass.FAILURE_RETRY: 0.86,
                BenchmarkTaskClass.SINGLE_FILE_BUG: 0.78,
            }
            return mapping.get(tc, 0.62)
        raise KeyError(profile_id)

    def _eligibility(self, profile: ExecutionProfile, task: TaskProfileV2) -> tuple[bool, tuple[str, ...]]:
        reasons: list[str] = []
        if not task.resources.worker_online:
            reasons.append("worker_offline")
        if profile.estimated_vram_gb > self.policy.hard_vram_limit_gb:
            reasons.append("profile_exceeds_hard_vram_limit")
        loaded = profile.profile_id in task.resources.loaded_profile_ids
        if not loaded and task.resources.free_vram_gb is not None:
            available = float(task.resources.free_vram_gb) + float(task.resources.reclaimable_vram_gb)
            required = profile.estimated_vram_gb + self.policy.minimum_free_vram_reserve_gb
            if available + 1e-9 < required:
                reasons.append("insufficient_vram_for_load_or_switch")
        if profile.profile_id == "deepseek_micro":
            if not task.targeted_known_file_task:
                reasons.append("deepseek_requires_known_target_file")
            if task.estimated_file_count > 1:
                reasons.append("deepseek_micro_file_count_gt_1")
            if task.difficulty in {BenchmarkDifficulty.HARD, BenchmarkDifficulty.VERY_HARD, BenchmarkDifficulty.CRITICAL}:
                reasons.append("deepseek_micro_difficulty_too_high")
        if profile.profile_id == "qwen36_heavy_quality":
            if not task.targeted_known_file_task:
                reasons.append("qwen36_targeted_profile_requires_known_files_and_low_navigation")
            if task.estimated_file_count > 4:
                reasons.append("qwen36_targeted_file_count_gt_4")
            has_noninfra_prior_failure = any(
                not failure.infrastructure_invalid
                and failure.attribution is not FaultAttribution.INFRASTRUCTURE
                for failure in task.previous_failures
            )
            if not has_noninfra_prior_failure:
                reasons.append("qwen36_heavy_quality_is_fallback_only_without_prior_failure")
        return not reasons, tuple(reasons)

    def _failure_penalty(self, profile_id: str, task: TaskProfileV2) -> float:
        count = 0
        for failure in task.previous_failures:
            if failure.infrastructure_invalid or failure.attribution is FaultAttribution.INFRASTRUCTURE:
                continue
            if failure.profile_id == profile_id:
                count += 1
        return min(self.policy.max_failure_penalty, count * self.policy.failure_penalty_per_prior_failure)

    def _candidate(self, profile_id: str, task: TaskProfileV2) -> ProfileScoreV2:
        profile = self.registry.get(profile_id)
        eligible, reasons = self._eligibility(profile, task)
        prior = self._success_prior(profile_id, task)
        fit = self._task_fit(profile_id, task)
        max_latency = max(self.policy.expected_latency_ms.values())
        latency_penalty = self.policy.expected_latency_ms[profile_id] / max_latency
        loaded = profile_id in task.resources.loaded_profile_ids
        load_penalty = 0.0 if loaded else 1.0
        failure_penalty = self._failure_penalty(profile_id, task)
        base = (
            self.policy.success_prior_weight * prior
            + self.policy.task_fit_weight * fit
            - self.policy.latency_penalty_weight * latency_penalty
            - self.policy.load_penalty_weight * load_penalty
            - failure_penalty
        )
        return ProfileScoreV2(
            profile_id,
            eligible,
            prior,
            fit,
            latency_penalty,
            load_penalty,
            failure_penalty,
            0.0,
            base if eligible else -1.0,
            reasons,
        )

    def _apply_warm_bonus(self, candidates: list[ProfileScoreV2], task: TaskProfileV2) -> list[ProfileScoreV2]:
        eligible = [item for item in candidates if item.eligible]
        if not eligible or not task.resources.loaded_profile_ids:
            return candidates
        best = max(item.total_score for item in eligible)
        result: list[ProfileScoreV2] = []
        for item in candidates:
            bonus = 0.0
            if (
                item.eligible
                and item.profile_id in task.resources.loaded_profile_ids
                and best - item.total_score <= self.policy.maximum_warm_bonus_score_gap
            ):
                bonus = self.policy.warm_profile_bonus
            result.append(
                ProfileScoreV2(
                    item.profile_id,
                    item.eligible,
                    item.success_prior,
                    item.task_fit,
                    item.latency_penalty,
                    item.load_penalty,
                    item.failure_penalty,
                    bonus,
                    item.total_score + bonus if item.eligible else item.total_score,
                    item.rejection_reasons,
                )
            )
        return result

    @staticmethod
    def _cascade(selected: str, task: TaskProfileV2) -> tuple[CascadeStepV2, ...]:
        chains = {
            "deepseek_micro": ["iquest_repo", "qwen38_heavy_fast"],
            "iquest_repo": ["qwen38_heavy_fast"],
            "qwen38_heavy_fast": [],
            "qwen36_heavy_quality": [],
        }
        profiles = list(chains[selected])
        if selected != "qwen36_heavy_quality" and task.targeted_known_file_task and "qwen36_heavy_quality" not in profiles:
            profiles.append("qwen36_heavy_quality")
        steps = [
            CascadeStepV2(RouteAction.PROFILE_ATTEMPT, profile_id, "verification/implementation fallback")
            for profile_id in profiles
        ]
        steps.extend(
            [
                CascadeStepV2(RouteAction.MINISTRAL_CLASSIFY, None, "classify repeated heavy-worker failure"),
                CascadeStepV2(RouteAction.ORNITH_REPLAN, None, "replan after classified heavy fallback failure"),
            ]
        )
        return tuple(steps)

    def route(self, task: TaskProfileV2) -> RouterDecisionV2:
        if not isinstance(task, TaskProfileV2):
            raise ValueError("task must be TaskProfileV2")
        candidates = [self._candidate(profile_id, task) for profile_id in self.PROFILE_ORDER]
        candidates = self._apply_warm_bonus(candidates, task)
        candidates.sort(key=lambda item: (-item.total_score, self.PROFILE_ORDER.index(item.profile_id)))
        eligible = [item for item in candidates if item.eligible]
        if not eligible:
            return RouterDecisionV2(
                RouteAction.NO_ELIGIBLE_PROFILE,
                None,
                None,
                0.0,
                "no execution profile passes task/resource eligibility",
                tuple(candidates),
                (),
                ("orchestrator_attention_required",),
            )
        selected = eligible[0]
        second = eligible[1] if len(eligible) > 1 else None
        margin = selected.total_score - (second.total_score if second is not None else 0.0)
        confidence = max(0.05, min(0.99, 0.55 + margin))
        warnings: list[str] = []
        if task.ambiguity is AmbiguityLevel.HIGH:
            warnings.append("high_task_ambiguity")
        if task.navigation_need is NavigationNeed.HIGH and selected.profile_id == "qwen36_heavy_quality":
            warnings.append("targeted_profile_navigation_mismatch")
        return RouterDecisionV2(
            RouteAction.PROFILE_ATTEMPT,
            selected.profile_id,
            selected.total_score,
            confidence,
            (
                f"selected {selected.profile_id}: prior={selected.success_prior:.2f}, "
                f"fit={selected.task_fit:.2f}, failure_penalty={selected.failure_penalty:.2f}"
            ),
            tuple(candidates),
            self._cascade(selected.profile_id, task),
            tuple(warnings),
        )
