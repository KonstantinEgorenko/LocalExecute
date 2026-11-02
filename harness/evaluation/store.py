'Durable atomic evaluation benchmark suite/run/summary store.'
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping

from .errors import EvaluationIntegrityError, EvaluationStoreError
from .models import BenchmarkRun, BenchmarkSuite, BenchmarkSummary


class FileEvaluationStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.suites = self.root / "suites"
        self.runs = self.root / "runs"
        self.summaries = self.root / "summaries"
        for path in (self.suites, self.runs, self.summaries):
            path.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _safe_component(value: str, name: str) -> str:
        if not isinstance(value, str) or not value.strip() or any(ch in value for ch in ("/", "\\", "\x00")) or value in (".", ".."):
            raise EvaluationStoreError(f"unsafe {name}")
        return value

    @staticmethod
    def _read(path: Path) -> Mapping[str, Any]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise EvaluationStoreError(f"cannot read {path}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise EvaluationIntegrityError(f"corrupt evaluation JSON {path}: {exc}") from exc
        if not isinstance(payload, dict):
            raise EvaluationIntegrityError(f"evaluation JSON {path} must contain an object")
        return payload

    @staticmethod
    def _write(path: Path, payload: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(dict(payload), sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
        try:
            fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                    handle.write(text)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        except OSError as exc:
            raise EvaluationStoreError(f"cannot persist {path}: {exc}") from exc

    def save_suite(self, suite: BenchmarkSuite) -> Path:
        name = self._safe_component(suite.suite_id, "suite_id")
        path = self.suites / f"{name}-{suite.fingerprint[:16]}.json"
        if path.exists():
            existing = BenchmarkSuite.from_dict(self._read(path))
            if existing.to_dict() != suite.to_dict():
                raise EvaluationIntegrityError("suite identity collision")
            return path
        self._write(path, suite.to_dict())
        return path

    def load_suite(self, path: Path) -> BenchmarkSuite:
        return BenchmarkSuite.from_dict(self._read(Path(path)))

    def save_run(self, run: BenchmarkRun, *, allow_progress: bool = False) -> Path:
        name = self._safe_component(run.run_id, "run_id")
        path = self.runs / f"{name}.json"
        if path.exists():
            existing = BenchmarkRun.from_dict(self._read(path))
            if existing.to_dict() == run.to_dict():
                return path
            if not allow_progress:
                raise EvaluationIntegrityError("benchmark run_id already exists with different content")
            if existing.suite_id != run.suite_id or existing.suite_fingerprint != run.suite_fingerprint or existing.environment_fingerprint != run.environment_fingerprint:
                raise EvaluationIntegrityError("benchmark run progress changed immutable identity")
            if existing.complete:
                raise EvaluationIntegrityError("completed benchmark run is immutable")
            old_ids = tuple(record.invocation_id for record in existing.records)
            new_ids = tuple(record.invocation_id for record in run.records)
            if new_ids[:len(old_ids)] != old_ids:
                raise EvaluationIntegrityError("benchmark progress must extend existing invocation prefix")
            if [record.to_dict() for record in run.records[:len(existing.records)]] != [record.to_dict() for record in existing.records]:
                raise EvaluationIntegrityError("benchmark progress cannot rewrite existing records")
        self._write(path, run.to_dict())
        return path

    def load_run(self, run_id: str) -> BenchmarkRun:
        name = self._safe_component(run_id, "run_id")
        path = self.runs / f"{name}.json"
        if not path.is_file():
            raise EvaluationStoreError(f"benchmark run not found: {run_id}")
        return BenchmarkRun.from_dict(self._read(path))

    def save_summary(self, summary: BenchmarkSummary) -> Path:
        name = self._safe_component(summary.run_id, "run_id")
        path = self.summaries / f"{name}.json"
        if path.exists():
            existing = self._read(path)
            if existing != summary.to_dict():
                raise EvaluationIntegrityError("summary run_id already exists with different content")
            return path
        self._write(path, summary.to_dict())
        return path

    def load_summary_payload(self, run_id: str) -> Mapping[str, Any]:
        name = self._safe_component(run_id, "run_id")
        path = self.summaries / f"{name}.json"
        if not path.is_file():
            raise EvaluationStoreError(f"benchmark summary not found: {run_id}")
        return self._read(path)
