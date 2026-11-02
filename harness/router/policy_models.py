'Immutable contracts for advanced routing Router V2 and failure-aware cascades.'
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from harness.evaluation.models import BenchmarkDifficulty, BenchmarkTaskClass
from harness.observability.fault_attribution import FaultAnalysis, FaultAttribution, FaultCode, FaultDomain, SampleValidity


class NavigationNeed(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class AmbiguityLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RouteAction(StrEnum):
    PROFILE_ATTEMPT = "PROFILE_ATTEMPT"
    RETRY_SAME_PROFILE = "RETRY_SAME_PROFILE"
    FINALIZATION_GRACE = "FINALIZATION_GRACE"
    EXPAND_CONTEXT = "EXPAND_CONTEXT"
    MINISTRAL_CLASSIFY = "MINISTRAL_CLASSIFY"
    ORNITH_REPLAN = "ORNITH_REPLAN"
    NO_ELIGIBLE_PROFILE = "NO_ELIGIBLE_PROFILE"


@dataclass(frozen=True, slots=True)
class ResourceStateV2:
    worker_online: bool
    free_vram_gb: float | None
    reclaimable_vram_gb: float
    loaded_profile_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.worker_online, bool):
            raise ValueError("worker_online must be bool")
        if self.free_vram_gb is not None:
            if not isinstance(self.free_vram_gb, (int, float)) or isinstance(self.free_vram_gb, bool) or self.free_vram_gb < 0:
                raise ValueError("free_vram_gb must be numeric >= 0 or None")
        if not isinstance(self.reclaimable_vram_gb, (int, float)) or isinstance(self.reclaimable_vram_gb, bool) or self.reclaimable_vram_gb < 0:
            raise ValueError("reclaimable_vram_gb must be numeric >= 0")
        if not isinstance(self.loaded_profile_ids, tuple) or any(
            not isinstance(item, str) or not item.strip() for item in self.loaded_profile_ids
        ):
            raise ValueError("loaded_profile_ids must be tuple[str, ...]")
        if len(self.loaded_profile_ids) != len(set(self.loaded_profile_ids)):
            raise ValueError("loaded_profile_ids must be unique")


@dataclass(frozen=True, slots=True)
class FailureSignalV2:
    domain: FaultDomain
    code: FaultCode
    attribution: FaultAttribution
    profile_id: str | None = None
    infrastructure_invalid: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.domain, FaultDomain):
            raise ValueError("domain must be FaultDomain")
        if not isinstance(self.code, FaultCode):
            raise ValueError("code must be FaultCode")
        if not isinstance(self.attribution, FaultAttribution):
            raise ValueError("attribution must be FaultAttribution")
        if self.profile_id is not None and (not isinstance(self.profile_id, str) or not self.profile_id.strip()):
            raise ValueError("profile_id must be non-empty str|None")
        if not isinstance(self.infrastructure_invalid, bool):
            raise ValueError("infrastructure_invalid must be bool")

    @classmethod
    def from_fault_analysis(
        cls,
        analysis: FaultAnalysis,
        *,
        profile_id: str | None = None,
    ) -> "FailureSignalV2":
        if not isinstance(analysis, FaultAnalysis):
            raise ValueError("analysis must be FaultAnalysis")
        return cls(
            analysis.primary_domain,
            analysis.primary_code,
            analysis.attribution,
            profile_id,
            analysis.sample_validity is SampleValidity.INVALID_INFRASTRUCTURE,
        )


