'Discovery handoff inside workflow; no new routing/recovery/acceptance owner.'
from dataclasses import replace
import json

from harness.control_plane import Evidence, EvidenceStatus, RunStatus
from harness.context_manager.integration import ControlPlaneArtifactBridge
from harness.context_manager.models import ArtifactKind, ArtifactOrigin
from harness.execution_policy import DIRECT_CAPABILITY_V1, DIRECT_CAPABILITY_MODELS, EscalationSummaryV1
from harness.orchestrator.profile_factory import TaskProfileFactory
from harness.workflow.models import ExecutionObservationV1
from harness.router.scoring import RoutingDecision, DecisionType
from harness.simple_worker_v2.discovery import SimpleDiscoveryV1
from harness.simple_worker_v2.interaction_state import persist_state, restore_interaction_state
from harness.workspace.diagnosis_domain import project_diagnosis_paths
from .simple_worker_adapter import _DurableExecutor
from .native_mutation import execute_native_mutation
from .native_transport_recovery import NativeTransportCheckpoint


def execute_simple_native(owner, *, todo, run_id, lease, session, interaction,
                          decision, routing_spec, request_id, profile_id, remaining, observe, context_bundle):
    workspace = owner.legacy.workspace
    store = workspace.run_store
    contract = todo.work_contract
    editor_model = DIRECT_CAPABILITY_MODELS[DIRECT_CAPABILITY_V1]
    discovery = None
    native = None
    native_tools = 0
    intent = None
    initial_usage = store.get_run(run_id).quota_usage.worker_calls
    def finish(failure=None):
        snapshot = workspace.manager.snapshot(lease.lease_id)
        latest = restore_interaction_state(store, run_id=run_id, todo_id=todo.todo_id,
            contract=contract, session=session)
        # Existing content/version invalidation after native edits; no new cache.
        persist_state(store, latest)
        changed = tuple(snapshot.dirty_paths)
        raw = {'run_id':run_id, 'lease_id':lease.lease_id, 'typed_scenario_execution':True,
            'execution_mode':'SIMPLE_DISCOVERY_NATIVE_V1',
            'exploration_model':decision.selected_model, 'mutation_model':editor_model,
            'edit_intent':None if intent is None else intent.to_dict(), 'native_result':native,
            'diagnosis_path_domain':project_diagnosis_paths(session=session,contract=contract,
                interaction=latest,changed_paths=changed).to_dict()}
        calls = store.get_run(run_id).quota_usage.worker_calls-initial_usage
        tools = (0 if discovery is None else discovery.tool_calls)+native_tools
        success = failure is None
        summary = 'SOURCE and regression TEST verified by Phase7' if success else failure
        if success:
            content = {'summary':summary, 'changed_paths':list(changed), 'model_turns':calls,
                'tool_calls':tools, 'exploration_model':decision.selected_model, 'mutation_model':editor_model}
            ControlPlaneArtifactBridge(store,owner.common.artifacts).record_text_evidence(
                run_id,artifact_id=request_id+':worker-result',todo_id=todo.todo_id,evidence_id=request_id+':worker-result',
                evidence_type='WORKER_RESULT',source='SimpleNativeAdapter',content=json.dumps(content),
                kind=ArtifactKind.WORKER_RESULT,origin=ArtifactOrigin.WORKER,scope_tags=changed,evidence_payload=content)
        else:
            stages=[] if native is None else native['stages']
            escalation=EscalationSummaryV1(contract.objective,{'execution_mode':'SIMPLE_DISCOVERY_NATIVE_V1'},
                () if intent is None else intent.source_targets,() if intent is None else intent.test_targets,
                changed,getattr(snapshot,'diff_text','')[:65536],
                next((s for s in stages if s['responsibility']=='SOURCE'),None),
                next((s for s in stages if s['responsibility']=='TEST'),None),
                stages[-1].get('report') if stages else None,{'category':failure},'STANDARD')
            store.record_evidence(run_id,Evidence(request_id+':native-escalation',todo.todo_id,
                'SIMPLE_NATIVE_ESCALATION','SimpleNativeAdapter',EvidenceStatus.FAILED,escalation.to_dict()))
            observe('NATIVE_ESCALATION',escalation.to_dict(),actor=editor_model)
            store.update_run(replace(store.get_run(run_id),status=RunStatus.NEEDS_ATTENTION))
            raw.update(owner._failed_execution_raw(todo,run_id,lease.lease_id,
                'WORKER_QUOTA' if failure in ('DISCOVERY_QUOTA','INSUFFICIENT_NATIVE_BUDGET') else 'TOOL_FAILURE'))
            raw['native_failure_category']=failure
        observation=ExecutionObservationV1(profile_id,success,changed,calls,tools,
            'SUCCESS' if success else 'NEEDS_ATTENTION',summary,raw)
        return owner._record_execution(store,run_id,todo.todo_id,request_id,observation)

    registry=getattr(owner.common,'registry',None)
    definition=None if registry is None else registry.get(editor_model)
    model={'logical_id':editor_model}
    if definition is not None:
        model.update(tag=definition.ollama_model,runtime=dict(definition.runtime),
                     context=dict(definition.context))
    try:
        checkpoint=NativeTransportCheckpoint(store=store,session=session,contract=contract,
            lease_id=lease.lease_id,model=model)
        intent=checkpoint.resume_intent()
    except Exception as exc:
        return finish(getattr(exc,'category','AMBIGUOUS_NATIVE_INVOCATION'))
    if owner.verification is None or remaining < (1 if intent is not None else 5):
        return finish('INSUFFICIENT_NATIVE_BUDGET')
    if intent is None:
        from .simple_discovery_capability import SIMPLE_DISCOVERY_V1
        capability = owner.discovery_capability if decision.routing_reason == SIMPLE_DISCOVERY_V1 else None
        discovery_executor = None if capability is None else capability.guarded_executor(
            lambda kind, payload: observe(kind, {**payload, 'run_id':run_id, 'todo_id':todo.todo_id}))
        discovery=SimpleDiscoveryV1(executor=_DurableExecutor(owner,run_id,contract,routing_spec,request_id,
                executor=discovery_executor),
            observer=observe,persist=lambda state:persist_state(store,state),max_turns=min(12,remaining-4)).execute(
                decision=decision,contract=contract,session=session,state=interaction,request_id=request_id,
                context_bundle=context_bundle)
        intent=discovery.intent
    else:
        observe('NATIVE_RESPONSIBILITY_RESTORED',{'stage':'TEST','source_preserved':True},actor=editor_model)
    if intent is None:
        return finish(discovery.failure)
    try:
        intent.validate_current(session,contract)
        checkpoint.initialize(intent)
        router=getattr(owner.common.router,'inner',owner.common.router)
        profile=TaskProfileFactory().build(routing_spec,owner.common.snapshot_provider(),())
        candidate=router._eligibility(editor_model,profile,router._branch_for(profile.task_type.value),set(),None)
        if candidate.rejection_reasons:
            return finish('NATIVE_CAPABILITY_UNAVAILABLE')
        editor_decision=RoutingDecision(DecisionType.WORKER,editor_model,None,1.0,DIRECT_CAPABILITY_V1,(),
            candidate.feasibility.to_dict() if candidate.feasibility else None,None)
        executor=owner.common.executor
        def native_observe(kind,payload):
            nonlocal native_tools
            if kind=='EDIT_APPLIED':native_tools+=1
            observe('NATIVE_'+kind,{'role':'MUTATION','model':editor_model,**payload},actor=editor_model)
            if kind=='EDIT_APPLIED':
                recorder=owner.trace_registry.resolve_for_run(run_id)
                if recorder is not None:
                    recorder.record('TOOL_CALL_STARTED',run_id=run_id,todo_id=todo.todo_id,
                        actor='WorkspaceAciV2',payload={'role':'MUTATION',**payload})
        observe('MODEL_ROLES',{'exploration_model':decision.selected_model,'mutation_model':editor_model,
            'capability':DIRECT_CAPABILITY_V1})
        # Original objective only; recovery prose and full discovery transcript
        # never become editor instructions or permission sources.
        objective='UNTRUSTED EXPLORER HYPOTHESES\n'+json.dumps({
            'provenance':'MODEL_HYPOTHESIS','hypotheses':intent.relevant_facts,
            'verification_obligations':intent.verification_obligations},ensure_ascii=False)
        native=execute_native_mutation(owner=owner,todo=todo,run_id=run_id,lease=lease,session=session,
            decision=editor_decision,
            executor=_DurableExecutor(owner,run_id,contract,routing_spec,request_id,executor=executor),
            client=executor.client,observe=native_observe,objective=objective,
            sources=intent.source_targets,tests=intent.test_targets,
            expected_initial_versions=(*intent.source_versions,*intent.test_versions),checkpoint=checkpoint)
        if native['outcome']!='VERIFIED':
            return finish('NATIVE_MUTATION_FAILED')
        return finish()
    except Exception as exc:
        observe('NATIVE_HANDOFF_FAILED',{'category':getattr(exc,'category','NATIVE_HANDOFF_FAILED'),
            'exception':type(exc).__name__,'detail':str(exc)[:2048]},actor=editor_model)
        return finish(getattr(exc,'category','NATIVE_HANDOFF_FAILED'))
