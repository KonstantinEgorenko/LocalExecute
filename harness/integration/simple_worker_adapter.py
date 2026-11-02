'Opt-in SimpleWorker execution under existing workflow authorities.\n\nNo scheduler, verifier, review or recovery is owned here. The existing scheduler\nallocates the lease; RunStore owns quotas/evidence and verification owns acceptance.\n'
from dataclasses import asdict, replace
import json
from collections.abc import Mapping

from harness.context_manager import ContextBudget
from harness.context_manager.integration import ControlPlaneArtifactBridge
from harness.context_manager.models import ArtifactKind, ArtifactOrigin
from harness.control_plane import Evidence, EvidenceStatus, QuotaUsage, RunStatus
from harness.orchestrator.profile_factory import TaskProfileFactory
from harness.workflow.models import ExecutionObservationV1
from harness.simple_worker_v2 import SimpleWorkerV2, SimpleWorkerPolicyV2
from harness.simple_worker_v2.admission import SelectedWorkerAdmissionExecutor
from .repair_activation import (
    latest_repair_activation,
    record_repair_directive_presented,
    record_repair_execution_observation,
)


def _plain(value):
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


def direct_escalation_context(store, run_id, todo_id):
    'Bounded historical handoff, never substitute for current verification authority.'
    summaries=[e.to_dict()['payload'].get('payload',{}) for e in store.list_events(run_id)
        if e.to_dict()['payload'].get('evidence_type')=='DIRECT_EVENT'
        and e.to_dict()['payload'].get('todo_id')==todo_id
        and e.to_dict()['payload'].get('payload',{}).get('kind')=='ESCALATION']
    if not summaries:
        return None
    summary=summaries[-1]
    def bounded(value,limit):
        return str(value or '').encode('utf-8')[:limit].decode('utf-8','ignore')
    def stage(value):
        if not value:return None
        return {key:value.get(key) for key in ('responsibility','status','calls','changed_paths')}
    report=summary.get('last_verification')
    projected_report=None if not report else {key:report.get(key) for key in
        ('report_id','outcome','candidate_head','candidate_diff_sha256')}
    failure=summary.get('typed_failure',{})
    projected={
        'schema_version':1,'objective':bounded(summary.get('objective'),4096),
        'initial_assessment':summary.get('initial_assessment'),
        'source_targets':summary.get('source_targets',[]),'test_targets':summary.get('test_targets',[]),
        'changed_paths':summary.get('changed_paths',[]),'current_diff':bounded(summary.get('current_diff'),4096),
        'current_diff_truncated':len(str(summary.get('current_diff','')).encode('utf-8'))>4096,
        'source_stage_result':stage(summary.get('source_stage_result')),
        'test_stage_result':stage(summary.get('test_stage_result')),
        'last_verification':projected_report,
        'typed_failure':{'category':failure.get('category'),'detail':bounded(failure.get('detail'),2048),
                         'failed_gates':failure.get('failed_gates',[])[:16]},
        'next_mode':summary.get('next_mode','SIMPLE'),
        'acceptance_authority':'fresh full Phase7; provisional source result is not acceptance',
        'diff_is_untrusted_source':True}
    text=json.dumps(projected,ensure_ascii=False)
    if len(text.encode('utf-8'))>16384:
        # Preserve the complete summary in RunStore; never emit broken JSON or
        # grow an unbounded fallback prompt with a large diagnostic record.
        projected={key:projected[key] for key in ('schema_version','typed_failure','next_mode','acceptance_authority')}
        projected['summary_truncated']=True
        text=json.dumps(projected,ensure_ascii=False)
    return 'DIRECT_ESCALATION_SUMMARY_V1\n'+text


