"""Diagnostic for a second rule-based worker turn after warm model load."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..agent_loop.protocol import agent_action_json_schema
from ..executor.models import ExecutionMessage, ExecutionRequest
from ..orchestrator.models import RoutingTaskSpec
from ..orchestrator.profile_factory import TaskProfileFactory
from ..router.profile import Complexity, ContextBucket, ContextEstimate, NeedLevel, Scope, TaskType, ValidationLevel
from .common_runtime import create_common_runtime_components


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--state-root", type=Path, required=True)
    args = parser.parse_args()
    common = create_common_runtime_components(
        swarm_root=args.root,
        workspace=args.workspace,
        state_root=args.state_root,
    )
    spec = RoutingTaskSpec(
        TaskType.CODE,
        Complexity.NORMAL,
        Scope.LOCAL,
        NeedLevel.LOW,
        NeedLevel.LOW,
        NeedLevel.LOW,
        ContextEstimate(512, ContextBucket.SMALL),
        ValidationLevel.HIGH,
        NeedLevel.LOW,
        1.0,
    )
    factory = TaskProfileFactory()
    before = common.snapshot_provider()
    first = common.router.route(factory.build(spec, before, ()))
    result = common.executor.execute(ExecutionRequest(
        first,
        (ExecutionMessage("user", "Return FINAL JSON with summary 'routing probe complete'."),),
        -1,
        request_id="phase21-2-routing-reuse-probe",
        task_id="phase21-2-routing-reuse-probe",
        response_format=agent_action_json_schema(),
        temperature=0.0,
    ))
    after = common.snapshot_provider()
    second = common.router.route(factory.build(spec, after, ()))
    print(json.dumps({
        "first": first.to_dict(),
        "worker": result.to_dict(),
        "snapshot_after": after.to_dict(),
        "second": second.to_dict(),
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
