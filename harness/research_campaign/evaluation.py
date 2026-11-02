"""Phase 22.R5 generic evaluation authority and integrity guards.

This module deliberately keeps metric authority outside candidate code.  Candidate
execution may produce artifacts, but a trusted EvaluationBackendPort prepares,
collects and scores them against an EvaluationContractV1.  Protected evaluator and
holdout resources are fingerprinted independently and must live outside the
candidate worktree when that worktree identity is available.
"""
from __future__ import annotations

import hashlib
import os
import statistics
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping, Protocol

from .contracts import EvaluationContractV1, MetricDirectionV1
from .models import ExperimentFailureKindV1, ExperimentSpecV1
from .ports import (
    ExperimentEvaluationStatusV1,
    ExperimentEvaluationV1,
    ExperimentExecutionOutcomeV1,
)
from .serialization import (
    finite_number,
    freeze_json,
    non_negative_number,
    plain,
    sha256_hex,
    text,
)


class EvaluationError(RuntimeError):
    """Base class for deterministic research evaluation failures."""


class EvaluationIntegrityError(EvaluationError):
    """Protected evaluator/holdout state is missing, changed or not isolated."""


class _EvaluationRunError(EvaluationError):
    def __init__(self, message: str, *, metric_calls: int) -> None:
        super().__init__(message)
        self.metric_calls = metric_calls


class EvaluationSurfaceV1(StrEnum):
    DEV = "DEV"
    GATE = "GATE"
    TEST = "TEST"


@dataclass(frozen=True, slots=True)
class EvaluationSampleV1:
    """One independently scored repetition returned by a trusted backend."""

    primary_metric: float
    secondary_metrics: Mapping[str, float] = field(default_factory=dict)
    telemetry: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "primary_metric", finite_number(self.primary_metric, "primary_metric"))
        if not isinstance(self.secondary_metrics, Mapping):
            raise ValueError("secondary_metrics must be mapping")
        secondary: dict[str, float] = {}
        for key, value in self.secondary_metrics.items():
            text(key, "secondary metric name", max_length=512)
            secondary[key] = finite_number(value, f"secondary_metrics.{key}")
        object.__setattr__(self, "secondary_metrics", freeze_json(secondary, "secondary_metrics"))
        telemetry = freeze_json(self.telemetry, "telemetry")
        if not isinstance(telemetry, Mapping):
            raise ValueError("telemetry must be mapping")
        object.__setattr__(self, "telemetry", telemetry)


class EvaluationBackendPort(Protocol):
    """Trusted domain-specific scorer used by GenericEvaluationAdapterV1."""

    def prepare(
        self,
        spec: ExperimentSpecV1,
        execution: ExperimentExecutionOutcomeV1,
        *,
        surface: EvaluationSurfaceV1,
    ) -> Any: ...

    def run_or_collect(self, prepared: Any, *, repetition_index: int) -> Any: ...

    def score(self, prepared: Any, observation: Any) -> EvaluationSampleV1: ...


def _path_digest(path: Path) -> str:
    """Hash file/directory contents and relative structure without trusting symlinks."""

    if not path.exists():
        raise EvaluationIntegrityError(f"protected resource is missing: {path}")
    if path.is_symlink():
        raise EvaluationIntegrityError(f"protected resource must not be symlink: {path}")
    digest = hashlib.sha256()
    if path.is_file():
        digest.update(b"FILE\0")
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    if not path.is_dir():
        raise EvaluationIntegrityError(f"protected resource must be file or directory: {path}")

    digest.update(b"DIR\0")
    for root, dirs, files in os.walk(path, topdown=True, followlinks=False):
        root_path = Path(root)
        dirs.sort()
        files.sort()
        for name in dirs:
            child = root_path / name
            if child.is_symlink():
                raise EvaluationIntegrityError(f"protected resource contains symlink: {child}")
            rel = child.relative_to(path).as_posix()
            digest.update(b"D\0" + rel.encode("utf-8") + b"\0")
        for name in files:
            child = root_path / name
            if child.is_symlink():
                raise EvaluationIntegrityError(f"protected resource contains symlink: {child}")
            rel = child.relative_to(path).as_posix()
            digest.update(b"F\0" + rel.encode("utf-8") + b"\0")
            with child.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class ProtectedResourceV1:
    """Expected immutable evaluator/holdout resource."""

    path: str
    expected_sha256: str

    def __post_init__(self) -> None:
        raw = text(self.path, "path", max_length=32_000)
        absolute = os.path.abspath(os.path.normpath(os.path.expanduser(raw)))
        object.__setattr__(self, "path", absolute)
        object.__setattr__(self, "expected_sha256", sha256_hex(self.expected_sha256, "expected_sha256"))

    @classmethod
    def capture(cls, path: str | os.PathLike[str]) -> "ProtectedResourceV1":
        absolute = Path(os.path.abspath(os.path.normpath(os.fspath(path))))
        return cls(path=str(absolute), expected_sha256=_path_digest(absolute))

    def verify(self) -> str:
        actual = _path_digest(Path(self.path))
        if actual != self.expected_sha256:
            raise EvaluationIntegrityError(f"protected resource digest changed: {self.path}")
        return actual


