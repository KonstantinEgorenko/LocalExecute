'Atomic durable evidence/report store for external evidence.'
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

from .models import ResearchReportV1, _safe_id, stable_sha256


class ResearchStoreError(RuntimeError):
    pass


class JsonResearchStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _dir(self, research_id: str) -> Path:
        return self.root / _safe_id(research_id, "research_id")

    @staticmethod
    def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + f".tmp-{os.getpid()}")
        data = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
        try:
            with temp.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)

    def write_report(self, report: ResearchReportV1) -> str:
        payload = report.to_dict(include_text=True)
        payload["report_fingerprint"] = stable_sha256(report.to_dict(include_text=False))
        path = self._dir(report.research_id) / "research_report.json"
        self._atomic_write(path, payload)
        return path.relative_to(self.root.parent).as_posix()

    def write_evidence_index(self, report: ResearchReportV1) -> str:
        payload = {
            "schema_version": 1,
            "research_id": report.research_id,
            "sources": [
                {
                    "source_id": item.source_id,
                    "canonical_url": item.canonical_url,
                    "tier": item.tier.value,
                    "kind": item.kind.value,
                    "content_sha256": item.content_sha256,
                }
                for item in report.sources
            ],
            "evidence": [item.to_dict() for item in report.evidence],
            "claims": [item.to_dict() for item in report.claims],
        }
        payload["evidence_graph_fingerprint"] = stable_sha256(payload)
        path = self._dir(report.research_id) / "evidence_graph.json"
        self._atomic_write(path, payload)
        return path.relative_to(self.root.parent).as_posix()
