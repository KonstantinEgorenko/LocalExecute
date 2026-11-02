'Benchmark suite JSON loading with evaluation policy validation.'
from __future__ import annotations

import json
from pathlib import Path

from .errors import EvaluationIntegrityError
from .models import BenchmarkSuite, EvaluationPolicy


def load_benchmark_suite(path: Path, *, policy: EvaluationPolicy | None = None) -> BenchmarkSuite:
    path = Path(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise EvaluationIntegrityError(f"cannot read benchmark suite {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise EvaluationIntegrityError(f"malformed benchmark suite JSON {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise EvaluationIntegrityError("benchmark suite JSON must contain object")
    try:
        suite = BenchmarkSuite.from_dict(payload)
    except (KeyError, TypeError, ValueError) as exc:
        raise EvaluationIntegrityError(f"invalid benchmark suite {path}: {exc}") from exc
    if policy is not None:
        if len(suite.cases) > policy.max_cases_per_suite:
            raise EvaluationIntegrityError("benchmark suite exceeds max_cases_per_suite")
        for case in suite.cases:
            if len(case.tags) > policy.max_tags_per_case or case.repetitions > policy.max_repetitions_per_case or len(case.candidate_models) > policy.max_models_per_case:
                raise EvaluationIntegrityError(f"benchmark case {case.case_id} exceeds policy bounds")
    return suite