def _within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


@dataclass(frozen=True, slots=True)
class EvaluationIntegrityGuardV1:
    """Validates protected state and candidate/protected path isolation."""

    protected_resources: tuple[ProtectedResourceV1, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.protected_resources, tuple) or not self.protected_resources:
            raise ValueError("protected_resources must be non-empty tuple")
        seen: set[str] = set()
        for item in self.protected_resources:
            if not isinstance(item, ProtectedResourceV1):
                raise ValueError("protected_resources items must be ProtectedResourceV1")
            folded = os.path.normcase(item.path)
            if folded in seen:
                raise ValueError("protected_resources must not contain duplicate paths")
            seen.add(folded)

    def validate(self, *, candidate_root: str | None = None) -> Mapping[str, str]:
        candidate: Path | None = None
        if candidate_root is not None:
            raw = text(candidate_root, "candidate_root", max_length=32_000)
            candidate = Path(os.path.abspath(os.path.normpath(os.path.expanduser(raw))))
            if not candidate.exists() or not candidate.is_dir() or candidate.is_symlink():
                raise EvaluationIntegrityError("candidate_root must be an existing non-symlink directory")
        verified: dict[str, str] = {}
        for item in self.protected_resources:
            protected = Path(item.path)
            if candidate is not None:
                # Neither tree may contain the other.  This makes hidden evaluator
                # and holdout data structurally outside candidate mutation scope.
                if _within(protected, candidate) or _within(candidate, protected):
                    raise EvaluationIntegrityError(
                        f"protected resource and candidate_root are not isolated: {protected} vs {candidate}"
                    )
            verified[item.path] = item.verify()
        return freeze_json(verified, "verified protected resources")


@dataclass(frozen=True, slots=True)
class ChampionAdmissionPolicyV1:
    """Deterministic DEV -> GATE admission policy.

    For an empty campaign the first candidate must still pass GATE when
    ``require_gate`` is true.  For later candidates DEV must first clear
    ``dev_improvement_margin`` and GATE must independently clear
    ``gate_improvement_margin`` against the incumbent protected score.
    """

    require_gate: bool = True
    dev_improvement_margin: float = 0.0
    gate_improvement_margin: float = 0.0

    def __post_init__(self) -> None:
        if not isinstance(self.require_gate, bool):
            raise ValueError("require_gate must be bool")
        object.__setattr__(
            self,
            "dev_improvement_margin",
            non_negative_number(self.dev_improvement_margin, "dev_improvement_margin"),
        )
        object.__setattr__(
            self,
            "gate_improvement_margin",
            non_negative_number(self.gate_improvement_margin, "gate_improvement_margin"),
        )

    @staticmethod
    def improvement(candidate: float, incumbent: float, direction: MetricDirectionV1) -> float:
        candidate = finite_number(candidate, "candidate")
        incumbent = finite_number(incumbent, "incumbent")
        if direction is MetricDirectionV1.MAXIMIZE:
            return candidate - incumbent
        if direction is MetricDirectionV1.MINIMIZE:
            return incumbent - candidate
        raise ValueError(f"unsupported metric direction {direction!r}")

    def dev_qualifies(self, candidate: float, incumbent: float | None, direction: MetricDirectionV1) -> bool:
        if incumbent is None:
            return True
        return self.improvement(candidate, incumbent, direction) > self.dev_improvement_margin

    def gate_admits(self, candidate: float, incumbent: float | None, direction: MetricDirectionV1) -> bool:
        if incumbent is None:
            return True
        return self.improvement(candidate, incumbent, direction) > self.gate_improvement_margin


@dataclass(frozen=True, slots=True)
class _AggregateV1:
    primary_metric: float
    secondary_metrics: Mapping[str, float]
    variance: float
    repetitions: int


