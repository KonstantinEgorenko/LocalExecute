'Durable diagnostic-only recording for research diagnostics shadow scenarios.'

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .selector import ScenarioShadowResultV1


class ScenarioShadowRecorderV1:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def record(
        self,
        *,
        mission_id: str,
        result: ScenarioShadowResultV1,
    ) -> Path:
        if not isinstance(mission_id, str) or not mission_id.strip():
            raise ValueError("mission_id must be a non-empty string")
        if not isinstance(result, ScenarioShadowResultV1):
            raise TypeError("result must be ScenarioShadowResultV1")

        self.root.mkdir(parents=True, exist_ok=True)
        destination = self.root / f"{mission_id}.json"
        payload = {
            "schema_version": 1,
            "mode": "SHADOW_ONLY",
            "mission_id": mission_id,
            **result.to_dict(),
        }
        content = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        ) + "\n"

        handle, tmp_name = tempfile.mkstemp(
            prefix=destination.name + ".",
            suffix=".tmp",
            dir=str(self.root),
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp_name, destination)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        return destination
