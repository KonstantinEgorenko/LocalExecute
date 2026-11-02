'Durable mission lifecycle coordinator for mission.\n\nmission does not own planning semantics, Scheduler state, Router V2, model\nexecution, verification, or recovery. It owns mission durability and the rules\nfor safely invoking frozen workflow across restarts and bounded DAG amendments.\n'
from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping

from ..workflow.models import WorkflowOutcome, WorkflowResultV1
from .models import (
    DagAmendmentV1,
    MissionCheckpointV1,
    MissionEventV1,
    MissionReportV1,
    MissionRunResultV1,
    MissionSpecV1,
    MissionState,
    MissionLifecyclePolicy,
    workflow_is_amendable,
    workflow_summary,
)
from .ports import DagAmendmentPort, GlobalCompletionPort, MissionPlanningPort, WorkflowExecutionPort
from .store import JsonMissionStore


class DurableMissionController:
    def __init__(
        self,
        *,
        policy: MissionLifecyclePolicy,
        store: JsonMissionStore,
        planner: MissionPlanningPort,
        workflow: WorkflowExecutionPort,
        amendments: DagAmendmentPort,
        completion: GlobalCompletionPort,
    ) -> None:
        self.policy = policy
        self.store = store
        self.planner = planner
        self.workflow = workflow
        self.amendments = amendments
        self.completion = completion

    @staticmethod
    def _event(checkpoint: MissionCheckpointV1, event_type: str, state: MissionState, details: Mapping[str, Any] | None = None) -> MissionEventV1:
        return MissionEventV1(len(checkpoint.events), event_type, state, details or {})

    def _save_replace(self, old: MissionCheckpointV1, **changes: Any) -> MissionCheckpointV1:
        state = changes.get("state", old.state)
        event_type = changes.pop("event_type", None)
        event_details = changes.pop("event_details", None)
        events = old.events
        if event_type is not None:
            events = (*events, self._event(old, str(event_type), state, event_details))
        if len(events) > self.policy.max_journal_events:
            raise RuntimeError("Phase 17 max_journal_events exhausted")
        new = replace(old, generation=old.generation + 1, events=events, **changes)
        self.store.save(new, expected_generation=old.generation)
        return new

    def create(self, spec: MissionSpecV1) -> MissionCheckpointV1:
        event = MissionEventV1(0, "MISSION_CREATED", MissionState.CREATED, {"goal_id": spec.goal_id})
        checkpoint = MissionCheckpointV1(spec, MissionState.CREATED, 0, events=(event,))
        self.store.create(checkpoint)
        return checkpoint

    def plan(self, mission_id: str) -> MissionCheckpointV1:
        checkpoint = self.store.load(mission_id)
        from ..lead.required_graph import REQUIRED_GRAPH
        recheck = checkpoint.state is MissionState.PLANNED and REQUIRED_GRAPH in checkpoint.spec.global_constraints
        if checkpoint.state is not MissionState.CREATED and not recheck:
            return checkpoint
        from ..scenarios.scope_evidence import PlanningContractError
        try:
            if recheck:
                validator = getattr(self.planner, "validate_committed_plan", None)
                if validator is None:
                    raise PlanningContractError("PLANNING_REQUIRED_GRAPH_CONTRACT_INVALID", reason="restart graph validation unsupported by planning port")
                validator(checkpoint.spec, checkpoint.plan)
                return checkpoint
            plan = self.planner.ensure_initial_plan(checkpoint.spec)
        except PlanningContractError as exc:
            return self._save_replace(
                checkpoint, state=MissionState.FAILED, plan=None, failure_reason=str(exc),
                event_type="MISSION_FAILED",
                event_details={"exception_type": type(exc).__name__,
                               "failure_diagnostic": exc.failure_diagnostic},
            )
        return self._save_replace(
            checkpoint,
            state=MissionState.PLANNED,
            plan=plan,
            event_type="MISSION_PLANNED",
            event_details={"plan_id": plan.plan_id, "todo_ids": list(plan.todo_ids), "fingerprint": plan.fingerprint},
        )

    @staticmethod
    def _merge_completed(checkpoint: MissionCheckpointV1, workflow: WorkflowResultV1) -> tuple[str, ...]:
        return tuple(dict.fromkeys((*checkpoint.completed_todo_ids, *workflow.completed_todo_ids)))

    def _validate_amendment(self, checkpoint: MissionCheckpointV1, amendment: DagAmendmentV1) -> None:
        if len(amendment.operations) > self.policy.max_operations_per_amendment:
            raise RuntimeError("DAG amendment exceeds max_operations_per_amendment")
        if amendment.amendment_id in {item.amendment_id for item in checkpoint.amendments}:
            raise RuntimeError("duplicate DAG amendment_id")
        completed = set(checkpoint.completed_todo_ids)
        for operation in amendment.operations:
            if operation.todo_id in completed:
                raise RuntimeError(f"DAG amendment cannot mutate completed Todo: {operation.todo_id}")
            if operation.dependency_id is not None and operation.dependency_id == operation.todo_id:
                raise RuntimeError("Todo cannot depend on itself")

    def _ensure_pending_amendments_applied(self, checkpoint: MissionCheckpointV1) -> MissionCheckpointV1:
        applied = list(checkpoint.applied_amendment_ids)
        for amendment in checkpoint.amendments:
            if amendment.amendment_id in applied:
                continue
            # DagAmendmentPort.apply is an ensure-applied operation: the DAG owner
            # must make repeated calls with the same amendment_id idempotent. This
            # closes the crash window between external DAG mutation and journal commit.
            self.amendments.apply(amendment)
            applied.append(amendment.amendment_id)
            checkpoint = self._save_replace(
                checkpoint,
                state=MissionState.RECOVERING,
                applied_amendment_ids=tuple(applied),
                event_type="DAG_AMENDMENT_APPLIED",
                event_details={
                    "amendment_id": amendment.amendment_id,
                    "fingerprint": amendment.fingerprint,
                },
            )
        return checkpoint

    def _blocked_result(self, checkpoint: MissionCheckpointV1, reason: str) -> MissionRunResultV1:
        return MissionRunResultV1(
            checkpoint.mission_id,
            checkpoint.state,
            checkpoint.generation,
            checkpoint.workflow_invocations,
            checkpoint.amendment_count,
            checkpoint.restart_count,
            checkpoint.completed_todo_ids,
            checkpoint.final_report_ref,
            reason,
        )

    def _write_final_report(self, checkpoint: MissionCheckpointV1, workflow: WorkflowResultV1) -> tuple[MissionReportV1, str]:
        assert checkpoint.plan is not None
        assert checkpoint.completion_gate is not None
        summary=workflow.final_summary or workflow_summary(workflow)
        finalize=getattr(self.completion,'finalize_summary',None)
        if callable(finalize):summary=finalize(checkpoint.spec,summary,checkpoint.completion_gate)
        report = MissionReportV1(
            checkpoint.mission_id,
            checkpoint.spec.goal_id,
            checkpoint.spec.root_task_sha256,
            checkpoint.plan.plan_id,
            checkpoint.workflow_invocations,
            checkpoint.amendment_count,
            checkpoint.restart_count,
            checkpoint.completed_todo_ids,
            checkpoint.completion_gate.fingerprint,
            summary,
            checkpoint.generation,
            len(checkpoint.events),
        )
        if len(report.final_summary) > self.policy.max_report_chars:
            raise RuntimeError("final mission report exceeds max_report_chars")
        return report, self.store.write_report(report)

    def run(self, mission_id: str) -> MissionRunResultV1:
        checkpoint = self.store.load(mission_id)
        from ..lead.required_graph import REQUIRED_GRAPH
        if (checkpoint.state is MissionState.CREATED or
                checkpoint.state is MissionState.PLANNED and REQUIRED_GRAPH in checkpoint.spec.global_constraints):
            checkpoint = self.plan(mission_id)
        if checkpoint.state is MissionState.COMPLETED:
            revalidate=getattr(self.completion,'revalidate_completed',None)
            gate=revalidate(checkpoint.spec,checkpoint) if callable(revalidate) else None
            if gate is not None and not gate.passed:
                checkpoint=self._save_replace(checkpoint,state=MissionState.BLOCKED,completion_gate=gate,
                    failure_reason=gate.summary,event_type='TERMINAL_ACCEPTANCE_INVALIDATED')
                return self._blocked_result(checkpoint,gate.summary)
            return self._blocked_result(checkpoint, "mission already completed")
        if checkpoint.state is MissionState.FAILED:
            return self._blocked_result(checkpoint, checkpoint.failure_reason or "mission failed")

        if checkpoint.amendments and len(checkpoint.applied_amendment_ids) < len(checkpoint.amendments):
            checkpoint = self._ensure_pending_amendments_applied(checkpoint)

        if checkpoint.state is MissionState.RUNNING:
            checkpoint = self._save_replace(
                checkpoint,
                state=MissionState.RUNNING,
                restart_count=checkpoint.restart_count + 1,
                event_type="MISSION_RESUMED_AFTER_RESTART",
                event_details={"previous_generation": checkpoint.generation},
            )

        invocations_this_process = 0
        while invocations_this_process < self.policy.max_workflow_invocations_per_run:
            if checkpoint.state is not MissionState.RUNNING:
                checkpoint = self._save_replace(
                    checkpoint,
                    state=MissionState.RUNNING,
                    event_type="PHASE16_INVOCATION_STARTED",
                    event_details={"next_invocation": checkpoint.workflow_invocations + 1},
                )
            try:
                workflow = self.workflow.run(checkpoint.spec)
            except Exception as exc:
                from ..transport_diagnostics import error_projection
                checkpoint = self._save_replace(
                    checkpoint,
                    state=MissionState.FAILED,
                    failure_reason=f"Phase16 workflow boundary exception: {type(exc).__name__}: {exc}",
                    event_type="MISSION_FAILED",
                    event_details={"exception_type": type(exc).__name__, "message": str(exc),**error_projection(exc)},
                )
                return self._blocked_result(checkpoint, checkpoint.failure_reason or "workflow exception")

            invocations_this_process += 1
            merged = self._merge_completed(checkpoint, workflow)
            checkpoint = self._save_replace(
                checkpoint,
                state=MissionState.RUNNING,
                workflow_invocations=checkpoint.workflow_invocations + 1,
                completed_todo_ids=merged,
                last_workflow_outcome=workflow.outcome.value,
                last_workflow_summary=workflow_summary(workflow),
                event_type="PHASE16_INVOCATION_FINISHED",
                event_details={
                    "outcome": workflow.outcome.value,
                    "completed_todo_ids": list(workflow.completed_todo_ids),
                    "failed_todo_id": workflow.failed_todo_id,
                    "workflow_steps": workflow.workflow_steps,
                    "recovery_cycles": workflow.recovery_cycles,
                },
            )

            if workflow.outcome is WorkflowOutcome.SUCCEEDED:
                checkpoint = self._save_replace(
                    checkpoint,
                    state=MissionState.VERIFYING,
                    event_type="GLOBAL_COMPLETION_STARTED",
                )
                gate = self.completion.evaluate(checkpoint.spec, checkpoint, workflow)
                checkpoint = self._save_replace(
                    checkpoint,
                    state=MissionState.VERIFYING if gate.passed else MissionState.BLOCKED,
                    completion_gate=gate,
                    event_type="GLOBAL_COMPLETION_PASSED" if gate.passed else "GLOBAL_COMPLETION_BLOCKED",
                    event_details={"passed": gate.passed, "fingerprint": gate.fingerprint, "summary": gate.summary},
                )
                if not gate.passed:
                    return self._blocked_result(checkpoint, gate.summary)
                report, report_ref = self._write_final_report(checkpoint, workflow)
                checkpoint = self._save_replace(
                    checkpoint,
                    state=MissionState.COMPLETED,
                    final_report_ref=report_ref,
                    event_type="MISSION_COMPLETED",
                    event_details={"report_ref": report_ref, "report_fingerprint": report.fingerprint},
                )
                return MissionRunResultV1(
                    checkpoint.mission_id,
                    checkpoint.state,
                    checkpoint.generation,
                    checkpoint.workflow_invocations,
                    checkpoint.amendment_count,
                    checkpoint.restart_count,
                    checkpoint.completed_todo_ids,
                    checkpoint.final_report_ref,
                    "global completion gate passed",
                )

            if workflow_is_amendable(workflow) and checkpoint.amendment_count < self.policy.max_dag_amendments_per_mission:
                proposal = self.amendments.propose(checkpoint.spec, checkpoint, workflow)
                if proposal is not None:
                    self._validate_amendment(checkpoint, proposal)
                    # Persist the accepted amendment BEFORE asking the scheduler/DAG
                    # owner to apply it. If the process dies during apply, restart sees
                    # a pending persisted amendment and ensures it is applied exactly
                    # once by amendment_id semantics before workflow re-entry.
                    checkpoint = self._save_replace(
                        checkpoint,
                        state=MissionState.RECOVERING,
                        amendment_count=checkpoint.amendment_count + 1,
                        amendments=(*checkpoint.amendments, proposal),
                        event_type="DAG_AMENDMENT_ACCEPTED",
                        event_details={
                            "amendment_id": proposal.amendment_id,
                            "fingerprint": proposal.fingerprint,
                            "operations": len(proposal.operations),
                        },
                    )
                    checkpoint = self._ensure_pending_amendments_applied(checkpoint)
                    continue

            reason = workflow.final_summary or f"Phase 16 outcome={workflow.outcome.value}"
            checkpoint = self._save_replace(
                checkpoint,
                state=MissionState.BLOCKED,
                event_type="MISSION_BLOCKED",
                event_details={"reason": reason, "outcome": workflow.outcome.value},
            )
            return self._blocked_result(checkpoint, reason)

        checkpoint = self._save_replace(
            checkpoint,
            state=MissionState.BLOCKED,
            event_type="MISSION_BLOCKED",
            event_details={"reason": "max_phase16_invocations_per_run exhausted"},
        )
        return self._blocked_result(checkpoint, "max_phase16_invocations_per_run exhausted")