class _DurableExecutor:
    def __init__(self, adapter, run_id, contract, routing_spec, request_id, executor=None):
        self.adapter, self.run_id, self.contract = adapter, run_id, contract
        self.request_id = request_id
        loop = adapter.legacy.agent.loop
        self.inner = SelectedWorkerAdmissionExecutor(
            executor=loop.executor.inner if executor is None else executor, router=adapter.common.router,
            snapshot_provider=adapter.common.snapshot_provider, routing_spec=routing_spec,
        )

    def execute(self, request):
        store = self.adapter.legacy.workspace.run_store
        run = store.get_run(self.run_id)
        quota = run.quota_usage
        if quota.worker_calls >= self.contract.quota.max_worker_calls:
            raise RuntimeError("durable worker call quota exhausted")
        attempts = dict(quota.worker_attempts)
        model = request.routing_decision.selected_model
        attempts[model] = attempts.get(model, 0) + 1
        store.update_run(replace(run, quota_usage=QuotaUsage(
            quota.worker_calls + 1, quota.replans, quota.escalations, attempts,
        )))
        store.record_evidence(self.run_id, Evidence(
            f"{self.request_id}:model-start:{quota.worker_calls}", self.contract.todo_id,
            "SIMPLE_WORKER_EVENT", "SimpleWorkerV2", EvidenceStatus.PASSED,
            {"request_id": self.request_id, "kind": "MODEL_CALL_STARTED", "model_id": model},
        ))
        observer = self.adapter.legacy.agent.loop.observer
        args = dict(run_id=self.run_id, todo_id=self.contract.todo_id,
                    attempt_index=quota.worker_calls, decision=request.routing_decision)
        observer.model_call_started(**args, request=request)
        try:
            result = self.inner.execute(request)
        except Exception as exc:
            from harness.transport_diagnostics import error_projection
            projection=error_projection(exc)
            if 'transport_error' in projection:
                store.record_evidence(self.run_id,Evidence(
                    f"{self.request_id}:transport-error:{quota.worker_calls}",self.contract.todo_id,
                    projection['transport_error']['kind'],'NativeOllamaTransport',EvidenceStatus.FAILED,projection))
            observer.model_call_failed(**args, error=exc)
            raise
        observer.model_call_finished(**args, result=result)
        return result


def current_failed_verification_feedback(*, workspace, coordinator, contract, run_id, todo_id, lease_id):
    'Read a current FAILED verification report; never run, publish or accept verification.\n\n    The coordinator supplies the existing compiler and projected Todo store.\n    An older candidate/plan report is historical context, not current authority.\n    '
    from harness.verification.errors import VerificationIntegrityError
    from harness.verification.models import VerificationOutcome

    for event in reversed(workspace.run_store.list_events(run_id)):
        evidence = event.to_dict()["payload"]
        if (evidence.get("evidence_type") != "VERIFICATION_REPORT"
                or evidence.get("todo_id") != todo_id
                or evidence.get("source") != "VerificationRunner"):
            continue
        if evidence.get("status") != EvidenceStatus.FAILED.value:
            return None
        if coordinator is None or contract is None:
            raise VerificationIntegrityError("current verification feedback authority is not configured")
        identity = evidence.get("payload", {})
        if not isinstance(identity, Mapping):
            raise VerificationIntegrityError("verification feedback Evidence payload is invalid")
        report_id = identity.get("report_id")
        if not isinstance(report_id, str) or not report_id:
            raise VerificationIntegrityError("verification feedback report identity missing")
        report = coordinator.publisher.find_existing(report_id=report_id)
        if report is None:
            raise VerificationIntegrityError("verification feedback report artifact missing")
        if (report.run_id != run_id or report.todo_id != todo_id or report.lease_id != lease_id
                or report.outcome is not VerificationOutcome.FAILED
                or report.candidate_diff_sha256 != identity.get("candidate_diff_sha256")
                or report.plan_id != identity.get("plan_id")
                or report.plan_fingerprint != identity.get("plan_fingerprint")
                or identity.get("outcome") != VerificationOutcome.FAILED.value):
            raise VerificationIntegrityError("verification feedback report identity mismatch")
        snapshot = coordinator.manager.snapshot(lease_id)
        if (snapshot.head_commit != report.candidate_head
                or snapshot.diff_sha256 != report.candidate_diff_sha256):
            return None
        run = coordinator.run_store.get_run(run_id)
        todo = coordinator.run_store.get_todo(todo_id)
        lease = coordinator.manager.get_active(lease_id)
        plan = coordinator.compiler.compile(run=run, todo=todo, contract=contract, lease=lease)
        if report.plan_id != plan.plan_id or report.plan_fingerprint != plan.fingerprint:
            return None
        coordinator.publisher.validate_report_identity(
            report, plan, head=snapshot.head_commit, diff_sha256=snapshot.diff_sha256)
        ref = coordinator.artifact_store.get_artifact(
            coordinator.publisher.artifact_id(report.report_id)).ref
        return report, ref
    return None


