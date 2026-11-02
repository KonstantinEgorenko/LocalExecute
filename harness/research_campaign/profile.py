'research diagnostics.1 Generic Research Campaign Profile and Budget Policy.'
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from .contracts import MetricDirectionV1, ResearchBudgetV1
from .search import StrategyDimensionTargetV1, StrategySpaceV1
from .serialization import (
    freeze_json,
    non_negative_int,
    non_negative_number,
    plain,
    positive_int,
    safe_id,
    stable_sha256,
    text,
)


def _validate_relative_path(path_str: str, name: str) -> str:
    if not isinstance(path_str, str) or not path_str.strip():
        raise ValueError(f"{name} must be a non-empty path string")
    clean = path_str.strip().replace("\\", "/")
    if clean.startswith("/") or ":" in clean:
        raise ValueError(f"{name} '{path_str}' must be a relative path without drive or root")
    parts = clean.split("/")
    if any(p == ".." or p == "." for p in parts if p):
        raise ValueError(f"{name} '{path_str}' contains forbidden path traversal segment")
    normalized = "/".join(p for p in parts if p)
    if not normalized:
        raise ValueError(f"{name} '{path_str}' resolved to empty path")
    return normalized


def compute_file_sha256(target_root: Path, rel_path: str) -> str:
    target_root = Path(target_root).resolve(strict=True)
    norm_rel = _validate_relative_path(rel_path, "rel_path")
    full_path = (target_root / norm_rel).resolve()
    try:
        full_path.relative_to(target_root)
    except ValueError as exc:
        raise ValueError(f"path '{rel_path}' escapes target root '{target_root}'") from exc
    if not full_path.exists():
        raise ValueError(f"fingerprint reference does not exist: {rel_path}")
    if not full_path.is_file():
        raise ValueError(f"fingerprint reference is not a regular file: {rel_path}")
    return hashlib.sha256(full_path.read_bytes()).hexdigest()


def compute_dataset_fingerprint(target_root: Path, dataset_refs: Sequence[str]) -> str:
    if not dataset_refs:
        raise ValueError("dataset_refs must not be empty")
    entries = []
    for ref in sorted(set(dataset_refs)):
        h = compute_file_sha256(target_root, ref)
        entries.append({"path": ref, "sha256": h})
    return stable_sha256(entries)


def compute_split_fingerprint(target_root: Path, research_split_ref: str) -> str:
    """Computes SHA-256 of the contents of target_root / research_split_ref."""
    return compute_file_sha256(target_root, research_split_ref)


def compute_scorer_fingerprint(target_root: Path, scorer_refs: Sequence[str]) -> str:
    if not scorer_refs:
        raise ValueError("scorer_refs must not be empty")
    entries = []
    for ref in sorted(set(scorer_refs)):
        h = compute_file_sha256(target_root, ref)
        entries.append({"path": ref, "sha256": h})
    return stable_sha256(entries)


