'Durable research remediation plan/derived-report store.'
from __future__ import annotations

import json
from pathlib import Path


class JsonRemediationStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def write_json(self, name: str, payload: dict) -> Path:
        target = self.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_suffix(target.suffix + ".tmp")
        temp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temp.replace(target)
        return target
