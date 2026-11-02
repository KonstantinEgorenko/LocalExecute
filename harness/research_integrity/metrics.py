from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping


def aggregate_child_runtime_metrics(targeted_paths: Mapping[str, str], root: Path) -> dict[str, int]:
    totals = {
        "cpu_enforcements": 0,
        "synthesis_compactions": 0,
        "synthesis_schema_enforcements": 0,
        "http_errors": 0,
    }
    for path_text in targeted_paths.values():
        path = Path(path_text)
        research_id = path.parent.name
        runtime_path = root / "state" / "evaluation" / "phase20q" / f"{research_id}_runtime.json"
        if not runtime_path.is_file():
            runtime_path = root / "state" / "evaluation" / "phase20r" / f"{research_id}_runtime.json"
        if not runtime_path.is_file():
            continue
        try:
            raw = json.loads(runtime_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        hardening = raw.get("runtime_hardening") if isinstance(raw.get("runtime_hardening"), Mapping) else {}
        for key in totals:
            totals[key] += int(hardening.get(key, 0) or 0)
    return totals

# Verified compatibility aliases for historical Python interfaces.
aggregate_child_phase20r_metrics = aggregate_child_runtime_metrics