def verification_feedback_summary(report):
    """Bounded projection of already-authoritative gate results, not a new gate."""
    from harness.verification.models import GateOutcome

    failed = tuple(item for item in report.results
                   if item.required and item.outcome is not GateOutcome.PASSED)
    limit = 16
    return {
        "schema_version": 1,
        "state": "CURRENT_FAILED_CANDIDATE",
        "report_id": report.report_id,
        "run_id": report.run_id,
        "todo_id": report.todo_id,
        "lease_id": report.lease_id,
        "candidate_head": report.candidate_head,
        "candidate_diff_sha256": report.candidate_diff_sha256,
        "plan_fingerprint": report.plan_fingerprint,
        "failed_required_gates": [{
            "gate_id": item.gate_id,
            "gate_type": item.gate_type.value,
            "outcome": item.outcome.value,
            "detail": item.detail.encode("utf-8")[:512].decode("utf-8", "ignore"),
            "detail_truncated": len(item.detail.encode("utf-8")) > 512,
        } for item in failed[:limit]],
        "observed_passed_gate_ids": [item.gate_id for item in report.results
                                    if item.outcome is GateOutcome.PASSED][:limit],
        "failed_required_gate_count": len(failed),
        "gate_count": len(report.results),
        "truncated": len(report.results) > limit,
        "acceptance_authority": "fresh Phase7 report for the resulting candidate",
        "candidate_change_invalidates_observed_results": True,
    }


