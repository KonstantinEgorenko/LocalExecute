"""Project a saved Phase 24B shadow selection without another model call."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from harness.config import ConfigLoader
from harness.lead.models import LeadRequest

from .compiler import ScenarioCompilerV1
from .models import ScenarioSelectionV1
from .projector import ScenarioPlanProjectorV1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--swarm-root", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--shadow-json", type=Path, required=True)
    parser.add_argument("--goal-id", required=True)
    return parser


def _required_gates(workspace: Path) -> tuple[str, ...]:
    gates = ["candidate_diff", "scope_guard"]
    if any(workspace.rglob("*.py")):
        gates.append("python_syntax")
    if (workspace / "tests").exists() or (workspace / "pytest.ini").exists():
        gates.append("pytest")
    return tuple(gates)


def main() -> int:
    args = _parser().parse_args()
    raw = json.loads(args.shadow_json.read_text(encoding="utf-8"))

    if raw.get("mode") != "SHADOW_ONLY":
        raise ValueError("projection probe requires a Phase 24 shadow artifact")
    selection = ScenarioSelectionV1.from_dict(raw["selection"])
    compiled = ScenarioCompilerV1().compile(selection)

    expected = raw.get("compiled_fingerprint")
    if expected != compiled.fingerprint:
        raise ValueError(
            "saved shadow artifact fingerprint does not match deterministic recompile"
        )

    loader = ConfigLoader(args.swarm_root)
    policies = loader.load_policies()
    request = LeadRequest(
        objective=selection.objective,
        constraints={
            "SEQUENTIAL_MODEL_EXECUTION": True,
            "ISOLATED_WORKTREE_REQUIRED": True,
            "NO_AUTO_MERGE": True,
            "verification_required_gates": list(_required_gates(args.workspace)),
        },
        forbidden_scope=(".git", "state"),
        metadata={"phase24_shadow_projection": True},
    )

    projection = ScenarioPlanProjectorV1().project(
        scenario=compiled,
        request=request,
        goal_id=args.goal_id,
        policies=policies,
        required_gates=_required_gates(args.workspace),
    )
    print(json.dumps({
        "shadow_only": True,
        "scenario": selection.to_dict(),
        "projection": projection.to_dict(),
        "projection_fingerprint": projection.fingerprint,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