@dataclass(frozen=True, slots=True)
class TaskProfileV2:
    task_id: str
    task_class: BenchmarkTaskClass
    difficulty: BenchmarkDifficulty
    known_target_files: tuple[str, ...]
    estimated_file_count: int
    navigation_need: NavigationNeed
    context_tokens: int
    dependency_depth: int
    ambiguity: AmbiguityLevel
    risk: AmbiguityLevel
    testable: bool
    parallel_safe: bool
    requires_workspace_mutation: bool
    previous_failures: tuple[FailureSignalV2, ...]
    resources: ResourceStateV2

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, str) or not self.task_id.strip():
            raise ValueError("task_id must be non-empty string")
        if not isinstance(self.task_class, BenchmarkTaskClass):
            raise ValueError("task_class must be BenchmarkTaskClass")
        if not isinstance(self.difficulty, BenchmarkDifficulty):
            raise ValueError("difficulty must be BenchmarkDifficulty")
        if not isinstance(self.known_target_files, tuple) or any(
            not isinstance(path, str)
            or not path.strip()
            or path.replace("\\", "/").startswith("/")
            or ".." in path.replace("\\", "/").split("/")
            or ":" in path.replace("\\", "/").split("/", 1)[0]
            for path in self.known_target_files
        ):
            raise ValueError("known_target_files must be safe repository-relative tuple[str, ...]")
        if len(self.known_target_files) != len(set(self.known_target_files)):
            raise ValueError("known_target_files must be unique")
        for name in ("estimated_file_count", "context_tokens", "dependency_depth"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be integer >= 0")
        if not isinstance(self.navigation_need, NavigationNeed):
            raise ValueError("navigation_need must be NavigationNeed")
        if not isinstance(self.ambiguity, AmbiguityLevel) or not isinstance(self.risk, AmbiguityLevel):
            raise ValueError("ambiguity/risk must be AmbiguityLevel")
        for name in ("testable", "parallel_safe", "requires_workspace_mutation"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be bool")
        if not isinstance(self.previous_failures, tuple) or any(
            not isinstance(item, FailureSignalV2) for item in self.previous_failures
        ):
            raise ValueError("previous_failures must be tuple[FailureSignalV2, ...]")
        if not isinstance(self.resources, ResourceStateV2):
            raise ValueError("resources must be ResourceStateV2")

    @property
    def known_files(self) -> bool:
        return bool(self.known_target_files)

    @property
    def targeted_known_file_task(self) -> bool:
        return self.known_files and self.navigation_need is NavigationNeed.LOW

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "task_class": self.task_class.value,
            "difficulty": self.difficulty.value,
            "known_target_files": list(self.known_target_files),
            "estimated_file_count": self.estimated_file_count,
            "navigation_need": self.navigation_need.value,
            "context_tokens": self.context_tokens,
            "dependency_depth": self.dependency_depth,
            "ambiguity": self.ambiguity.value,
            "risk": self.risk.value,
            "testable": self.testable,
            "parallel_safe": self.parallel_safe,
            "requires_workspace_mutation": self.requires_workspace_mutation,
            "previous_failures": [
                {
                    "domain": item.domain.value,
                    "code": item.code.value,
                    "attribution": item.attribution.value,
                    "profile_id": item.profile_id,
                    "infrastructure_invalid": item.infrastructure_invalid,
                }
                for item in self.previous_failures
            ],
            "resources": {
                "worker_online": self.resources.worker_online,
                "free_vram_gb": self.resources.free_vram_gb,
                "reclaimable_vram_gb": self.resources.reclaimable_vram_gb,
                "loaded_profile_ids": list(self.resources.loaded_profile_ids),
            },
        }


@dataclass(frozen=True, slots=True)
class ProfileScoreV2:
    profile_id: str
    eligible: bool
    success_prior: float
    task_fit: float
    latency_penalty: float
    load_penalty: float
    failure_penalty: float
    warm_bonus: float
    total_score: float
    rejection_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.profile_id, str) or not self.profile_id.strip():
            raise ValueError("profile_id must be non-empty string")
        if not isinstance(self.eligible, bool):
            raise ValueError("eligible must be bool")
        for name in (
            "success_prior", "task_fit", "latency_penalty", "load_penalty",
            "failure_penalty", "warm_bonus", "total_score",
        ):
            if not isinstance(getattr(self, name), (int, float)) or isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be numeric")
        if not isinstance(self.rejection_reasons, tuple) or any(not isinstance(x, str) for x in self.rejection_reasons):
            raise ValueError("rejection_reasons must be tuple[str, ...]")

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "eligible": self.eligible,
            "success_prior": round(self.success_prior, 6),
            "task_fit": round(self.task_fit, 6),
            "latency_penalty": round(self.latency_penalty, 6),
            "load_penalty": round(self.load_penalty, 6),
            "failure_penalty": round(self.failure_penalty, 6),
            "warm_bonus": round(self.warm_bonus, 6),
            "total_score": round(self.total_score, 6),
            "rejection_reasons": list(self.rejection_reasons),
        }


@dataclass(frozen=True, slots=True)
class CascadeStepV2:
    action: RouteAction
    profile_id: str | None
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.action, RouteAction):
            raise ValueError("action must be RouteAction")
        if self.profile_id is not None and (not isinstance(self.profile_id, str) or not self.profile_id.strip()):
            raise ValueError("profile_id must be non-empty str|None")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("reason must be non-empty string")

    def to_dict(self) -> dict[str, Any]:
        return {"action": self.action.value, "profile_id": self.profile_id, "reason": self.reason}


@dataclass(frozen=True, slots=True)
class RouterDecisionV2:
    action: RouteAction
    selected_profile_id: str | None
    score: float | None
    confidence: float
    reason: str
    candidates: tuple[ProfileScoreV2, ...]
    cascade: tuple[CascadeStepV2, ...]
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.action, RouteAction):
            raise ValueError("action must be RouteAction")
        if self.selected_profile_id is not None and (
            not isinstance(self.selected_profile_id, str) or not self.selected_profile_id.strip()
        ):
            raise ValueError("selected_profile_id must be non-empty str|None")
        if self.score is not None and (not isinstance(self.score, (int, float)) or isinstance(self.score, bool)):
            raise ValueError("score must be numeric|None")
        if not isinstance(self.confidence, (int, float)) or isinstance(self.confidence, bool) or not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be in [0,1]")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("reason must be non-empty string")
        if not isinstance(self.candidates, tuple) or any(not isinstance(x, ProfileScoreV2) for x in self.candidates):
            raise ValueError("candidates must be tuple[ProfileScoreV2, ...]")
        if not isinstance(self.cascade, tuple) or any(not isinstance(x, CascadeStepV2) for x in self.cascade):
            raise ValueError("cascade must be tuple[CascadeStepV2, ...]")
        if not isinstance(self.warnings, tuple) or any(not isinstance(x, str) for x in self.warnings):
            raise ValueError("warnings must be tuple[str, ...]")

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "selected_profile_id": self.selected_profile_id,
            "score": None if self.score is None else round(self.score, 6),
            "confidence": round(self.confidence, 6),
            "reason": self.reason,
            "candidates": [item.to_dict() for item in self.candidates],
            "cascade": [item.to_dict() for item in self.cascade],
            "warnings": list(self.warnings),
        }
