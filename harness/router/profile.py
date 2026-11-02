"""Typed, immutable input structures for deterministic routing."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ..resource_manager.snapshot import ResourceSnapshot


class TaskType(StrEnum):
    ARCHITECTURE = "architecture"
    REASONING = "reasoning"
    CRITIQUE = "critique"
    FAILURE_ANALYSIS = "failure_analysis"
    CODE = "code"
    BUGFIX = "bugfix"
    REFACTOR = "refactor"
    TESTS = "tests"
    UTILITY = "utility"
    REPO_EXPLORATION = "repo_exploration"
    MULTI_FILE_IMPLEMENTATION = "multi_file_implementation"


class Complexity(StrEnum):
    TRIVIAL = "trivial"
    NORMAL = "normal"
    HARD = "hard"
    VERY_HARD = "very_hard"
    CRITICAL = "critical"


class Scope(StrEnum):
    LOCAL = "local"
    MULTI_FILE = "multi_file"
    MODULE = "module"
    REPOSITORY = "repository"
    CROSS_REPOSITORY = "cross_repository"


class NeedLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ValidationLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ContextBucket(StrEnum):
    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"
    VERY_LARGE = "very_large"


class FailureReason(StrEnum):
    VERIFICATION_FAILED = "verification_failed"
    WORKER_LOW_CONFIDENCE = "worker_low_confidence"
    REPEATED_SAME_FAILURE = "repeated_same_failure"
    CONTEXT_TOO_LARGE = "context_too_large"
    COMPLEXITY_RECLASSIFIED = "complexity_reclassified"
    TOOL_FAILURE = "tool_failure"
    JUDGE_REJECTED = "judge_rejected"


@dataclass(frozen=True, slots=True)
class ContextEstimate:
    estimated_tokens: int | None
    bucket: ContextBucket


@dataclass(frozen=True, slots=True)
class AttemptFailure:
    model_id: str
    reason: FailureReason
    attempt_number: int


@dataclass(frozen=True, slots=True)
class TaskProfile:
    task_type: TaskType
    complexity: Complexity
    scope: Scope
    reasoning_need: NeedLevel
    repo_exploration_need: NeedLevel
    tool_depth: NeedLevel
    context_estimate: ContextEstimate
    testability: ValidationLevel
    risk: NeedLevel
    router_confidence: float
    previous_failures: tuple[AttemptFailure, ...]
    resource_state: ResourceSnapshot

    def __post_init__(self) -> None:
        if not 0.0 <= self.router_confidence <= 1.0:
            raise ValueError("router_confidence must be between 0 and 1")
        if self.context_estimate.estimated_tokens is not None and self.context_estimate.estimated_tokens < 0:
            raise ValueError("estimated_tokens must be non-negative")