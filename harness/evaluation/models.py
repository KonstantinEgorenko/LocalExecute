'Immutable evaluation evaluation / benchmark contracts.'
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping

from ..critic.models import FailureClass
from ..judge.models import JudgeDecision
from ..scheduler.models import SchedulerTodoStatus
from ..verification.models import VerificationOutcome


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty string")
    return value.strip()


def _non_negative_int(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be integer >= 0")
    return value


def _positive_int(value: object, name: str) -> int:
    value = _non_negative_int(value, name)
    if value == 0:
        raise ValueError(f"{name} must be > 0")
    return value


def _non_negative_float(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be finite numeric >= 0")
    return float(value)


def _unique_text(values: object, name: str) -> tuple[str, ...]:
    if not isinstance(values, (tuple, list)):
        raise ValueError(f"{name} must be list/tuple")
    result = tuple(_text(item, name) for item in values)
    if len(result) != len(set(result)):
        raise ValueError(f"{name} must not contain duplicates")
    return result


def _plain_mapping(value: object, name: str) -> Mapping[str, Any]:
    if value is None:
        return MappingProxyType({})
    if not isinstance(value, Mapping) or any(not isinstance(k, str) for k in value):
        raise ValueError(f"{name} must be mapping[str, Any]")
    # Round-trip through canonical JSON so callers cannot retain mutable nested objects.
    try:
        plain = json.loads(json.dumps(dict(value), sort_keys=True, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be JSON-serializable") from exc
    return MappingProxyType(plain)


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def stable_id(prefix: str, payload: object, *, length: int = 24) -> str:
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return f"{prefix}-{digest[:length]}"


class BenchmarkTaskClass(StrEnum):
    SIMPLE_EDIT = "simple_edit"
    SINGLE_FILE_BUG = "single_file_bug"
    MULTI_FILE_CHANGE = "multi_file_change"
    REPO_NAVIGATION = "repo_navigation"
    TEST_FAILURE = "test_failure"
    REFACTOR = "refactor"
    ARCHITECTURE = "architecture"
    AMBIGUOUS = "ambiguous"
    HARD_DEBUGGING = "hard_debugging"
    FAILURE_RETRY = "failure_retry"
    FAILURE_ESCALATION = "failure_escalation"
    DEPENDENCY_DAG = "dependency_dag"
    PARALLEL_SAFE = "parallel_safe"
    CONFLICT_SERIALIZATION = "conflict_serialization"


class BenchmarkDifficulty(StrEnum):
    TRIVIAL = "trivial"
    NORMAL = "normal"
    HARD = "hard"
    VERY_HARD = "very_hard"
    CRITICAL = "critical"


@dataclass(frozen=True, slots=True)
class EvaluationPolicy:
    store_dir: str = "state/evaluation"
    max_cases_per_suite: int = 200
    max_records_per_run: int = 5000
    max_tags_per_case: int = 16
    max_repetitions_per_case: int = 20
    max_models_per_case: int = 8
    latency_percentiles: tuple[int, ...] = (50, 95)
    require_verified_truth: bool = True
    require_router_choice_in_candidates: bool = True
    allow_partial_runs: bool = False

    def __post_init__(self) -> None:
        _text(self.store_dir, "store_dir")
        for name in (
            "max_cases_per_suite", "max_records_per_run", "max_tags_per_case",
            "max_repetitions_per_case", "max_models_per_case",
        ):
            _positive_int(getattr(self, name), name)
        if not isinstance(self.latency_percentiles, tuple) or not self.latency_percentiles:
            raise ValueError("latency_percentiles must be non-empty tuple")
        if tuple(sorted(set(self.latency_percentiles))) != self.latency_percentiles:
            raise ValueError("latency_percentiles must be unique ascending integers")
        if any(not isinstance(p, int) or isinstance(p, bool) or not 1 <= p <= 100 for p in self.latency_percentiles):
            raise ValueError("latency_percentiles must be integers in [1, 100]")
        for name in ("require_verified_truth", "require_router_choice_in_candidates", "allow_partial_runs"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be bool")
        if self.require_verified_truth is not True:
            raise ValueError("Phase 12 requires deterministic verification truth")
        if self.require_router_choice_in_candidates is not True:
            raise ValueError("Phase 12 V1 requires router choice to be benchmarked")

    @classmethod
    def from_policies(cls, policies: Mapping[str, Any]) -> "EvaluationPolicy":
        raw = policies.get("evaluation", {}) if isinstance(policies, Mapping) else {}
        if not isinstance(raw, Mapping):
            raise ValueError("policies.evaluation must be a mapping")
        percentiles = raw.get("latency_percentiles", [50, 95])
        return cls(
            store_dir=str(raw.get("store_dir", "state/evaluation")),
            max_cases_per_suite=int(raw.get("max_cases_per_suite", 200)),
            max_records_per_run=int(raw.get("max_records_per_run", 5000)),
            max_tags_per_case=int(raw.get("max_tags_per_case", 16)),
            max_repetitions_per_case=int(raw.get("max_repetitions_per_case", 20)),
            max_models_per_case=int(raw.get("max_models_per_case", 8)),
            latency_percentiles=tuple(int(x) for x in percentiles),
            require_verified_truth=bool(raw.get("require_verified_truth", True)),
            require_router_choice_in_candidates=bool(raw.get("require_router_choice_in_candidates", True)),
            allow_partial_runs=bool(raw.get("allow_partial_runs", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "store_dir": self.store_dir,
            "max_cases_per_suite": self.max_cases_per_suite,
            "max_records_per_run": self.max_records_per_run,
            "max_tags_per_case": self.max_tags_per_case,
            "max_repetitions_per_case": self.max_repetitions_per_case,
            "max_models_per_case": self.max_models_per_case,
            "latency_percentiles": list(self.latency_percentiles),
            "require_verified_truth": self.require_verified_truth,
            "require_router_choice_in_candidates": self.require_router_choice_in_candidates,
            "allow_partial_runs": self.allow_partial_runs,
        }


@dataclass(frozen=True, slots=True)
class BenchmarkCase:
    case_id: str
    task_class: BenchmarkTaskClass
    objective: str
    difficulty: BenchmarkDifficulty
    candidate_models: tuple[str, ...]
    router_selected_model: str
    tags: tuple[str, ...] = ()
    repetitions: int = 1
    timeout_seconds: int = 300
    metadata: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        _text(self.case_id, "case_id")
        _text(self.objective, "objective")
        if not isinstance(self.task_class, BenchmarkTaskClass):
            raise ValueError("task_class must be BenchmarkTaskClass")
        if not isinstance(self.difficulty, BenchmarkDifficulty):
            raise ValueError("difficulty must be BenchmarkDifficulty")
        candidates = _unique_text(self.candidate_models, "candidate_models")
        if not candidates:
            raise ValueError("candidate_models must not be empty")
        object.__setattr__(self, "candidate_models", candidates)
        _text(self.router_selected_model, "router_selected_model")
        if self.router_selected_model not in candidates:
            raise ValueError("router_selected_model must be in candidate_models")
        object.__setattr__(self, "tags", _unique_text(self.tags, "tags"))
        _positive_int(self.repetitions, "repetitions")
        _positive_int(self.timeout_seconds, "timeout_seconds")
        object.__setattr__(self, "metadata", _plain_mapping(self.metadata, "metadata"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "task_class": self.task_class.value,
            "objective": self.objective,
            "difficulty": self.difficulty.value,
            "candidate_models": list(self.candidate_models),
            "router_selected_model": self.router_selected_model,
            "tags": list(self.tags),
            "repetitions": self.repetitions,
            "timeout_seconds": self.timeout_seconds,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "BenchmarkCase":
        return cls(
            str(raw["case_id"]), BenchmarkTaskClass(str(raw["task_class"])), str(raw["objective"]),
            BenchmarkDifficulty(str(raw["difficulty"])), tuple(str(x) for x in raw["candidate_models"]),
            str(raw["router_selected_model"]), tuple(str(x) for x in raw.get("tags", ())),
            int(raw.get("repetitions", 1)), int(raw.get("timeout_seconds", 300)), raw.get("metadata", {}),
        )


@dataclass(frozen=True, slots=True)
class BenchmarkSuite:
    suite_id: str
    schema_version: int
    cases: tuple[BenchmarkCase, ...]
    description: str = ""

    def __post_init__(self) -> None:
        _text(self.suite_id, "suite_id")
        if self.schema_version != 1:
            raise ValueError("schema_version must be exactly 1")
        if not isinstance(self.cases, tuple) or not self.cases or any(not isinstance(c, BenchmarkCase) for c in self.cases):
            raise ValueError("cases must be non-empty tuple[BenchmarkCase]")
        ids = tuple(case.case_id for case in self.cases)
        if len(ids) != len(set(ids)):
            raise ValueError("benchmark case IDs must be unique")
        if not isinstance(self.description, str):
            raise ValueError("description must be str")

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(canonical_json(self.to_dict()).encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "suite_id": self.suite_id,
            "schema_version": self.schema_version,
            "description": self.description,
            "cases": [case.to_dict() for case in self.cases],
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "BenchmarkSuite":
        return cls(
            str(raw["suite_id"]), int(raw["schema_version"]),
            tuple(BenchmarkCase.from_dict(x) for x in raw["cases"]), str(raw.get("description", "")),
        )


@dataclass(frozen=True, slots=True)
class BenchmarkMeasurement:
    verification_outcome: VerificationOutcome
    final_status: SchedulerTodoStatus
    latency_ms: int
    model_calls: int
    tool_calls: int
    prompt_tokens: int
    output_tokens: int
    retry_count: int = 0
    escalation_count: int = 0
    protocol_failures: int = 0
    load_duration_ms: int = 0
    generation_duration_ms: int = 0
    peak_vram_mb: int | None = None
    peak_ram_mb: int | None = None
    critic_failure_class: FailureClass = FailureClass.NONE
    judge_decision: JudgeDecision | None = None
    error_category: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        if not isinstance(self.verification_outcome, VerificationOutcome):
            raise ValueError("verification_outcome must be VerificationOutcome")
        if not isinstance(self.final_status, SchedulerTodoStatus):
            raise ValueError("final_status must be SchedulerTodoStatus")
        for name in (
            "latency_ms", "model_calls", "tool_calls", "prompt_tokens", "output_tokens", "retry_count",
            "escalation_count", "protocol_failures", "load_duration_ms", "generation_duration_ms",
        ):
            _non_negative_int(getattr(self, name), name)
        for name in ("peak_vram_mb", "peak_ram_mb"):
            value = getattr(self, name)
            if value is not None:
                _non_negative_int(value, name)
        if not isinstance(self.critic_failure_class, FailureClass):
            raise ValueError("critic_failure_class must be FailureClass")
        if self.judge_decision is not None and not isinstance(self.judge_decision, JudgeDecision):
            raise ValueError("judge_decision must be JudgeDecision|None")
        if self.error_category is not None:
            _text(self.error_category, "error_category")
        object.__setattr__(self, "metadata", _plain_mapping(self.metadata, "metadata"))
        if self.final_status is SchedulerTodoStatus.SUCCEEDED and self.verification_outcome is not VerificationOutcome.PASSED:
            raise ValueError("SUCCEEDED benchmark measurement requires PASSED deterministic verification")
        if self.verification_outcome is VerificationOutcome.FAILED and self.judge_decision is JudgeDecision.ACCEPT:
            raise ValueError("benchmark measurement cannot record Judge ACCEPT over failed verification")

    @property
    def verified_success(self) -> bool:
        return self.verification_outcome is VerificationOutcome.PASSED and self.final_status is SchedulerTodoStatus.SUCCEEDED

    @property
    def first_pass_success(self) -> bool:
        return self.verified_success and self.retry_count == 0 and self.escalation_count == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "verification_outcome": self.verification_outcome.value,
            "final_status": self.final_status.value,
            "latency_ms": self.latency_ms,
            "model_calls": self.model_calls,
            "tool_calls": self.tool_calls,
            "prompt_tokens": self.prompt_tokens,
            "output_tokens": self.output_tokens,
            "retry_count": self.retry_count,
            "escalation_count": self.escalation_count,
            "protocol_failures": self.protocol_failures,
            "load_duration_ms": self.load_duration_ms,
            "generation_duration_ms": self.generation_duration_ms,
            "peak_vram_mb": self.peak_vram_mb,
            "peak_ram_mb": self.peak_ram_mb,
            "critic_failure_class": self.critic_failure_class.value,
            "judge_decision": self.judge_decision.value if self.judge_decision else None,
            "error_category": self.error_category,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "BenchmarkMeasurement":
        return cls(
            VerificationOutcome(str(raw["verification_outcome"])), SchedulerTodoStatus(str(raw["final_status"])),
            int(raw["latency_ms"]), int(raw["model_calls"]), int(raw["tool_calls"]), int(raw["prompt_tokens"]),
            int(raw["output_tokens"]), int(raw.get("retry_count", 0)), int(raw.get("escalation_count", 0)),
            int(raw.get("protocol_failures", 0)), int(raw.get("load_duration_ms", 0)), int(raw.get("generation_duration_ms", 0)),
            raw.get("peak_vram_mb"), raw.get("peak_ram_mb"), FailureClass(str(raw.get("critic_failure_class", FailureClass.NONE.value))),
            JudgeDecision(str(raw["judge_decision"])) if raw.get("judge_decision") is not None else None,
            raw.get("error_category"), raw.get("metadata", {}),
        )


@dataclass(frozen=True, slots=True)
class BenchmarkInvocation:
    suite_id: str
    suite_fingerprint: str
    case: BenchmarkCase
    repetition: int
    model_id: str
    router_selected: bool

    def __post_init__(self) -> None:
        _text(self.suite_id, "suite_id")
        _text(self.suite_fingerprint, "suite_fingerprint")
        if not isinstance(self.case, BenchmarkCase):
            raise ValueError("case must be BenchmarkCase")
        _non_negative_int(self.repetition, "repetition")
        if self.repetition >= self.case.repetitions:
            raise ValueError("repetition index outside case repetitions")
        _text(self.model_id, "model_id")
        if self.model_id not in self.case.candidate_models:
            raise ValueError("model_id must be one of case candidate_models")
        if not isinstance(self.router_selected, bool):
            raise ValueError("router_selected must be bool")
        if self.router_selected != (self.model_id == self.case.router_selected_model):
            raise ValueError("router_selected must match case.router_selected_model")

    @property
    def invocation_id(self) -> str:
        return stable_id("bench-inv", {
            "suite": self.suite_fingerprint, "case": self.case.case_id,
            "repetition": self.repetition, "model": self.model_id,
        })


@dataclass(frozen=True, slots=True)
class BenchmarkRecord:
    invocation_id: str
    suite_id: str
    suite_fingerprint: str
    case_id: str
    task_class: BenchmarkTaskClass
    difficulty: BenchmarkDifficulty
    repetition: int
    model_id: str
    router_selected: bool
    measurement: BenchmarkMeasurement
    tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("invocation_id", "suite_id", "suite_fingerprint", "case_id", "model_id"):
            _text(getattr(self, name), name)
        if not isinstance(self.task_class, BenchmarkTaskClass) or not isinstance(self.difficulty, BenchmarkDifficulty):
            raise ValueError("benchmark record enum fields are invalid")
        _non_negative_int(self.repetition, "repetition")
        if not isinstance(self.router_selected, bool):
            raise ValueError("router_selected must be bool")
        if not isinstance(self.measurement, BenchmarkMeasurement):
            raise ValueError("measurement must be BenchmarkMeasurement")
        object.__setattr__(self, "tags", _unique_text(self.tags, "tags"))

    @property
    def verified_success(self) -> bool:
        return self.measurement.verified_success

    def to_dict(self) -> dict[str, Any]:
        return {
            "invocation_id": self.invocation_id,
            "suite_id": self.suite_id,
            "suite_fingerprint": self.suite_fingerprint,
            "case_id": self.case_id,
            "task_class": self.task_class.value,
            "difficulty": self.difficulty.value,
            "repetition": self.repetition,
            "model_id": self.model_id,
            "router_selected": self.router_selected,
            "tags": list(self.tags),
            "measurement": self.measurement.to_dict(),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "BenchmarkRecord":
        return cls(
            str(raw["invocation_id"]), str(raw["suite_id"]), str(raw["suite_fingerprint"]), str(raw["case_id"]),
            BenchmarkTaskClass(str(raw["task_class"])), BenchmarkDifficulty(str(raw["difficulty"])), int(raw["repetition"]),
            str(raw["model_id"]), bool(raw["router_selected"]), BenchmarkMeasurement.from_dict(raw["measurement"]),
            tuple(str(x) for x in raw.get("tags", ())),
        )


@dataclass(frozen=True, slots=True)
class BenchmarkRun:
    run_id: str
    suite_id: str
    suite_fingerprint: str
    environment_fingerprint: str
    records: tuple[BenchmarkRecord, ...]
    complete: bool = True

    def __post_init__(self) -> None:
        for name in ("run_id", "suite_id", "suite_fingerprint", "environment_fingerprint"):
            _text(getattr(self, name), name)
        if not isinstance(self.records, tuple) or any(not isinstance(r, BenchmarkRecord) for r in self.records):
            raise ValueError("records must be tuple[BenchmarkRecord]")
        if len({r.invocation_id for r in self.records}) != len(self.records):
            raise ValueError("benchmark run cannot contain duplicate invocation IDs")
        if any(r.suite_id != self.suite_id or r.suite_fingerprint != self.suite_fingerprint for r in self.records):
            raise ValueError("all records must match run suite identity")
        if not isinstance(self.complete, bool):
            raise ValueError("complete must be bool")

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(canonical_json(self.to_dict()).encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "suite_id": self.suite_id,
            "suite_fingerprint": self.suite_fingerprint,
            "environment_fingerprint": self.environment_fingerprint,
            "complete": self.complete,
            "records": [r.to_dict() for r in self.records],
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "BenchmarkRun":
        return cls(
            str(raw["run_id"]), str(raw["suite_id"]), str(raw["suite_fingerprint"]), str(raw["environment_fingerprint"]),
            tuple(BenchmarkRecord.from_dict(x) for x in raw.get("records", ())), bool(raw.get("complete", True)),
        )


@dataclass(frozen=True, slots=True)
class RateMetric:
    numerator: int
    denominator: int
    rate: float | None

    def __post_init__(self) -> None:
        _non_negative_int(self.numerator, "numerator")
        _non_negative_int(self.denominator, "denominator")
        if self.numerator > self.denominator:
            raise ValueError("rate numerator must not exceed denominator")
        expected = None if self.denominator == 0 else round(self.numerator / self.denominator, 6)
        if self.rate != expected:
            raise ValueError("rate must equal deterministic rounded numerator/denominator")

    @classmethod
    def make(cls, numerator: int, denominator: int) -> "RateMetric":
        return cls(numerator, denominator, None if denominator == 0 else round(numerator / denominator, 6))

    def to_dict(self) -> dict[str, Any]:
        return {"numerator": self.numerator, "denominator": self.denominator, "rate": self.rate}


@dataclass(frozen=True, slots=True)
class LatencyMetrics:
    sample_count: int
    mean_ms: float | None
    p50_ms: int | None
    p95_ms: int | None

    def to_dict(self) -> dict[str, Any]:
        return {"sample_count": self.sample_count, "mean_ms": self.mean_ms, "p50_ms": self.p50_ms, "p95_ms": self.p95_ms}


@dataclass(frozen=True, slots=True)
class GroupMetrics:
    sample_count: int
    verified_success: RateMetric
    first_pass_success: RateMetric
    retry_success: RateMetric
    escalation_success: RateMetric
    protocol_failure_rate: RateMetric
    latency: LatencyMetrics
    mean_model_calls: float | None
    mean_tool_calls: float | None
    mean_prompt_tokens: float | None
    mean_output_tokens: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "sample_count": self.sample_count,
            "verified_success": self.verified_success.to_dict(),
            "first_pass_success": self.first_pass_success.to_dict(),
            "retry_success": self.retry_success.to_dict(),
            "escalation_success": self.escalation_success.to_dict(),
            "protocol_failure_rate": self.protocol_failure_rate.to_dict(),
            "latency": self.latency.to_dict(),
            "mean_model_calls": self.mean_model_calls,
            "mean_tool_calls": self.mean_tool_calls,
            "mean_prompt_tokens": self.mean_prompt_tokens,
            "mean_output_tokens": self.mean_output_tokens,
        }


@dataclass(frozen=True, slots=True)
class RouterRegretMetrics:
    comparable_groups: int
    oracle_success_groups: int
    avoidable_failures: int
    avoidable_failure_rate: float | None
    router_oracle_wins: int
    router_oracle_win_rate: float | None
    mean_latency_regret_ms: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "comparable_groups": self.comparable_groups,
            "oracle_success_groups": self.oracle_success_groups,
            "avoidable_failures": self.avoidable_failures,
            "avoidable_failure_rate": self.avoidable_failure_rate,
            "router_oracle_wins": self.router_oracle_wins,
            "router_oracle_win_rate": self.router_oracle_win_rate,
            "mean_latency_regret_ms": self.mean_latency_regret_ms,
        }


@dataclass(frozen=True, slots=True)
class BenchmarkSummary:
    summary_id: str
    run_id: str
    run_fingerprint: str
    overall: GroupMetrics
    by_model: Mapping[str, GroupMetrics]
    by_task_class: Mapping[str, GroupMetrics]
    failure_classes: Mapping[str, int]
    judge_decisions: Mapping[str, int]
    final_statuses: Mapping[str, int]
    router_regret: RouterRegretMetrics

    def __post_init__(self) -> None:
        for name in ("summary_id", "run_id", "run_fingerprint"):
            _text(getattr(self, name), name)
        if not isinstance(self.overall, GroupMetrics) or not isinstance(self.router_regret, RouterRegretMetrics):
            raise ValueError("summary metrics types are invalid")
        for name in ("by_model", "by_task_class"):
            value = getattr(self, name)
            if not isinstance(value, Mapping) or any(not isinstance(k, str) or not isinstance(v, GroupMetrics) for k, v in value.items()):
                raise ValueError(f"{name} must be mapping[str, GroupMetrics]")
            object.__setattr__(self, name, MappingProxyType(dict(sorted(value.items()))))
        for name in ("failure_classes", "judge_decisions", "final_statuses"):
            value = getattr(self, name)
            if not isinstance(value, Mapping) or any(not isinstance(k, str) or not isinstance(v, int) or isinstance(v, bool) or v < 0 for k, v in value.items()):
                raise ValueError(f"{name} must be mapping[str, non-negative int]")
            object.__setattr__(self, name, MappingProxyType(dict(sorted(value.items()))))

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary_id": self.summary_id,
            "run_id": self.run_id,
            "run_fingerprint": self.run_fingerprint,
            "overall": self.overall.to_dict(),
            "by_model": {k: v.to_dict() for k, v in self.by_model.items()},
            "by_task_class": {k: v.to_dict() for k, v in self.by_task_class.items()},
            "failure_classes": dict(self.failure_classes),
            "judge_decisions": dict(self.judge_decisions),
            "final_statuses": dict(self.final_statuses),
            "router_regret": self.router_regret.to_dict(),
        }

@dataclass(frozen=True, slots=True)
class BenchmarkRunnerResult:
    run: BenchmarkRun
    summary: BenchmarkSummary
    reused_existing_run: bool
    resumed_records: int

    def __post_init__(self) -> None:
        if not isinstance(self.run, BenchmarkRun) or not isinstance(self.summary, BenchmarkSummary):
            raise ValueError("run/summary types are invalid")
        if self.summary.run_id != self.run.run_id or self.summary.run_fingerprint != self.run.fingerprint:
            raise ValueError("runner result summary is not bound to run")
        if not isinstance(self.reused_existing_run, bool):
            raise ValueError("reused_existing_run must be bool")
        _non_negative_int(self.resumed_records, "resumed_records")
