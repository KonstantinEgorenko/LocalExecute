'Bounded native source/test execution inside the existing workflow workflow.'
from dataclasses import replace
import json

from harness.control_plane import Evidence, EvidenceStatus
from harness.context_manager.integration import ControlPlaneArtifactBridge
from harness.context_manager.models import ArtifactKind, ArtifactOrigin
from harness.edit_transport_experiment import EditResponsibilityExperiment
from harness.execution_policy import (TaskAssessmentV1, ExecutionPolicyV1, ExecutionModeV1,
    EscalationSummaryV1, DIRECT_CAPABILITY_MODELS)
from harness.execution_policy import isolated_computation_source
from harness.execution_policy import MULTIFILE_CAPABILITY_V1, MULTIFILE_SOURCE_LIMIT
from harness.orchestrator.profile_factory import TaskProfileFactory
from harness.workflow.models import ExecutionObservationV1
from harness.repo_intelligence.scanner import _kind
from harness.repo_intelligence.models import RepositoryFileKind
from harness.router.scoring import RoutingDecision, DecisionType
from harness.workspace import Workspace
from harness.workspace.errors import WorkspaceError
from .simple_worker_adapter import _DurableExecutor, _plain


class DirectExecutionAdapter:
    def __init__(self, owner):
        self.owner = owner

    def execute(self, todo, *, profile_id, attempt_index):
        owner=self.owner; workspace=owner.legacy.workspace; store=workspace.run_store
        run_id,lease=workspace.current(todo.todo_id)
        request_id=f'{run_id}:direct:{todo.todo_id}'
        evidence=[e.to_dict()['payload'] for e in store.list_events(run_id)]
        previous=[e for e in evidence if e.get('evidence_type')=='DIRECT_EXECUTION'
                  and e.get('todo_id')==todo.todo_id]
        if previous:
            payload=previous[-1]['payload']
            if payload.get('attempt_index')==attempt_index:
                return ExecutionObservationV1(**payload['observation'])
            return None
        prior=[e for e in evidence if e.get('evidence_type')=='DIRECT_EVENT'
               and e.get('todo_id')==todo.todo_id]
        # Never restart an interrupted responsibility or repeat DIRECT in recovery.
        if prior:
            selected=any(e['payload'].get('policy',{}).get('mode')=='DIRECT'
                or e['payload'].get('policy',{}).get('capability')==MULTIFILE_CAPABILITY_V1 for e in prior)
            if not selected:
                return None
            from .source_repair import SourceRepairPolicy, SourceExecutionFailure
            if SourceRepairPolicy.from_constraints(todo.work_contract.constraints) is not None:
                raise SourceExecutionFailure('DIRECT_INTERRUPTED: bounded responsibility cannot be replayed')
            if not any(e['payload']['kind']=='ESCALATION' for e in prior):
                assessment=next(e['payload']['assessment'] for e in prior
                                if e['payload'].get('policy',{}).get('mode')=='DIRECT')
                snapshot=workspace.manager.snapshot(lease.lease_id)
                last_report=next((e['payload']['report'] for e in reversed(prior)
                                  if e['payload']['kind']=='EDIT_VERIFICATION'),None)
                summary=EscalationSummaryV1(todo.work_contract.objective,assessment,
                    tuple(assessment['source_targets']),tuple(assessment['test_targets']),
                    tuple(snapshot.dirty_paths),snapshot.diff_text[:65536],None,None,last_report,
                    {'category':'DIRECT_INTERRUPTED','last_event_kind':prior[-1]['payload']['kind'],
                     'responsibility':prior[-1]['payload'].get('stage')},'SIMPLE')
                self._event(store,run_id,todo,request_id,'ESCALATION',summary.to_dict())
            return None
        contract=todo.work_contract
        if store.get_run(run_id).quota_usage.worker_calls >= contract.quota.max_worker_calls:
            return None
        session=workspace.resolve(run_id=run_id,contract=contract)
        constraints=contract.constraints
        from harness.scenarios.qualified_shape import SHAPE, QualifiedExecutionShapeV1
        shape = (QualifiedExecutionShapeV1.from_dict(constraints[SHAPE])
                 if SHAPE in constraints else None)
        if constraints.get(MULTIFILE_SOURCE_LIMIT) == 2 and shape is None:
            from harness.scenarios.scope_evidence import PlanningContractError
            raise PlanningContractError('QUALIFIED_EXECUTION_SHAPE_INVALID', reason='required multifile proof missing; no fallback')
        if shape is not None:
            shape.validate_contract(contract, todo.work_contract.constraints.get('verification_required_gates', ()) + ('regression_change',))
        routing=owner.legacy._agent_routing_spec(todo)
        try:
            files=Workspace(session).list_files('',recursive=True)
            paths=tuple(e['path'] for e in files if e['type']=='file' and e['path'].endswith('.py'))
            sources=tuple(p for p in paths if _kind(p) is not RepositoryFileKind.TEST)
            tests=tuple(p for p in paths if _kind(p) is RepositoryFileKind.TEST)
            context_bytes=sum(len(Workspace(session).read_text(p)[1].encode('utf-8')) for p in paths)
        except (ValueError, WorkspaceError):
            sources=tests=();context_bytes=0
        risk=constraints.get('SCENARIO_RISK',getattr(routing.risk,'value', 'UNKNOWN'))
        declared_risk=risk
        risk_evidence=None
        ambiguity=constraints.get('SCENARIO_AMBIGUITY',getattr(routing.reasoning_need,'value','UNKNOWN'))
        scope=constraints.get('SCENARIO_SCOPE',getattr(routing.scope,'value','UNKNOWN'))
        run=store.get_run(run_id)
        plan=None
        if owner.verification is not None:
            plan=owner.verification.compiler.compile(run=run,todo=owner.verification.run_store.get_todo(todo.todo_id),
                contract=contract,lease=lease)
        gates=set() if plan is None else {g.gate_id for g in plan.gates if g.required}
        verification_ok={'candidate_diff','scope_guard','python_syntax','pytest','regression_change'} <= gates
        # Selector sees the request only. Repository observation can establish
        # bounded operational effects; an explicit HIGH risk is never downgraded.
        if (risk=='MEDIUM' and scope in ('LOCAL','SINGLE_FILE') and len(sources)==1
                and len(tests)==1 and verification_ok and 0<context_bytes<=32768
                and isolated_computation_source(Workspace(session).read_text(sources[0])[1])):
            risk='LOW';risk_evidence='ORIGINAL_SOURCE_ISOLATED_COMPUTATION'
        assessment=TaskAssessmentV1(
            constraints.get('SCENARIO_TYPE','UNKNOWN'),
            2 if scope in ('LOCAL','SINGLE_FILE') and risk=='LOW' and ambiguity=='LOW' else 7,
            scope,ambiguity,risk,
            'HIGH' if (len(sources)==1 or len(sources)==2 and type(constraints.get(MULTIFILE_SOURCE_LIMIT)) is int
                and constraints[MULTIFILE_SOURCE_LIMIT]==2) and len(tests)==1 and 0<context_bytes<=32768 else 'LOW',
            'HIGH' if verification_ok else 'LOW',
            constraints.get('research_required',False) is not False,
            constraints.get('REQUIRES_REGRESSION_CHANGE',False) is True,sources,tests,
            run.quota_usage.worker_calls>0 or any(e.get('evidence_type')=='SIMPLE_WORKER_EXECUTION'
                and e.get('todo_id')==todo.todo_id and e.get('status')=='FAILED' for e in evidence),
            declared_risk,risk_evidence,
            type(constraints.get(MULTIFILE_SOURCE_LIMIT)) is int and constraints[MULTIFILE_SOURCE_LIMIT]==2,
            shape)
        policy=ExecutionPolicyV1.select(assessment)
        if shape is not None and policy.capability != MULTIFILE_CAPABILITY_V1:
            from harness.scenarios.scope_evidence import PlanningContractError
            raise PlanningContractError('QUALIFIED_EXECUTION_SHAPE_INVALID', reason='runtime assessment rejected; no single-file fallback')
        self._event(store,run_id,todo,request_id,'EXECUTION_POLICY',{
            'assessment':assessment.to_dict(),'policy':policy.to_dict()})
        if policy.mode is not ExecutionModeV1.DIRECT and policy.capability != MULTIFILE_CAPABILITY_V1:
            return None
        model=DIRECT_CAPABILITY_MODELS[policy.capability if policy.mode is ExecutionModeV1.DIRECT else 'DIRECT_NATIVE_SPLIT_V1']
        stages=[]
        def escalate(failure):
            snapshot=workspace.manager.snapshot(lease.lease_id)
            summary=EscalationSummaryV1(contract.objective,assessment.to_dict(),sources,tests,
                tuple(snapshot.dirty_paths),snapshot.diff_text[:65536],
                next((s for s in stages if s['responsibility']=='SOURCE'),None),
                next((s for s in stages if s['responsibility']=='TEST'),None),
                stages[-1].get('report') if stages else None,failure,'SIMPLE')
            self._event(store,run_id,todo,request_id,'ESCALATION',summary.to_dict())
            return None
        if contract.quota.max_worker_calls-run.quota_usage.worker_calls<4:
            return escalate({'category':'INSUFFICIENT_DIRECT_BUDGET'})
        router=getattr(owner.common.router,'inner',owner.common.router)
        profile=TaskProfileFactory().build(routing,owner.common.snapshot_provider(),())
        # Existing router hard eligibility and lifecycle admission remain authoritative.
        candidate=router._eligibility(model,profile,router._branch_for(profile.task_type.value),set(),None)
        if candidate.rejection_reasons:
            return escalate({'category':'DIRECT_CAPABILITY_UNAVAILABLE','reasons':list(candidate.rejection_reasons)})
        decision=RoutingDecision(DecisionType.WORKER,model,None,1.0,policy.capability,(),
            candidate.feasibility.to_dict() if candidate.feasibility else None,None)
        recorder=owner.trace_registry.resolve_for_run(run_id)
        aci_calls=0
        def observe(kind,payload):
            nonlocal aci_calls
            payload={**payload,'run_id':run_id,'todo_id':todo.todo_id,
                'lease_id':lease.lease_id,'worktree':str(session.root)}
            if kind=='EDIT_APPLIED':
                aci_calls+=1
            self._event(store,run_id,todo,request_id,kind,payload)
            if recorder is not None:
                recorder.record('DIRECT_'+kind,run_id=run_id,todo_id=todo.todo_id,actor=model,payload=payload)
                if kind=='EDIT_APPLIED':
                    recorder.record('TOOL_CALL_STARTED',run_id=run_id,todo_id=todo.todo_id,actor='WorkspaceAciV2',payload=payload)
        observe('SELECTED_MODEL',{'model':model,'capability':policy.capability})
        owner.legacy._reopen_for_slice(run_id)
        from .native_mutation import execute_native_mutation
        from .source_repair import SourceRepairPolicy, SourceExecutionFailure
        source_policy=SourceRepairPolicy.from_constraints(contract.constraints)
        checkpoint=None
        if source_policy is not None:
            from .native_transport_recovery import NativeTransportCheckpoint
            checkpoint=NativeTransportCheckpoint(store=store,session=session,contract=contract,
                lease_id=lease.lease_id,model=model,reopen_semantic_failure=False)
        result=execute_native_mutation(owner=owner,todo=todo,run_id=run_id,lease=lease,
            session=session,decision=decision,
            executor=_DurableExecutor(owner,run_id,contract,routing,request_id),
            client=owner.legacy.agent.loop.executor.inner.client,observe=observe,
            objective=contract.objective,sources=sources,tests=tests,checkpoint=checkpoint)
        stages=result['stages']
        if result['outcome']!='VERIFIED':
            if source_policy is not None:
                # Exception is handled by the existing durable mission boundary;
                # no telemetry interception, SIMPLE fallback or outer recovery.
                failure=stages[-1].get('failure') or {'category':'DIRECT_FAILED'}
                self._event(store,run_id,todo,request_id,'TERMINAL_EXECUTION_FAILURE',
                    {'failure':failure,'report':stages[-1].get('report'),
                     'diff':workspace.manager.snapshot(lease.lease_id).diff_text[:65536]})
                from .source_repair import TestExecutionFailure
                error=TestExecutionFailure if stages[-1]['responsibility']=='TEST' else SourceExecutionFailure
                raise error(f"{failure['category']}: bounded {stages[-1]['responsibility']} execution failed; verification_report_id={failure.get('verification_report_id')}",
                    failure_diagnostic=failure) from getattr(result,'causal_exception',None)
            return escalate(stages[-1].get('failure') or {'category':'DIRECT_FAILED'})
        changed=tuple(dict.fromkeys(p for s in stages for p in s['changed_paths']))
        from harness.workspace.diagnosis_domain import project_diagnosis_paths
        from harness.verification.acceptance import fingerprint
        domain = project_diagnosis_paths(session=session, contract=contract,
            observed_paths=(*sources, *tests), changed_paths=changed)
        content={'summary':'Source implementation and regression coverage verified by Phase7',
                 'changed_paths':list(changed),'model_id':model,'model_turns':result['model_calls'],
                 'tool_calls':aci_calls,'execution_mode':policy.mode.value}
        ControlPlaneArtifactBridge(store,owner.common.artifacts).record_text_evidence(
            run_id,artifact_id=request_id+':worker-result',todo_id=todo.todo_id,evidence_id=request_id+':worker-result',
            evidence_type='WORKER_RESULT',source='DirectExecutionAdapter',content=json.dumps(content),
            kind=ArtifactKind.WORKER_RESULT,origin=ArtifactOrigin.WORKER,scope_tags=changed,evidence_payload=content)
        observation=ExecutionObservationV1(profile_id,True,changed,result['model_calls'],content['tool_calls'],
            'SUCCESS',content['summary'],{'run_id':run_id,'lease_id':lease.lease_id,
                'typed_scenario_execution':True,'execution_mode':policy.mode.value,'direct_result':result,
                'diagnosis_path_domain':domain.to_dict(),
                'path_authority_provenance':{'run_id':run_id,'todo_id':todo.todo_id,
                    'lease_id':lease.lease_id,'contract_fingerprint':fingerprint(contract.to_dict()),
                    'source':'WorkspaceSessionFactory'}})
        store.record_evidence(run_id,Evidence(request_id+':execution',todo.todo_id,'DIRECT_EXECUTION',
            'DirectExecutionAdapter',EvidenceStatus.PASSED,{'attempt_index':attempt_index,
                'observation':{k:_plain(getattr(observation,k)) for k in ('profile_id','finalized','changed_paths',
                    'model_calls','tool_calls','outcome','summary','raw')}}))
        return observation

    @staticmethod
    def _event(store,run_id,todo,request_id,kind,payload):
        store.record_evidence(run_id,Evidence(f'{request_id}:{len(store.list_events(run_id))}',todo.todo_id,
            'DIRECT_EVENT','DirectExecutionAdapter',EvidenceStatus.PASSED,
            {'request_id':request_id,'kind':kind,**payload}))
