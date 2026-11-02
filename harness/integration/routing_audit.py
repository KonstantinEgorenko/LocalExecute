"""Append-only audit proxy for production rule-based routing decisions."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class RoutingAuditProxy:
    def __init__(self, inner: Any, path: Path) -> None:
        self.inner = inner
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def route(self, profile: Any):
        decision = self.inner.route(profile)
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "task_type": profile.task_type.value,
            "resource_state": profile.resource_state.to_dict(),
            "decision": decision.to_dict(),
        }
        with self.path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        return decision