@dataclass(frozen=True, slots=True)
class ResearchBudgetPolicyV1:
    """Immutable budget policy for a generic research campaign."""

    wall_time_seconds: int = 86400
    max_experiments: int = 10
    max_gpu_seconds: float = 3600.0
    max_api_cost: float = 100.0
    max_metric_calls: int = 100
    max_external_research_rounds: int = 1
    max_consecutive_crashes: int = 3
    max_no_improvement_window: int = 5

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "wall_time_seconds",
            positive_int(self.wall_time_seconds, "wall_time_seconds"),
        )
        object.__setattr__(
            self,
            "max_experiments",
            positive_int(self.max_experiments, "max_experiments"),
        )
        object.__setattr__(
            self,
            "max_gpu_seconds",
            non_negative_number(self.max_gpu_seconds, "max_gpu_seconds"),
        )
        object.__setattr__(
            self,
            "max_api_cost",
            non_negative_number(self.max_api_cost, "max_api_cost"),
        )
        object.__setattr__(
            self,
            "max_metric_calls",
            positive_int(self.max_metric_calls, "max_metric_calls"),
        )
        object.__setattr__(
            self,
            "max_external_research_rounds",
            positive_int(
                self.max_external_research_rounds, "max_external_research_rounds"
            ),
        )
        object.__setattr__(
            self,
            "max_consecutive_crashes",
            positive_int(
                self.max_consecutive_crashes, "max_consecutive_crashes"
            ),
        )
        object.__setattr__(
            self,
            "max_no_improvement_window",
            positive_int(
                self.max_no_improvement_window, "max_no_improvement_window"
            ),
        )

    def materialize_budget(self, start_iso: str) -> ResearchBudgetV1:
        dt = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
        deadline_dt = dt + timedelta(seconds=self.wall_time_seconds)
        wall_deadline = (
            deadline_dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        )
        return ResearchBudgetV1(
            wall_deadline=wall_deadline,
            max_experiments=self.max_experiments,
            max_gpu_seconds=float(self.max_gpu_seconds),
            max_api_cost=float(self.max_api_cost),
            max_metric_calls=self.max_metric_calls,
            max_external_research_rounds=self.max_external_research_rounds,
            max_consecutive_crashes=self.max_consecutive_crashes,
            max_no_improvement_window=self.max_no_improvement_window,
        )

    def validate_compatible(self, persisted_budget: ResearchBudgetV1) -> None:
        mismatches: list[str] = []
        if persisted_budget.max_experiments != self.max_experiments:
            mismatches.append(
                f"max_experiments {persisted_budget.max_experiments} != {self.max_experiments}"
            )
        if abs(persisted_budget.max_gpu_seconds - float(self.max_gpu_seconds)) > 1e-6:
            mismatches.append(
                f"max_gpu_seconds {persisted_budget.max_gpu_seconds} != {self.max_gpu_seconds}"
            )
        if abs(persisted_budget.max_api_cost - float(self.max_api_cost)) > 1e-6:
            mismatches.append(
                f"max_api_cost {persisted_budget.max_api_cost} != {self.max_api_cost}"
            )
        if persisted_budget.max_metric_calls != self.max_metric_calls:
            mismatches.append(
                f"max_metric_calls {persisted_budget.max_metric_calls} != {self.max_metric_calls}"
            )
        if (
            persisted_budget.max_external_research_rounds
            != self.max_external_research_rounds
        ):
            mismatches.append(
                f"max_external_research_rounds {persisted_budget.max_external_research_rounds} != {self.max_external_research_rounds}"
            )
        if (
            persisted_budget.max_consecutive_crashes
            != self.max_consecutive_crashes
        ):
            mismatches.append(
                f"max_consecutive_crashes {persisted_budget.max_consecutive_crashes} != {self.max_consecutive_crashes}"
            )
        if (
            persisted_budget.max_no_improvement_window
            != self.max_no_improvement_window
        ):
            mismatches.append(
                f"max_no_improvement_window {persisted_budget.max_no_improvement_window} != {self.max_no_improvement_window}"
            )
        if mismatches:
            raise ValueError(
                f"persisted budget incompatible with requested policy: {', '.join(mismatches)}"
            )


@dataclass(frozen=True, slots=True)
class CommandMetricEvaluationConfigV1:
    'Configuration for command metric evaluation port.\n\n    TRUST BOUNDARY:\n    research diagnostics.1 profiles are trusted code-constructed configurations only.\n    Arbitrary evaluation commands must not be loaded from untrusted external YAML,\n    the target repository itself, or LLM output.\n    '

    verification_argv: tuple[str, ...]
    evaluation_argv: tuple[str, ...]
    metric_json_key: str = "metric"
    research_split: str = "DEV"
    timeout_seconds: int = 300
    expected_config_path: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.verification_argv, tuple) or any(
            not isinstance(x, str) or not x.strip() for x in self.verification_argv
        ):
            raise ValueError("verification_argv must be tuple of non-empty strings")
        if (
            not isinstance(self.evaluation_argv, tuple)
            or not self.evaluation_argv
            or any(not isinstance(x, str) or not x.strip() for x in self.evaluation_argv)
        ):
            raise ValueError("evaluation_argv must be non-empty tuple of non-empty strings")
        safe_id(self.metric_json_key, "metric_json_key")
        safe_id(self.research_split, "research_split")
        object.__setattr__(
            self,
            "timeout_seconds",
            positive_int(self.timeout_seconds, "timeout_seconds"),
        )
        if self.expected_config_path is not None:
            _validate_relative_path(self.expected_config_path, "expected_config_path")


