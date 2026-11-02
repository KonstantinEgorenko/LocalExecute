"""Run Phase 24D planning-only A/B comparison on the benchmark suite."""
from __future__ import annotations
import argparse
import json
from pathlib import Path

from harness.integration.common_runtime import create_common_runtime_components
from .ab_shadow import ScenarioAbShadowRunnerV1, write_ab_shadow_report

def _required_gates(workspace: Path) -> tuple[str, ...]:
    gates = ["candidate_diff", "scope_guard"]
    if any(workspace.rglob("*.py")):
        gates.append("python_syntax")
    if (workspace / "tests").exists() or (workspace / "pytest.ini").exists():
        gates.append("pytest")
    return tuple(gates)

def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--swarm-root", type=Path, required=True)
    p.add_argument("--workspace", type=Path, required=True)
    p.add_argument("--state-root", type=Path, required=True)
    p.add_argument("--suite", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()

    suite = json.loads(args.suite.read_text(encoding="utf-8-sig"))
    common = create_common_runtime_components(
        swarm_root=args.swarm_root,
        workspace=args.workspace,
        state_root=args.state_root,
        require_model_runtime_environment=False,
    )
    report = ScenarioAbShadowRunnerV1(
        lead=common.lead,
        policies=common.policies,
        required_gates=_required_gates(args.workspace),
    ).compare_suite(suite)
    output = write_ab_shadow_report(args.output, report)

    compact = {
        "shadow_only": True,
        "output": str(output),
        "case_count": report["case_count"],
        "scenario_distribution": report["scenario_distribution"],
        "typed_compiled_cases": report["typed_compiled_cases"],
        "legacy_total_todos": report["legacy_total_todos"],
        "legacy_total_lead_calls": report["legacy_total_lead_calls"],
        "cases": [{
            "case_id": c["case_id"],
            "scenario": c["typed"]["selection"]["scenario"],
            "compile_status": c["typed"]["compile_status"],
            "legacy_todo_count": c["legacy"]["todo_count"],
            "legacy_lead_call_count": c["legacy"]["lead_call_count"],
            "legacy_task_types": [t["task_type"] for t in c["legacy"]["todos"]],
        } for c in report["cases"]],
    }
    print(json.dumps(compact, ensure_ascii=False, indent=2))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
