'Publish evaluation benchmark run/summary as immutable context management artifacts.'
from __future__ import annotations

import json
from datetime import datetime, timezone

from ..context_manager.models import Artifact, ArtifactKind, ArtifactOrigin
from ..context_manager.store import ArtifactStore
from .errors import EvaluationIntegrityError
from .models import BenchmarkRun, BenchmarkSummary, canonical_json


class EvaluationArtifactPublisher:
    def __init__(self, artifact_store: ArtifactStore) -> None:
        self.artifact_store = artifact_store

    def publish(self, run: BenchmarkRun, summary: BenchmarkSummary) -> tuple[str, str]:
        if summary.run_id != run.run_id or summary.run_fingerprint != run.fingerprint:
            raise EvaluationIntegrityError("benchmark summary is not bound to run")
        run_artifact_id = f"evaluation:{run.run_id}:run"
        summary_artifact_id = f"evaluation:{run.run_id}:summary"
        self._create_once(run_artifact_id, canonical_json(run.to_dict()), "BENCHMARK_RUN", {
            "suite_id": run.suite_id, "suite_fingerprint": run.suite_fingerprint, "run_fingerprint": run.fingerprint,
        })
        self._create_once(summary_artifact_id, canonical_json(summary.to_dict()), "BENCHMARK_SUMMARY", {
            "summary_id": summary.summary_id, "run_fingerprint": summary.run_fingerprint,
        })
        return run_artifact_id, summary_artifact_id

    def _create_once(self, artifact_id: str, content: str, subtype: str, metadata: dict[str, object]) -> None:
        if self.artifact_store.has_artifact(artifact_id):
            if self.artifact_store.get_artifact(artifact_id).content != content:
                raise EvaluationIntegrityError(f"existing evaluation artifact conflicts: {artifact_id}")
            return
        self.artifact_store.create_artifact(Artifact.from_text(
            artifact_id=artifact_id,
            kind=ArtifactKind.CONTROL_PLANE_EVIDENCE,
            origin=ArtifactOrigin.CONTROL_PLANE,
            content=content,
            media_type="application/json",
            source="Phase12Evaluation",
            created_at=datetime.now(timezone.utc).isoformat(),
            metadata={"subtype": subtype, **metadata},
        ))
