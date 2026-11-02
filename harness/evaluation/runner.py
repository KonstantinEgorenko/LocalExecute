'Reproducible, resumable evaluation benchmark matrix runner.'
from __future__ import annotations

import hashlib
import json
import platform
import sys
from pathlib import Path
from typing import Callable

from .errors import BenchmarkExecutionError, EvaluationIntegrityError
from .metrics import BenchmarkAggregator
from .models import (
    BenchmarkInvocation,
    BenchmarkMeasurement,
    BenchmarkRecord,
    BenchmarkRun,
    BenchmarkRunnerResult,
    BenchmarkSuite,
    EvaluationPolicy,
)
from .store import FileEvaluationStore

BenchmarkExecutor = Callable[[BenchmarkInvocation], BenchmarkMeasurement]


def environment_fingerprint(project_root: Path) -> str:
    """Hash stable runtime/config identity without making network/model calls."""
    root = Path(project_root)
    files = [
        root / "config" / "models.yaml",
        root / "config" / "routing.yaml",
        root / "config" / "resources.yaml",
        root / "config" / "policies.yaml",
        root / "pyproject.toml",
    ]
    payload: dict[str, object] = {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "system": platform.system(),
        "machine": platform.machine(),
        "files": {},
    }
    for path in files:
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise EvaluationIntegrityError(f"cannot fingerprint benchmark environment file {path}: {exc}") from exc
        payload["files"][str(path.relative_to(root)).replace("\\", "/")] = hashlib.sha256(data).hexdigest()  # type: ignore[index]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()


class BenchmarkRunner:
    def __init__(
        self,
        *,
        store: FileEvaluationStore,
        policy: EvaluationPolicy,
        aggregator: BenchmarkAggregator | None = None,
    ) -> None:
        self.store = store
        self.policy = policy
        self.aggregator = aggregator or BenchmarkAggregator()

    def run(
        self,
        *,
        suite: BenchmarkSuite,
        executor: BenchmarkExecutor,
        run_id: str,
        environment_fingerprint: str,
    ) -> BenchmarkRunnerResult:
        self._validate_suite(suite)
        expected = self._invocations(suite)
        if len(expected) > self.policy.max_records_per_run:
            raise EvaluationIntegrityError("benchmark plan exceeds max_records_per_run")
        self.store.save_suite(suite)

        existing: BenchmarkRun | None = None
        try:
            existing = self.store.load_run(run_id)
        except Exception as exc:
            # Only a missing run may start new; corruption must still fail closed.
            from .errors import EvaluationStoreError
            if not isinstance(exc, EvaluationStoreError) or "not found" not in str(exc):
                raise

        if existing is not None:
            if existing.suite_id != suite.suite_id or existing.suite_fingerprint != suite.fingerprint:
                raise EvaluationIntegrityError("existing benchmark run is bound to a different suite")
            if existing.environment_fingerprint != environment_fingerprint:
                raise EvaluationIntegrityError("existing benchmark run environment fingerprint changed")
            if existing.complete:
                summary = self.aggregator.summarize(existing)
                self.store.save_summary(summary)
                return BenchmarkRunnerResult(existing, summary, True, len(existing.records))

        existing_records = () if existing is None else existing.records
        expected_ids = tuple(invocation.invocation_id for invocation in expected)
        if tuple(record.invocation_id for record in existing_records) != expected_ids[: len(existing_records)]:
            raise EvaluationIntegrityError("incomplete benchmark checkpoint is not a prefix of deterministic plan")

        records = list(existing_records)
        resumed = len(records)
        for invocation in expected[resumed:]:
            try:
                measurement = executor(invocation)
            except Exception as exc:
                raise BenchmarkExecutionError(
                    f"benchmark invocation {invocation.invocation_id} failed: {type(exc).__name__}: {exc}"
                ) from exc
            if not isinstance(measurement, BenchmarkMeasurement):
                raise BenchmarkExecutionError("benchmark executor must return BenchmarkMeasurement")
            record = BenchmarkRecord(
                invocation.invocation_id,
                suite.suite_id,
                suite.fingerprint,
                invocation.case.case_id,
                invocation.case.task_class,
                invocation.case.difficulty,
                invocation.repetition,
                invocation.model_id,
                invocation.router_selected,
                measurement,
                invocation.case.tags,
            )
            records.append(record)
            checkpoint = BenchmarkRun(run_id, suite.suite_id, suite.fingerprint, environment_fingerprint, tuple(records), False)
            self.store.save_run(checkpoint, allow_progress=True)

        final_run = BenchmarkRun(run_id, suite.suite_id, suite.fingerprint, environment_fingerprint, tuple(records), True)
        self.store.save_run(final_run, allow_progress=True)
        summary = self.aggregator.summarize(final_run)
        self.store.save_summary(summary)
        return BenchmarkRunnerResult(final_run, summary, False, resumed)

    def _validate_suite(self, suite: BenchmarkSuite) -> None:
        if len(suite.cases) > self.policy.max_cases_per_suite:
            raise EvaluationIntegrityError("benchmark suite exceeds max_cases_per_suite")
        for case in suite.cases:
            if len(case.tags) > self.policy.max_tags_per_case:
                raise EvaluationIntegrityError(f"case {case.case_id} exceeds max_tags_per_case")
            if case.repetitions > self.policy.max_repetitions_per_case:
                raise EvaluationIntegrityError(f"case {case.case_id} exceeds max_repetitions_per_case")
            if len(case.candidate_models) > self.policy.max_models_per_case:
                raise EvaluationIntegrityError(f"case {case.case_id} exceeds max_models_per_case")
            if self.policy.require_router_choice_in_candidates and case.router_selected_model not in case.candidate_models:
                raise EvaluationIntegrityError(f"case {case.case_id} router selection is not benchmarked")

    @staticmethod
    def _invocations(suite: BenchmarkSuite) -> tuple[BenchmarkInvocation, ...]:
        result: list[BenchmarkInvocation] = []
        for case in sorted(suite.cases, key=lambda c: (c.case_id.casefold(), c.case_id)):
            for repetition in range(case.repetitions):
                for model_id in case.candidate_models:
                    result.append(BenchmarkInvocation(
                        suite.suite_id, suite.fingerprint, case, repetition, model_id,
                        model_id == case.router_selected_model,
                    ))
        return tuple(result)
