from __future__ import annotations

import json
from pathlib import Path


class JsonSandboxStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def write_snapshot(self, sandbox_id: str, payload: dict) -> Path:
        target = self.root / f"{sandbox_id}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_suffix(".json.tmp")
        temp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temp.replace(target)
        return target

    def append_event(self, sandbox_id: str, payload: dict) -> Path:
        target = self.root / f"{sandbox_id}.events.jsonl"
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(
                json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"
            )
        return target

    def read_snapshot(self, sandbox_id: str) -> dict | None:
        target = self.root / f"{sandbox_id}.json"
        if not target.is_file():
            return None
        return json.loads(target.read_text(encoding="utf-8"))
