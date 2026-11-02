'Durable research quality quality report store.'
from __future__ import annotations

import json
from pathlib import Path

from .models import ResearchQualityReportV1


class JsonQualityStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def write(self, report: ResearchQualityReportV1, *, name: str | None = None) -> Path:
        target = self.root / (name or f"{report.research_id}_quality.json")
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_suffix(target.suffix + ".tmp")
        temp.write_text(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temp.replace(target)
        return target
