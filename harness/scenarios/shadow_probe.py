"""Standalone Phase 24B shadow probe using the existing production Ornith runtime."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from harness.integration.common_runtime import create_common_runtime_components

from .selector import OrnithScenarioSelectorV1
from .shadow import ScenarioShadowRecorderV1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--swarm-root", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--mission-id", required=True)
    parser.add_argument("--objective", required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()

    common = create_common_runtime_components(
        swarm_root=args.swarm_root,
        workspace=args.workspace,
        state_root=args.state_root,
        require_model_runtime_environment=False,
    )
    selector = OrnithScenarioSelectorV1(common.lead.runtime)
    result = selector.select(args.objective)
    path = ScenarioShadowRecorderV1(args.output_root).record(
        mission_id=args.mission_id,
        result=result,
    )
    print(json.dumps({
        "shadow_only": True,
        "output": str(path),
        "selection": result.selection.to_dict(),
        "compiled": result.compiled.to_dict(),
        "compiled_fingerprint": result.compiled.fingerprint,
        "telemetry": result.telemetry.to_dict(),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