@dataclass(frozen=True, slots=True)
class ResearchCampaignProfileV1:
    """Declarative reusable domain mechanics for an autonomous research campaign."""

    profile_id: str
    metric_name: str
    metric_direction: MetricDirectionV1
    strategy_space: StrategySpaceV1
    baseline_parameters: Mapping[str, Any]
    dataset_refs: tuple[str, ...]
    research_split_ref: str
    scorer_refs: tuple[str, ...]
    allowed_mutation_scope: tuple[str, ...]
    forbidden_mutation_scope: tuple[str, ...]
    evaluation_config: CommandMetricEvaluationConfigV1
    metric_version: str = "1"
    protected_no_feedback_paths: tuple[str, ...] = ()
    execution_goal_capsule: str = ""
    execution_global_constraints: Mapping[str, Any] = field(default_factory=dict)
    budget_policy: ResearchBudgetPolicyV1 = field(default_factory=ResearchBudgetPolicyV1)
    data_split_policy: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        safe_id(self.profile_id, "profile_id")
        safe_id(self.metric_name, "metric_name")
        text(self.metric_version, "metric_version", max_length=64)
        if not isinstance(self.metric_direction, MetricDirectionV1):
            raise ValueError("metric_direction must be MetricDirectionV1")
        if not isinstance(self.strategy_space, StrategySpaceV1):
            raise ValueError("strategy_space must be StrategySpaceV1")
        if not self.strategy_space.dimensions:
            raise ValueError("strategy_space must contain at least one dimension")

        # Validate baseline_parameters against PARAMETER dimensions
        param_dims = [
            dim
            for dim in self.strategy_space.dimensions
            if dim.target == StrategyDimensionTargetV1.PARAMETER
        ]
        param_keys = {dim.key for dim in param_dims if dim.key is not None}
        frozen_baseline = freeze_json(self.baseline_parameters, "baseline_parameters")
        if not isinstance(frozen_baseline, Mapping):
            raise ValueError("baseline_parameters must be a mapping")
        baseline_keys = set(frozen_baseline.keys())
        if param_keys and baseline_keys != param_keys:
            raise ValueError(
                f"baseline_parameters keys {baseline_keys!r} must match PARAMETER dimensions {param_keys!r}"
            )
        object.__setattr__(self, "baseline_parameters", frozen_baseline)

        # Validate paths
        norm_dataset = tuple(
            _validate_relative_path(p, "dataset_refs") for p in self.dataset_refs
        )
        if not norm_dataset:
            raise ValueError("dataset_refs must not be empty")
        if len(set(norm_dataset)) != len(norm_dataset):
            raise ValueError("dataset_refs contains duplicate paths")
        object.__setattr__(self, "dataset_refs", norm_dataset)

        norm_split_ref = _validate_relative_path(self.research_split_ref, "research_split_ref")
        object.__setattr__(self, "research_split_ref", norm_split_ref)

        norm_scorer = tuple(
            _validate_relative_path(p, "scorer_refs") for p in self.scorer_refs
        )
        if not norm_scorer:
            raise ValueError("scorer_refs must not be empty")
        if len(set(norm_scorer)) != len(norm_scorer):
            raise ValueError("scorer_refs contains duplicate paths")
        object.__setattr__(self, "scorer_refs", norm_scorer)

        norm_allowed = tuple(
            _validate_relative_path(p, "allowed_mutation_scope")
            for p in self.allowed_mutation_scope
        )
        if not norm_allowed:
            raise ValueError("allowed_mutation_scope must not be empty")
        if len(set(norm_allowed)) != len(norm_allowed):
            raise ValueError("allowed_mutation_scope contains duplicate paths")
        object.__setattr__(self, "allowed_mutation_scope", norm_allowed)

        norm_forbidden = tuple(
            _validate_relative_path(p, "forbidden_mutation_scope")
            for p in self.forbidden_mutation_scope
        )
        if len(set(norm_forbidden)) != len(norm_forbidden):
            raise ValueError("forbidden_mutation_scope contains duplicate paths")
        object.__setattr__(self, "forbidden_mutation_scope", norm_forbidden)

        norm_protected = tuple(
            _validate_relative_path(p, "protected_no_feedback_paths")
            for p in self.protected_no_feedback_paths
        )
        if len(set(norm_protected)) != len(norm_protected):
            raise ValueError("protected_no_feedback_paths contains duplicate paths")
        object.__setattr__(self, "protected_no_feedback_paths", norm_protected)

        # Protect dataset / split / scorer / protected / forbidden authority:
        allowed_set = set(norm_allowed)
        dataset_overlap = allowed_set.intersection(set(norm_dataset))
        if dataset_overlap:
            raise ValueError(
                f"allowed_mutation_scope overlaps with dataset_refs: {dataset_overlap!r}"
            )

        scorer_overlap = allowed_set.intersection(set(norm_scorer))
        if scorer_overlap:
            raise ValueError(
                f"allowed_mutation_scope overlaps with scorer_refs: {scorer_overlap!r}"
            )

        if norm_split_ref in allowed_set:
            raise ValueError(
                f"allowed_mutation_scope overlaps with research_split_ref: {norm_split_ref!r}"
            )

        prot_overlap = allowed_set.intersection(set(norm_protected))
        if prot_overlap:
            raise ValueError(
                f"allowed_mutation_scope overlaps with protected_no_feedback_paths: {prot_overlap!r}"
            )

        forb_overlap = allowed_set.intersection(set(norm_forbidden))
        if forb_overlap:
            raise ValueError(
                f"allowed_mutation_scope overlaps with forbidden_mutation_scope: {forb_overlap!r}"
            )

        if not isinstance(self.evaluation_config, CommandMetricEvaluationConfigV1):
            raise ValueError(
                "evaluation_config must be CommandMetricEvaluationConfigV1"
            )
        if not isinstance(self.budget_policy, ResearchBudgetPolicyV1):
            raise ValueError("budget_policy must be ResearchBudgetPolicyV1")

        object.__setattr__(
            self,
            "execution_global_constraints",
            freeze_json(
                self.execution_global_constraints, "execution_global_constraints"
            ),
        )
        object.__setattr__(
            self,
            "data_split_policy",
            freeze_json(self.data_split_policy, "data_split_policy"),
        )

    @property
    def profile_fingerprint(self) -> str:
        dim_payload = [
            {
                "name": dim.name,
                "target": dim.target.value,
                "key": dim.key,
                "choices": [
                    {
                        "value": plain(c.value),
                        "priority": c.priority,
                        "estimated_cost": c.estimated_cost,
                    }
                    for c in dim.choices
                ],
            }
            for dim in self.strategy_space.dimensions
        ]
        excl_payload = [
            {"matches": plain(ex.matches), "reason": ex.reason}
            for ex in self.strategy_space.exclusions
        ]
        payload = {
            "profile_id": self.profile_id,
            "metric_name": self.metric_name,
            "metric_version": self.metric_version,
            "metric_direction": self.metric_direction.value,
            "strategy_space": {
                "dimensions": dim_payload,
                "exclusions": excl_payload,
                "max_candidates": self.strategy_space.max_candidates,
            },
            "baseline_parameters": plain(self.baseline_parameters),
            "dataset_refs": list(self.dataset_refs),
            "research_split_ref": self.research_split_ref,
            "scorer_refs": list(self.scorer_refs),
            "allowed_mutation_scope": list(self.allowed_mutation_scope),
            "forbidden_mutation_scope": list(self.forbidden_mutation_scope),
            "protected_no_feedback_paths": list(self.protected_no_feedback_paths),
            "evaluation_config": {
                "verification_argv": list(self.evaluation_config.verification_argv),
                "evaluation_argv": list(self.evaluation_config.evaluation_argv),
                "metric_json_key": self.evaluation_config.metric_json_key,
                "research_split": self.evaluation_config.research_split,
                "timeout_seconds": self.evaluation_config.timeout_seconds,
                "expected_config_path": self.evaluation_config.expected_config_path,
            },
            "execution_goal_capsule": self.execution_goal_capsule,
            "execution_global_constraints": plain(self.execution_global_constraints),
            "budget_policy": {
                "wall_time_seconds": self.budget_policy.wall_time_seconds,
                "max_experiments": self.budget_policy.max_experiments,
                "max_gpu_seconds": float(self.budget_policy.max_gpu_seconds),
                "max_api_cost": float(self.budget_policy.max_api_cost),
                "max_metric_calls": self.budget_policy.max_metric_calls,
                "max_external_research_rounds": self.budget_policy.max_external_research_rounds,
                "max_consecutive_crashes": self.budget_policy.max_consecutive_crashes,
                "max_no_improvement_window": self.budget_policy.max_no_improvement_window,
            },
            "data_split_policy": plain(self.data_split_policy),
        }
        return stable_sha256(payload)