class SimpleWorkerWorkflowAdapter:
    def __init__(self, *, legacy, common, trace_registry, verification=None, simple_native_mutation=False,
                 discovery_capability=None):
        self.legacy, self.common, self.trace_registry = legacy, common, trace_registry
        self.verification = verification
        # Explicit compatibility boundary for historical standalone adapters.
        # Production composition always enables discovery/native mutation.
        self.simple_native_mutation = simple_native_mutation
        self.discovery_capability = discovery_capability

    def _resolved_verification_feedback(self, run_id, todo_id, lease_id, contract):
        return current_failed_verification_feedback(
            workspace=self.legacy.workspace, coordinator=self.verification,
            contract=contract, run_id=run_id, todo_id=todo_id, lease_id=lease_id)

    def _verification_feedback(self, run_id, todo_id, lease_id, contract=None):
        """Compatibility facade: return only the required ArtifactRef."""
        resolved = self._resolved_verification_feedback(run_id, todo_id, lease_id, contract)
        return None if resolved is None else resolved[1]

    def _failed_execution_raw(self, todo, run_id, lease_id, execution_failure_kind):
        raw = {
            "run_id": run_id,
            "lease_id": lease_id,
            "typed_scenario_execution": True,
            "failure_kind": execution_failure_kind,
            "execution_failure_kind": execution_failure_kind,
        }
        resolved = self._resolved_verification_feedback(
            run_id, todo.todo_id, lease_id, todo.work_contract)
        if resolved is not None:
            report, _ = resolved
            raw.update({
                "failure_kind": "VERIFICATION_FAILED",
                "candidate_failure": "VERIFICATION_FAILED",
                "candidate_verification_report_id": report.report_id,
                "verification_feedback": verification_feedback_summary(report),
            })
        return raw

    @staticmethod
    def _record_execution(store, run_id, todo_id, request_id, observation):
        store.record_evidence(run_id, Evidence(
            request_id + ":execution", todo_id, "SIMPLE_WORKER_EXECUTION", "SimpleWorkerV2",
            EvidenceStatus.PASSED if observation.finalized else EvidenceStatus.FAILED,
            {"request_id": request_id, "observation": {
                "profile_id": observation.profile_id, "finalized": observation.finalized,
                "changed_paths": list(observation.changed_paths), "model_calls": observation.model_calls,
                "tool_calls": observation.tool_calls, "outcome": observation.outcome,
                "summary": observation.summary, "raw": _plain(observation.raw),
            }},
        ))
        return observation

    def execute(self, todo, *, profile_id, objective_with_context, attempt_index, fresh_workspace):
        if ('SCENARIO_SCOPE' in todo.work_contract.constraints
                and todo.work_contract.constraints.get('EXECUTION_DEPTH_POLICY', True) is True):
            from .direct_execution_adapter import DirectExecutionAdapter
            direct = DirectExecutionAdapter(self).execute(todo, profile_id=profile_id, attempt_index=attempt_index)
            if direct is not None:
                return direct
        workspace = self.legacy.workspace
        store = workspace.run_store
        run_id, lease = workspace.current(todo.todo_id)
        request_id = f"{run_id}:simple-worker:{attempt_index}:{profile_id}"
        previous = [e.to_dict()["payload"] for e in store.list_events(run_id)
                    if e.to_dict()["payload"].get("evidence_type") == "SIMPLE_WORKER_EXECUTION"]
        for item in previous:
            payload = item.get("payload", {})
            if payload.get("request_id") == request_id:
                value = payload["observation"]
                return ExecutionObservationV1(**value)
        # Interrupted calls cannot silently replay mutations. Durable evidence
        # is retained and the control plane receives the exact interrupted cause.
        interrupted = [e.to_dict()["payload"] for e in store.list_events(run_id)
                       if e.to_dict()["payload"].get("evidence_type") == "SIMPLE_WORKER_EVENT"
                       and e.to_dict()["payload"].get("payload", {}).get("request_id") == request_id]
        if interrupted:
            store.update_run(replace(store.get_run(run_id), status=RunStatus.NEEDS_ATTENTION))
            return ExecutionObservationV1(profile_id, False, (), 0, 0, "NEEDS_ATTENTION",
                "interrupted SimpleWorker execution requires control-plane recovery",
                {"run_id": run_id, "lease_id": lease.lease_id, "failure_kind": "TOOL_FAILURE"})

        # No admitted execution means no reopening of an attention state. In
        # particular, an exhausted fallback profile cannot make an unfinished
        # candidate verifiable merely by returning before its first model call.
        remaining = todo.work_contract.quota.max_worker_calls - store.get_run(run_id).quota_usage.worker_calls
        if remaining <= 0:
            store.update_run(replace(store.get_run(run_id), status=RunStatus.NEEDS_ATTENTION))
            observation = ExecutionObservationV1(profile_id, False, (), 0, 0, "QUOTA_EXHAUSTED",
                "durable worker call quota exhausted",
                self._failed_execution_raw(todo, run_id, lease.lease_id, "WORKER_QUOTA"))
            return self._record_execution(store, run_id, todo.todo_id, request_id, observation)

        resolved_feedback = self._resolved_verification_feedback(
            run_id, todo.todo_id, lease.lease_id, todo.work_contract)
        verification_feedback = None if resolved_feedback is None else resolved_feedback[1]
        feedback_summary = (None if resolved_feedback is None else
                            verification_feedback_summary(resolved_feedback[0]))
        self.legacy._reopen_for_slice(run_id)
        contract = replace(todo.work_contract, objective=objective_with_context)
        escalation = direct_escalation_context(store, run_id, todo.todo_id)
        if escalation is not None:
            contract = replace(contract, objective=contract.objective + '\n\n' + escalation)
        repair = latest_repair_activation(store, self.common.artifacts,
            run_id=run_id, todo_id=todo.todo_id, lease_id=lease.lease_id)
        if repair is not None:
            contract = replace(contract, objective=contract.objective + "\n\n" + repair.directive())
        profile = self.legacy.policy.profile(profile_id)
        # workflow has already projected its bounded pack into execution objective.
        # Resolve durable contract metadata independently: embedding that rendered
        # pack in the contract artifact again duplicates/JSON-escapes source.
        bundle = self.legacy.agent.context_resolver.assemble(run_id, todo.work_contract,
            ContextBudget(profile.model_context_tokens, profile.max_items + 8, profile.max_bytes),
            required_artifacts=(() if verification_feedback is None else (verification_feedback,)),
            request_id=request_id + ":context")
        session = workspace.resolve(run_id=run_id, contract=todo.work_contract)
        # This seam is after DIRECT return and after existing replay/quota guards.
        # The original contract scopes continuation; rendered recovery context
        # cannot silently change responsibility identity.
        from harness.simple_worker_v2.interaction_state import restore_interaction_state, persist_state
        interaction = restore_interaction_state(store, run_id=run_id, todo_id=todo.todo_id,
            contract=todo.work_contract, session=session)
        if feedback_summary is not None:
            interaction = interaction.with_verification(json.dumps({
                'state': 'HISTORICAL_FAILED_CANDIDATE',
                'report_id': feedback_summary.get('report_id'),
                'failed_required_gates': [g.get('gate_id') for g in feedback_summary.get('failed_required_gates', ())],
                'acceptance': False}, sort_keys=True))
        routing_spec = self.legacy._agent_routing_spec(todo)
        routing_profile = TaskProfileFactory().build(routing_spec, self.common.snapshot_provider(), ())
        decision = self.legacy.agent.loop.router.route(routing_profile)
        generic_decision = decision
        # Reuse the existing durable depth-policy decision. Explicit SIMPLE
        # opt-out and DIRECT escalation are existing fallback contracts; a
        # STANDARD/ADVANCED decision must keep its generic model authority.
        selected_mode = 'SIMPLE'
        if todo.work_contract.constraints.get('EXECUTION_DEPTH_POLICY', True) is not False:
            for event in reversed(store.list_events(run_id)):
                evidence = event.to_dict()['payload']
                if evidence.get('evidence_type') != 'DIRECT_EVENT' or evidence.get('todo_id') != todo.todo_id:
                    continue
                payload = evidence.get('payload', {})
                if payload.get('kind') == 'ESCALATION':
                    selected_mode = payload.get('next_mode')
                    break
                if payload.get('kind') == 'EXECUTION_POLICY':
                    selected_mode = payload.get('policy', {}).get('mode')
                    break
        qualified_discovery = (self.simple_native_mutation and self.discovery_capability is not None
                               and selected_mode == 'SIMPLE')
        if qualified_discovery:
            decision = self.discovery_capability.select(profile=routing_profile, router=self.common.router)
        recorder = self.trace_registry.resolve_for_run(run_id)
        index = len(store.list_events(run_id))

        def observe(kind, payload, *, actor=None):
            nonlocal index
            index += 1
            store.record_evidence(run_id, Evidence(
                f"{request_id}:event:{index}", todo.todo_id,
                "AGENT_PROTOCOL_FAILURE" if kind == "PROTOCOL_ERROR" else "SIMPLE_WORKER_EVENT",
                "SimpleWorkerV2", EvidenceStatus.FAILED if kind in ("PROTOCOL_ERROR", "TOOL_LOOP") else EvidenceStatus.PASSED,
                {"request_id": request_id, "kind": kind, **payload},
            ))
            if recorder is not None:
                recorder.record("SIMPLE_WORKER_" + kind, run_id=run_id, todo_id=todo.todo_id,
                                actor=actor or decision.selected_model, payload=payload)
                if kind == "ACI_ACTION":
                    recorder.record("TOOL_CALL_STARTED", run_id=run_id, todo_id=todo.todo_id,
                                    actor="WorkspaceAciV2", payload={"aci_action": payload["action"]})

        if repair is not None:
            record_repair_directive_presented(
                run_store=store, run_id=run_id, todo_id=todo.todo_id,
                activation=repair, request_id=request_id)
        if feedback_summary is not None:
            observe("VERIFICATION_FEEDBACK_PRESENTED", feedback_summary)
        observe('INTERACTION_STATE_RESTORED', {
            'state_version': interaction.state_version, 'execution_epoch': interaction.execution_epoch,
            'fresh_observations': sum(o.fresh for o in interaction.observations),
            'stale_observations': sum(not o.fresh for o in interaction.observations),
            'relevant_paths': list(interaction.relevant_paths),
            'responsibility_fingerprint': interaction.responsibility_fingerprint})
        if self.simple_native_mutation:
            if qualified_discovery:
                observe('SIMPLE_DISCOVERY_CAPABILITY_BOUND', {
                    **self.discovery_capability.binding.to_dict(),
                    'generic_router_candidate': generic_decision.selected_model,
                    'routing_task_spec': asdict(routing_spec),
                    'run_id': run_id, 'todo_id': todo.todo_id})
            from .simple_native_adapter import execute_simple_native
            return execute_simple_native(self,todo=todo,run_id=run_id,lease=lease,session=session,
                interaction=interaction,decision=decision,routing_spec=routing_spec,request_id=request_id,
                profile_id=profile_id,remaining=remaining,observe=observe,context_bundle=bundle)
        result = SimpleWorkerV2(
            executor=_DurableExecutor(self, run_id, contract, routing_spec, request_id),
            policy=SimpleWorkerPolicyV2(max_model_turns=min(12, remaining)), observer=observe,
        ).execute(decision=decision, contract=contract, context_bundle=bundle,
                  workspace_session=session, request_id=request_id, task_id=todo.todo_id,
                  verification_feedback=feedback_summary,
                  candidate_dirty_paths=workspace.manager.snapshot(lease.lease_id).dirty_paths,
                  interaction_state=interaction,
                  persist_interaction_state=lambda state: persist_state(store, state))
        if repair is not None:
            candidate = workspace.manager.snapshot(lease.lease_id)
            record_repair_execution_observation(
                run_store=store, run_id=run_id, todo_id=todo.todo_id,
                activation=repair, request_id=request_id,
                candidate_diff_after=candidate.diff_sha256,
                tool_calls_delta=result.aci_calls)
        success = result.outcome.value == "SUCCESS"
        raw = {"run_id": run_id, "lease_id": lease.lease_id,
               "agent_result": {**result.to_dict(), "tool_calls": result.aci_calls},
               "typed_scenario_execution": True}
        from harness.workspace.diagnosis_domain import project_diagnosis_paths
        latest_interaction = restore_interaction_state(store, run_id=run_id, todo_id=todo.todo_id,
            contract=todo.work_contract, session=session)
        raw['diagnosis_path_domain'] = project_diagnosis_paths(
            session=session, contract=todo.work_contract, interaction=latest_interaction,
            changed_paths=workspace.manager.snapshot(lease.lease_id).dirty_paths).to_dict()
        if success:
            content = {"summary": result.final_summary, "changed_paths": list(result.changed_paths),
                       "model_id": result.model_id, "model_turns": result.model_turns,
                       "tool_calls": result.aci_calls, "context_bundle_id": bundle.bundle_id}
            ControlPlaneArtifactBridge(store, self.common.artifacts).record_text_evidence(
                run_id, artifact_id=request_id + ":worker-result", todo_id=todo.todo_id,
                evidence_id=request_id + ":worker-result", evidence_type="WORKER_RESULT",
                source="SimpleWorkerV2", content=json.dumps(content),
                kind=ArtifactKind.WORKER_RESULT, origin=ArtifactOrigin.WORKER,
                scope_tags=result.changed_paths, evidence_payload=content,
            )
        else:
            store.update_run(replace(store.get_run(run_id), status=RunStatus.NEEDS_ATTENTION))
            execution_failure_kind = (result.failure_kind.value if result.failure_kind is not None else
                "WORKER_QUOTA" if result.outcome.value == "QUOTA_EXHAUSTED" else "TOOL_FAILURE")
            raw.update(self._failed_execution_raw(
                todo, run_id, lease.lease_id, execution_failure_kind))
        observation = ExecutionObservationV1(profile_id, success, result.changed_paths,
            result.model_turns, result.aci_calls, result.outcome.value,
            result.final_summary or result.reason, raw)
        return self._record_execution(store, run_id, todo.todo_id, request_id, observation)