class GenericEvaluationAdapterV1:
    'Generic trusted EvaluationPort implementation for research campaign.R5.'

    _SURFACE_KEY = "research_evaluation_surface"
    _INCUMBENT_KEY = "research_incumbent_primary_metric"
    _CANDIDATE_ROOT_KEY = "candidate_worktree_root"

    def __init__(
        self,
        *,
        contract: EvaluationContractV1,
        backend: EvaluationBackendPort,
        integrity_guard: EvaluationIntegrityGuardV1,
        admission_policy: ChampionAdmissionPolicyV1 | None = None,
    ) -> None:
        if not isinstance(contract, EvaluationContractV1):
            raise ValueError("contract must be EvaluationContractV1")
        if not isinstance(integrity_guard, EvaluationIntegrityGuardV1):
            raise ValueError("integrity_guard must be EvaluationIntegrityGuardV1")
        self.contract = contract
        self.backend = backend
        self.integrity_guard = integrity_guard
        self.admission_policy = admission_policy or ChampionAdmissionPolicyV1()

    def prepare(
        self,
        spec: ExperimentSpecV1,
        execution: ExperimentExecutionOutcomeV1,
        *,
        surface: EvaluationSurfaceV1,
    ) -> Any:
        return self.backend.prepare(spec, execution, surface=surface)

    def run_or_collect(self, prepared: Any, *, repetition_index: int) -> Any:
        return self.backend.run_or_collect(prepared, repetition_index=repetition_index)

    def score(self, prepared: Any, observation: Any) -> EvaluationSampleV1:
        result = self.backend.score(prepared, observation)
        if not isinstance(result, EvaluationSampleV1):
            raise EvaluationError("EvaluationBackendPort.score must return EvaluationSampleV1")
        return result

    def validate_integrity(self, execution: ExperimentExecutionOutcomeV1) -> Mapping[str, str]:
        root = execution.telemetry.get(self._CANDIDATE_ROOT_KEY)
        if root is not None and not isinstance(root, str):
            raise EvaluationIntegrityError("telemetry.candidate_worktree_root must be string when present")
        return self.integrity_guard.validate(candidate_root=root)

    def evaluate(
        self,
        spec: ExperimentSpecV1,
        execution: ExperimentExecutionOutcomeV1,
    ) -> ExperimentEvaluationV1:
        if spec.evaluation_contract_hash != self.contract.contract_hash:
            return self._failed(
                ExperimentFailureKindV1.INVALID_METRIC,
                "ExperimentSpec EvaluationContractHash does not match evaluator authority",
            )
        metric_calls = 0
        try:
            before = self.validate_integrity(execution)
            surface = self._requested_surface(execution)
            incumbent = self._incumbent_metric(execution)

            aggregates: dict[EvaluationSurfaceV1, _AggregateV1] = {}
            admission_passed = False
            selected_surface = surface

            if surface is EvaluationSurfaceV1.DEV:
                dev = self._evaluate_surface(spec, execution, EvaluationSurfaceV1.DEV)
                metric_calls += dev.repetitions
                aggregates[EvaluationSurfaceV1.DEV] = dev
                if not self.admission_policy.require_gate:
                    admission_passed = True
                elif self.admission_policy.dev_qualifies(dev.primary_metric, incumbent, self.contract.direction):
                    gate = self._evaluate_surface(spec, execution, EvaluationSurfaceV1.GATE)
                    metric_calls += gate.repetitions
                    aggregates[EvaluationSurfaceV1.GATE] = gate
                    selected_surface = EvaluationSurfaceV1.GATE
                    admission_passed = self.admission_policy.gate_admits(
                        gate.primary_metric, incumbent, self.contract.direction
                    )
            elif surface is EvaluationSurfaceV1.GATE:
                gate = self._evaluate_surface(spec, execution, EvaluationSurfaceV1.GATE)
                metric_calls += gate.repetitions
                aggregates[EvaluationSurfaceV1.GATE] = gate
                admission_passed = self.admission_policy.gate_admits(
                    gate.primary_metric, incumbent, self.contract.direction
                )
            elif surface is EvaluationSurfaceV1.TEST:
                test = self._evaluate_surface(spec, execution, EvaluationSurfaceV1.TEST)
                metric_calls += test.repetitions
                aggregates[EvaluationSurfaceV1.TEST] = test
            else:  # pragma: no cover - StrEnum construction already closes this
                raise EvaluationError(f"unsupported evaluation surface {surface!r}")

            after = self.validate_integrity(execution)
            if plain(before) != plain(after):
                raise EvaluationIntegrityError("protected resource verification changed during evaluation")

            selected = aggregates[selected_surface]
            telemetry: dict[str, Any] = {
                "evaluation_surface": selected_surface.value,
                "requested_evaluation_surface": surface.value,
                "integrity_validated": True,
                "protected_resource_count": len(before),
                "metric_calls": metric_calls,
                "admission_required": self.admission_policy.require_gate,
                "admission_passed": admission_passed,
                "admission_surface": EvaluationSurfaceV1.GATE.value if EvaluationSurfaceV1.GATE in aggregates else None,
            }
            for key, aggregate in aggregates.items():
                prefix = key.value.lower()
                telemetry[f"{prefix}_primary_metric"] = aggregate.primary_metric
                telemetry[f"{prefix}_metric_variance"] = aggregate.variance
                telemetry[f"{prefix}_repetitions"] = aggregate.repetitions
            return ExperimentEvaluationV1(
                status=ExperimentEvaluationStatusV1.SCORED,
                primary_metric=selected.primary_metric,
                secondary_metrics=selected.secondary_metrics,
                metric_variance=selected.variance,
                telemetry=telemetry,
            )
        except EvaluationIntegrityError as exc:
            return self._failed(
                ExperimentFailureKindV1.EVALUATION_FAILED,
                str(exc),
                status=ExperimentEvaluationStatusV1.INVALID,
                integrity_validated=False,
                metric_calls=metric_calls,
            )
        except _EvaluationRunError as exc:
            return self._failed(
                ExperimentFailureKindV1.EVALUATION_FAILED,
                str(exc),
                metric_calls=metric_calls + exc.metric_calls,
            )
        except Exception as exc:
            return self._failed(
                ExperimentFailureKindV1.EVALUATION_FAILED,
                f"{type(exc).__name__}: {exc}",
                metric_calls=metric_calls,
            )

    def _requested_surface(self, execution: ExperimentExecutionOutcomeV1) -> EvaluationSurfaceV1:
        raw = execution.telemetry.get(self._SURFACE_KEY, EvaluationSurfaceV1.DEV.value)
        if not isinstance(raw, str):
            raise EvaluationError(f"telemetry.{self._SURFACE_KEY} must be string")
        try:
            return EvaluationSurfaceV1(raw)
        except ValueError as exc:
            raise EvaluationError(f"unsupported evaluation surface: {raw}") from exc

    def _incumbent_metric(self, execution: ExperimentExecutionOutcomeV1) -> float | None:
        raw = execution.telemetry.get(self._INCUMBENT_KEY)
        if raw is None:
            return None
        return finite_number(raw, f"telemetry.{self._INCUMBENT_KEY}")

    def _evaluate_surface(
        self,
        spec: ExperimentSpecV1,
        execution: ExperimentExecutionOutcomeV1,
        surface: EvaluationSurfaceV1,
    ) -> _AggregateV1:
        prepared = self.prepare(spec, execution, surface=surface)
        samples: list[EvaluationSampleV1] = []
        for index in range(self.contract.repetitions):
            try:
                collected = self.run_or_collect(prepared, repetition_index=index)
                samples.append(self.score(prepared, collected))
            except Exception as exc:
                raise _EvaluationRunError(
                    f"evaluation repetition {index} failed: {type(exc).__name__}: {exc}",
                    metric_calls=index + 1,
                ) from exc
        return self._aggregate(samples)

    def _aggregate(self, samples: list[EvaluationSampleV1]) -> _AggregateV1:
        if len(samples) != self.contract.repetitions:
            raise EvaluationError("evaluation backend returned wrong repetition count")
        metric_values = [item.primary_metric for item in samples]
        if self.contract.aggregation == "mean":
            aggregate = statistics.fmean(metric_values)
        elif self.contract.aggregation == "median":
            aggregate = statistics.median(metric_values)
        else:
            raise EvaluationError(f"unsupported aggregation: {self.contract.aggregation}")
        keys = set(samples[0].secondary_metrics)
        for sample in samples[1:]:
            if set(sample.secondary_metrics) != keys:
                raise EvaluationError("secondary metric keys must be identical across repetitions")
        secondary: dict[str, float] = {}
        for key in sorted(keys):
            values = [float(item.secondary_metrics[key]) for item in samples]
            secondary[key] = statistics.fmean(values) if self.contract.aggregation == "mean" else statistics.median(values)
        variance = statistics.pvariance(metric_values) if len(metric_values) > 1 else 0.0
        return _AggregateV1(
            primary_metric=finite_number(aggregate, "aggregated primary metric"),
            secondary_metrics=freeze_json(secondary, "aggregated secondary metrics"),
            variance=non_negative_number(variance, "metric variance"),
            repetitions=len(samples),
        )

    @staticmethod
    def _failed(
        failure_kind: ExperimentFailureKindV1,
        reason: str,
        *,
        status: ExperimentEvaluationStatusV1 = ExperimentEvaluationStatusV1.FAILED,
        integrity_validated: bool | None = None,
        metric_calls: int = 0,
    ) -> ExperimentEvaluationV1:
        if not isinstance(metric_calls, int) or isinstance(metric_calls, bool) or metric_calls < 0:
            raise ValueError("metric_calls must be integer >= 0")
        telemetry: dict[str, Any] = {
            "evaluation_error": text(reason, "reason", max_length=32_000),
            "metric_calls": metric_calls,
        }
        if integrity_validated is not None:
            telemetry["integrity_validated"] = integrity_validated
        return ExperimentEvaluationV1(
            status=status,
            telemetry=telemetry,
            failure_kind=failure_kind,
        )
