"""Public OpenCode/CLI entrypoint for durable production Swarm missions."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .integration.end_to_end import create_end_to_end_runtime, preflight, utc_mission_id
from .integration.research_proposal_invocation_tracing import (
    ResearchProposalInvocationObserverV1,
)
from .integration.runtime_factory import create_production_swarm_runtime
from .mission import MissionSpecV1, MissionState
from .research import (
    DEFAULT_MODEL_PROPOSAL_FACTORY_REGISTRY,
    DEFAULT_RESEARCH_PROFILE_REGISTRY,
    MissionDispatcherV1,
    MissionModeV1,
    ResearchRuntimeCatalogV1,
)


PRODUCTION_PROFILE = "production"
SMOKE_PROFILE = "smoke-single-candidate"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one guarded durable Swarm mission")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--mission-id")
    parser.add_argument("--goal-id")
    parser.add_argument("--goal", required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--state-root", type=Path)
    parser.add_argument(
        "--profile",
        choices=(PRODUCTION_PROFILE, SMOKE_PROFILE),
        default=PRODUCTION_PROFILE,
        help="production uses durable Mission -> Workflow execution; smoke is the legacy single-candidate diagnostic",
    )
    parser.add_argument(
        "--mode",
        choices=tuple(item.value for item in MissionModeV1),
        default=None,
        help="omit for legacy EXECUTION behavior; RESEARCH_CAMPAIGN uses canonical research routing",
    )
    parser.add_argument(
        "--research-profile",
        help="trusted registered research profile id for RESEARCH_CAMPAIGN mode",
    )
    parser.add_argument(
        "--generator-identity",
        help="trusted registered proposal generator identity; omitted means deterministic",
    )
    parser.add_argument("--preflight", action="store_true", help="read-only readiness check")
    parser.add_argument("--execute", action="store_true", help="create/resume the durable mission and call models")
    return parser


def build_mission_spec(args: argparse.Namespace) -> MissionSpecV1:
    constraints: dict[str, object] = {"workspace": str(args.workspace)}
    mode = getattr(args, "mode", None)
    research_profile = getattr(args, "research_profile", None)
    generator_identity = getattr(args, "generator_identity", None)
    if mode is not None:
        constraints["mission_mode"] = mode
    if research_profile is not None:
        constraints["research_profile_id"] = research_profile
    if generator_identity is not None:
        constraints["proposal_generator_identity"] = generator_identity
    return MissionSpecV1(
        mission_id=args.mission_id,
        goal_id=args.goal_id,
        raw_objective=args.goal,
        goal_capsule=args.goal,
        global_constraints=constraints,
    )


def _production_report(runtime, spec: MissionSpecV1, result) -> dict:
    checkpoint = runtime.mission_store.load(spec.mission_id)
    payload = {
        "mission_id": result.mission_id,
        "goal_id": spec.goal_id,
        "profile": PRODUCTION_PROFILE,
        "status": result.state.value,
        "checkpoint_generation": result.checkpoint_generation,
        "phase16_invocations": result.workflow_invocations,
        "restart_count": result.restart_count,
        "amendment_count": result.amendment_count,
        "completed_todo_ids": list(result.completed_todo_ids),
        "planned_todo_ids": [] if checkpoint.plan is None else list(checkpoint.plan.todo_ids),
        "final_report_ref": result.final_report_ref,
        "reason": result.reason,
        "durable_state": str(runtime.mission_store.root),
        "candidate_retention": "Verified candidate worktrees/branches are retained; no automatic merge.",
    }
    if result.final_report_ref and result.state is MissionState.COMPLETED:
        report = runtime.mission_store.load_report(spec.mission_id)
        payload["final_summary"] = report.final_summary
        payload["final_report_fingerprint"] = report.fingerprint
    return payload


def _validate_cli_mode(args: argparse.Namespace, *, profile: str) -> None:
    raw_mode = getattr(args, "mode", None)
    mode = MissionModeV1.EXECUTION if raw_mode is None else MissionModeV1(raw_mode)
    research_profile = getattr(args, "research_profile", None)
    generator_identity = getattr(args, "generator_identity", None)

    if profile == SMOKE_PROFILE and mode is MissionModeV1.RESEARCH_CAMPAIGN:
        raise ValueError("RESEARCH_CAMPAIGN requires --profile production")
    if mode is MissionModeV1.RESEARCH_CAMPAIGN:
        if not isinstance(research_profile, str) or not research_profile.strip():
            raise ValueError("RESEARCH_CAMPAIGN requires --research-profile")
    else:
        if research_profile is not None:
            raise ValueError("--research-profile is only valid with --mode RESEARCH_CAMPAIGN")
        if generator_identity is not None:
            raise ValueError("--generator-identity is only valid with --mode RESEARCH_CAMPAIGN")


def run_cli(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve(strict=True)
    workspace = Path(args.workspace).resolve(strict=True)
    mission_id = args.mission_id or utc_mission_id()
    goal_id = args.goal_id or f"{mission_id}-goal"
    state_root = Path(args.state_root) if args.state_root else workspace.parent / ".swarm-state" / mission_id
    profile = getattr(args, "profile", PRODUCTION_PROFILE)
    _validate_cli_mode(args, profile=profile)

    readiness = preflight(
        swarm_root=root,
        workspace=workspace,
        state_root=state_root,
        single_endpoint=profile == PRODUCTION_PROFILE,
    )
    readiness["profile"] = profile
    readiness["production_path"] = "Mission -> Workflow -> Execution"
    if profile == PRODUCTION_PROFILE:
        readiness["physical_model_runtime"] = "single Ollama endpoint; logical lead/worker calls are serialized"
    raw_mode = getattr(args, "mode", None)
    mode = MissionModeV1.EXECUTION if raw_mode is None else MissionModeV1(raw_mode)
    readiness["mission_mode"] = mode.value

    catalog: ResearchRuntimeCatalogV1 | None = None
    if mode is MissionModeV1.RESEARCH_CAMPAIGN:
        catalog = ResearchRuntimeCatalogV1.create_default(swarm_root=root)
        research_profile_id = getattr(args, "research_profile", None)
        generator_identity = getattr(args, "generator_identity", None) or "deterministic"
        readiness["research_profile_id"] = research_profile_id
        readiness["proposal_generator_identity"] = generator_identity
        valid, issue = catalog.validate_resolution(research_profile_id, generator_identity)
        if valid:
            readiness["research_runtime_resolution"] = "READY"
        else:
            readiness["research_runtime_resolution"] = "FAILED"
            if issue:
                readiness["issues"].append(issue)
            readiness["status"] = "BLOCKED"

    if args.preflight:
        print(json.dumps(readiness, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if readiness["status"] == "READY" else 2
    if readiness["status"] != "READY":
        print(json.dumps(readiness, ensure_ascii=False, indent=2, sort_keys=True))
        return 2
    if not args.execute:
        confirm_payload = {
            "status": "CONFIRMATION_REQUIRED",
            "mission_id": mission_id,
            "goal_id": goal_id,
            "profile": profile,
            "mission_mode": readiness["mission_mode"],
            "preflight": readiness,
            "next_step": "Repeat with --execute to create/resume the durable mission.",
        }
        if mode is MissionModeV1.RESEARCH_CAMPAIGN:
            confirm_payload["research_profile_id"] = readiness.get("research_profile_id")
            confirm_payload["proposal_generator_identity"] = readiness.get("proposal_generator_identity")
        print(json.dumps(confirm_payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 2

    args.mission_id = mission_id
    args.goal_id = goal_id
    args.workspace = workspace
    spec = build_mission_spec(args)

    if profile == SMOKE_PROFILE:
        runtime = create_end_to_end_runtime(swarm_root=root, workspace=workspace, state_root=state_root)
        report = runtime.run(mission_id=mission_id, goal_id=goal_id, objective=args.goal)
        report["profile"] = SMOKE_PROFILE
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if report["status"] == "ACCEPTED" else 3

    runtime_box: dict[str, object] = {}

    def _execution_handler(request: MissionSpecV1):
        runtime = create_production_swarm_runtime(
            swarm_root=root,
            workspace=workspace,
            state_root=state_root,
        )
        runtime_box["runtime"] = runtime
        return runtime.run_or_resume(request)

    proposal_invocation_observer = (
        ResearchProposalInvocationObserverV1(state_root)
        if mode is MissionModeV1.RESEARCH_CAMPAIGN
        else None
    )

    profile_registry = (
        catalog.profile_registry
        if catalog is not None
        else DEFAULT_RESEARCH_PROFILE_REGISTRY
    )
    proposal_factory_registry = (
        catalog.proposal_factory_registry
        if catalog is not None
        else DEFAULT_MODEL_PROPOSAL_FACTORY_REGISTRY
    )

    dispatcher = MissionDispatcherV1(
        swarm_root=root,
        workspace=workspace,
        state_root=state_root,
        python=Path(sys.executable),
        execution_handler=_execution_handler,
        profile_registry=profile_registry,
        proposal_factory_registry=proposal_factory_registry,
        research_invocation_observer=proposal_invocation_observer,
    )
    dispatched = dispatcher.dispatch(spec)

    if mode is MissionModeV1.EXECUTION:
        runtime = runtime_box["runtime"]
        result = dispatched.execution_result
        report = _production_report(runtime, spec, result)
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if result.state is MissionState.COMPLETED else 3

    run_result = dispatched.research_run_result
    summary = dict(dispatched.research_summary or {})
    report = {
        "mission_id": mission_id,
        "goal_id": goal_id,
        "profile": PRODUCTION_PROFILE,
        "mission_mode": MissionModeV1.RESEARCH_CAMPAIGN.value,
        "research_profile_id": getattr(args, "research_profile", None),
        "proposal_generator_identity": getattr(args, "generator_identity", None) or "deterministic",
        "status": run_result.final_state.status.value,
        "research_summary": summary,
    }
    if proposal_invocation_observer is not None:
        lineage = proposal_invocation_observer.summary(spec)
        if lineage.get("research_proposal_invocation_count", 0) > 0:
            report["model_invocation_lineage"] = lineage
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if run_result.final_state.is_terminal else 3


def main() -> int:
    try:
        return run_cli(build_parser().parse_args())
    except Exception as exc:
        print(json.dumps({
            "status": "FAILED",
            "error_type": type(exc).__name__,
            "error": str(exc),
        }, ensure_ascii=False, indent=2, sort_keys=True))
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
