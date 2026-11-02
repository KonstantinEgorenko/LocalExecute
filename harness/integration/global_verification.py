'Fresh final-tree verification in existing stores, worktrees and verification.\n\nThe reserved attempt is one-shot. Only a fully committed receipt is reusable;\nan interrupted attempt never silently runs pytest again.\n'
from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import time
import uuid

from harness.context_manager import Artifact, ArtifactKind, ArtifactOrigin, FileArtifactStore
from harness.control_plane import (Run, RunStatus, QuotaUsage, Todo, TodoStatus, WorkContract,
    Quota, Evidence, EvidenceStatus, FileRunStore)
from harness.lead.required_graph import covers, RequiredDependencyGraphV1
from harness.verification.acceptance import canonical, fingerprint
from harness.verification.callable_authority import PublicCallableAuthorityV1
from harness.verification.global_contract import (GlobalVerificationContractV1, GLOBAL_GATES,
    GlobalVerificationError, require, sha, policy_document)
from harness.verification.models import (CommandCatalogEntry, VerificationGateSpec,
    VerificationGateKind, VerificationGateType, VerificationPlan, VerificationOutcome)
from harness.verification.publisher import VerificationPublisher
from harness.verification.runner import VerificationRunner
from .dependency_handoff import DependencyHandoff

SOURCE='GlobalIntegratedVerificationV1'


def admit_global_contract(spec, policy):
    # Cross-layer composition belongs here, not in the verification package.
    return GlobalVerificationContractV1.admit(spec,policy,graph=RequiredDependencyGraphV1.admit(spec))


@dataclass(frozen=True, slots=True)
class GlobalVerificationReceiptV1:
    document: str

    def __post_init__(self):
        from harness.agent_loop.protocol import decode_strict_json
        d=decode_strict_json(self.document)
        fields={'schema_version','mission_id','goal_id','global_contract_fingerprint','required_graph_fingerprint',
            'final_commit_sha','final_tree_sha','accepted_todo_receipt_fingerprints','dependency_versions',
            'verification_run_id','verification_policy_fingerprint','trusted_test_manifest_fingerprint',
            'verification_input','verification_input_fingerprint','verification_attempt_id','lease_id',
            'fresh_execution_evidence','phase7_gate_results','pytest_report_id','pytest_report_fingerprint',
            'global_verification_status','global_verification_fingerprint'}
        require(type(d) is dict and set(d)==fields,'GLOBAL_RECEIPT_INVALID','fields')
        require(type(d['schema_version']) is int and d['schema_version']==1,'GLOBAL_RECEIPT_INVALID','schema version')
        require(d['global_verification_status'] in ('PASSED','FAILED'),'GLOBAL_RECEIPT_INVALID','status')
        require(d['global_verification_fingerprint']==fingerprint({k:v for k,v in d.items() if k!='global_verification_fingerprint'})
            and canonical(d)==self.document,'GLOBAL_RECEIPT_INVALID','fingerprint/canonical JSON')

    def to_dict(self):return json.loads(self.document)


class GlobalIntegratedVerifier:
    def __init__(self, *, workspace, scheduler, policy):
        self.workspace,self.scheduler,self.policy=workspace,scheduler,policy
        self.artifacts,self.runs,self.manager=workspace.artifact_store,workspace.run_store,workspace.manager

    def key(self,spec,kind):return 'global-verification-'+kind+':'+fingerprint([spec.mission_id,spec.goal_id])

    def put(self,key,value):
        artifact=Artifact.from_text(artifact_id=key,kind=ArtifactKind.CONTROL_PLANE_EVIDENCE,
            origin=ArtifactOrigin.CONTROL_PLANE,content=canonical(value),media_type='application/json',
            source=SOURCE,created_at='1970-01-01T00:00:00+00:00')
        if self.artifacts.has_artifact(key):
            require(self.artifacts.get_artifact(key)==artifact,'GLOBAL_PUBLICATION_CONFLICT',key)
            return artifact.ref
        return self.artifacts.create_artifact(artifact).ref

    def load(self,key):
        from harness.agent_loop.protocol import decode_strict_json
        a=self.artifacts.get_artifact(key)
        require(a.source==SOURCE and a.origin is ArtifactOrigin.CONTROL_PLANE,'GLOBAL_PROVENANCE_INVALID',key)
        return decode_strict_json(a.content),a.ref

    def anchor(self,run_id,todo_id,kind,key,value):
        evidence=Evidence(run_id+':'+kind,todo_id,kind,SOURCE,EvidenceStatus.UNKNOWN,
            {'artifact_id':key,'fingerprint':fingerprint(value)})
        existing=[e.to_dict()['payload'] for e in self.runs.list_events(run_id)
            if e.to_dict()['payload'].get('evidence_id')==evidence.evidence_id]
        if existing:require(existing==[evidence.to_dict()],'GLOBAL_PROVENANCE_INVALID',kind)
        else:self.runs.record_evidence(run_id,evidence)

    def anchored(self,run_id,kind,key,value):
        rows=[e.to_dict()['payload'] for e in self.runs.list_events(run_id)
              if e.to_dict()['payload'].get('evidence_type')==kind]
        require(len(rows)==1 and rows[0]['source']==SOURCE and rows[0]['payload']==
            {'artifact_id':key,'fingerprint':fingerprint(value)},'GLOBAL_PROVENANCE_INVALID',kind)

    def failure(self,spec,exc):
        key=self.key(spec,'first-failure')
        if not self.artifacts.has_artifact(key):
            self.put(key,{'code':getattr(exc,'code','GLOBAL_VERIFICATION_INVALID'),'detail':str(exc)[:4096]})
        return key

    def resolve_input(self,spec,contract):
        h=DependencyHandoff(self.workspace,self.scheduler,spec);cfg=contract.to_dict()
        require(not self.manager.git.text(self.workspace.source_repo_root,'status','--porcelain'),
            'GLOBAL_ORIGINAL_WORKSPACE_DIRTY','owned target must remain unchanged')
        expected=[h.mapping[n.logical_id] for n in h.graph.todos]
        require(set(self.scheduler._items(spec.goal_id))==set(expected),'GLOBAL_TODO_SET_MISMATCH','exact admitted Todos')
        ordered=[];refs=[]
        for node in h.graph.todos:
            todo=h.todo(h.mapping[node.logical_id]);d,ref=h.resolve(todo)
            for dep in todo.dependencies:
                b,_,_=h.validate_consumer(todo,d['producer_run_id'])
                prior=h.resolve(h.todo(dep))[0]
                require(b['producer_receipt_fingerprint']==prior['receipt_fingerprint']
                    and d['parent_or_base_commit_sha']==prior['accepted_commit_sha'],
                    'GLOBAL_DEPENDENCY_MISMATCH','accepted consumer binding/base')
                require(self.manager.git.run(self.workspace.source_repo_root,'merge-base','--is-ancestor',
                    prior['accepted_commit_sha'],d['accepted_commit_sha'],check=False).returncode==0,
                    'GLOBAL_GIT_ANCESTRY_INVALID','accepted dependency')
            allowed=(*node.source_scope,*node.test_scope,*node.additional_scope)
            delta=self.manager.git.text(self.workspace.source_repo_root,'diff','--name-only',
                d['parent_or_base_commit_sha'],d['accepted_commit_sha']).splitlines()
            require(delta and all(any(covers(scope,p) for scope in allowed) for p in delta)
                and all(any(covers(scope,p) for p in delta) for scope in (*node.source_scope,*node.test_scope)),
                'GLOBAL_ACCEPTED_SCOPE_INVALID',node.logical_id)
            lease=self.manager.store.get(d['producer_lease_id'])
            if lease.status.value=='ACTIVE':
                snap=self.manager.snapshot(lease.lease_id)
                require(not snap.dirty and snap.head_commit==d['accepted_commit_sha'],
                    'GLOBAL_ACCEPTED_CANDIDATE_CHANGED',node.logical_id)
            ordered.append(d);refs.append(ref.to_dict())
        dependents={dep for n in h.graph.todos for dep in n.dependencies}
        sinks=[d for d in ordered if d['producer_logical_todo_id'] not in dependents]
        require(len(sinks)==1,'GLOBAL_JOIN_UNSUPPORTED','one proven final sink required')
        roots=[d for d in ordered if not h.todo(d['producer_physical_todo_id']).dependencies]
        require(len(roots)==1,'GLOBAL_JOIN_UNSUPPORTED','one proven original root required')
        final=sinks[0]
        for d in ordered:
            require(self.manager.git.run(self.workspace.source_repo_root,'merge-base','--is-ancestor',
                d['accepted_commit_sha'],final['accepted_commit_sha'],check=False).returncode==0,
                'GLOBAL_GIT_ANCESTRY_INVALID','final tree must contain every accepted Todo')
        snapshot,ref=self.workspace.spine.latest_project_snapshot(spec.goal_id)
        require(snapshot.head_commit==final['accepted_commit_sha'] and not snapshot.dirty_paths
            and ref.artifact_id==final['snapshot_artifact_id'] and ref.sha256==final['snapshot_fingerprint'],
            'GLOBAL_FINAL_SNAPSHOT_MISMATCH','latest snapshot is not the exact accepted final receipt')
        original=roots[0]['parent_or_base_commit_sha'];git=self.manager.git;root=self.workspace.source_repo_root
        authority=PublicCallableAuthorityV1.admit(spec,h.graph).to_dict()
        for path,row in {**authority['original_sources'],**cfg['baseline_tests']}.items():
            require(sha(git.run(root,'show',original+':'+path).stdout)==row['sha256'],
                'GLOBAL_TRUSTED_BASELINE_MISMATCH',path)
        for path in cfg['required_source_paths']+cfg['required_test_paths']:
            row=git.text(root,'ls-tree',final['accepted_commit_sha'],'--',path).split()
            require(len(row)==4 and row[0] in ('100644','100755') and row[1]=='blob',
                'GLOBAL_FINAL_PATH_INVALID',path)
        value=dict(mission_id=spec.mission_id,goal_id=spec.goal_id,
            required_graph_fingerprint=h.graph_fingerprint,global_contract_fingerprint=cfg['fingerprint'],
            ordered_accepted_todo_receipts=refs,producer_commit=roots[0]['accepted_commit_sha'],
            consumer_commit=final['accepted_commit_sha'],final_commit=final['accepted_commit_sha'],
            final_tree=final['accepted_tree_sha'],trusted_test_manifest_fingerprint=cfg['baseline_manifest_fingerprint'],
            verification_policy_fingerprint=cfg['verification_policy_fingerprint'])
        return value,ordered,authority

    def _refresh(self):
        # No work is active: all scheduler Todos must be accepted before this
        # phase. Refresh the shared store objects under the global OS mutex so
        # another completion process cannot consume a stale in-memory index.
        if isinstance(self.runs,FileRunStore):self.runs.__dict__.update(FileRunStore(self.runs.root).__dict__)
        if isinstance(self.artifacts,FileArtifactStore):self.artifacts._load()
        self.manager.store._load()

    def verify(self,spec):
        from harness.scheduler.store import _FileMutex
        try:
            with _FileMutex(self.scheduler.store.root/('global-'+fingerprint(spec.mission_id)+'.lock'),10):
                self._refresh()
                return self._verify(spec)
        except Exception as exc:
            self.failure(spec,exc)
            if isinstance(exc,GlobalVerificationError):raise
            raise GlobalVerificationError('GLOBAL_VERIFICATION_INVALID',str(exc)) from exc

    def validate(self,spec):
        try:return self._validate(spec)
        except Exception as exc:
            self.failure(spec,exc)
            if isinstance(exc,GlobalVerificationError):raise
            raise GlobalVerificationError('GLOBAL_RECEIPT_INVALID',str(exc)) from exc

    def _validate(self,spec):
        contract=admit_global_contract(spec,self.policy)
        require(contract is not None,'GLOBAL_CONTRACT_MISSING','opt-in')
        identity,ordered,_=self.resolve_input(spec,contract)
        key=self.key(spec,'receipt')
        require(self.artifacts.has_artifact(key),'GLOBAL_RECEIPT_MISSING','no fresh global result')
        d,ref=self.load(key);GlobalVerificationReceiptV1(canonical(d))
        attempt,_=self.load(self.key(spec,'attempt'))
        require(attempt['input']==identity and d['verification_input']==identity
            and d['verification_input_fingerprint']==fingerprint(identity),'GLOBAL_STALE_EVIDENCE','frozen final input')
        cfg=contract.to_dict()
        require(d['mission_id']==spec.mission_id and d['goal_id']==spec.goal_id
            and d['global_contract_fingerprint']==cfg['fingerprint']
            and d['required_graph_fingerprint']==identity['required_graph_fingerprint']
            and d['verification_attempt_id']==attempt['attempt_id']
            and d['verification_policy_fingerprint']==cfg['verification_policy_fingerprint']
            and d['trusted_test_manifest_fingerprint']==cfg['baseline_manifest_fingerprint'],
            'GLOBAL_RECEIPT_BINDING_INVALID','contract/attempt/policy')
        run=self.runs.get_run(d['verification_run_id'])
        require(run.run_id==attempt['run_id'] and run.status is RunStatus.COMPLETED,
            'GLOBAL_ATTEMPT_NOT_COMMITTED','verification Run')
        for kind,key2,value in (('GLOBAL_ATTEMPT_RESERVED',self.key(spec,'attempt'),attempt),
            ('GLOBAL_RECEIPT_PUBLISHED',key,d),('GLOBAL_VERIFICATION_COMMITTED',key,d)):
            self.anchored(run.run_id,kind,key2,value)
        publisher=VerificationPublisher(run_store=self.runs,artifact_store=self.artifacts)
        report=publisher.find_existing(report_id=d['pytest_report_id'])
        require(report is not None and fingerprint(report.to_dict())==d['pytest_report_fingerprint']
            and report.run_id==run.run_id and report.lease_id==d['lease_id']
            and report.candidate_head==identity['final_commit'] and not report.dirty_paths
            and report.outcome is VerificationOutcome.PASSED,'GLOBAL_PHASE7_REPORT_INVALID','exact global report')
        events=[e.to_dict()['payload'] for e in self.runs.list_events(run.run_id)
            if e.to_dict()['payload'].get('evidence_type')=='VERIFICATION_REPORT']
        require(len(events)==1 and events[0]['source']=='VerificationRunner'
            and events[0]['payload']['report_id']==report.report_id
            and events[0]['payload']['plan_fingerprint']==report.plan_fingerprint,
            'GLOBAL_PHASE7_REPORT_UNANCHORED','publisher evidence')
        require([r.to_dict() for r in report.results]==d['phase7_gate_results']
            and [r.gate_id for r in report.results]==list(GLOBAL_GATES),'GLOBAL_GATE_SET_INVALID','four genuine global gates')
        plan_key=attempt['artifact_key']+':plan';execution_plan,plan_ref=self.load(plan_key)
        self.anchored(run.run_id,'GLOBAL_EXECUTION_PLAN',plan_key,execution_plan)
        require(execution_plan['plan']['fingerprint']==report.plan_fingerprint
            and execution_plan['input_fingerprint']==fingerprint(identity)
            and execution_plan['plan']['run_id']==run.run_id and execution_plan['plan']['todo_id']==run.active_todo_id==attempt['todo_id']
            and execution_plan['plan']['lease_id']==d['lease_id'] and run.goal_id==spec.goal_id
            and execution_plan['policy']['global_verifier_source']==policy_document(self.policy)['global_verifier_source']
            and plan_ref.to_dict() in d['fresh_execution_evidence'],
            'GLOBAL_EXECUTION_PLAN_INVALID','durably reserved plan')
        for result in report.results:self._validate_process(attempt,result,d)
        lease=self.manager.store.get(d['lease_id'])
        require(lease is not None and lease.run_id==run.run_id and lease.base_commit==identity['final_commit'],
            'GLOBAL_CHECKOUT_INVALID','global lease/base')
        if lease.status.value=='ACTIVE':
            snapshot=self.manager.snapshot(lease.lease_id)
            require(not snapshot.dirty and snapshot.head_commit==identity['final_commit'],'GLOBAL_CHECKOUT_MUTATED','post-verification')
        require(d['accepted_todo_receipt_fingerprints']==[r['receipt_fingerprint'] for r in ordered]
            and d['dependency_versions']==[r['dependency_version'] for r in ordered]
            and d['final_commit_sha']==identity['final_commit'] and d['final_tree_sha']==identity['final_tree']
            and d['global_verification_status']=='PASSED','GLOBAL_RECEIPT_BINDING_INVALID','accepted lineage')
        return GlobalVerificationReceiptV1(canonical(d))

    def _validate_process(self,attempt,result,receipt):
        key=attempt['artifact_key']+':'+result.gate_id
        started,_=self.load(key+':STARTED');finished,ref=self.load(key+':FINISHED')
        plan,_=self.load(attempt['artifact_key']+':plan')
        command=plan['policy']['command_catalog'][result.gate_id]
        lease=self.manager.store.get(plan['plan']['lease_id'])
        require(started['argv'][0]==plan['policy']['trusted_python_executable']
            and started['argv'][1:]==command['argv'][1:]
            and os.path.normcase(os.path.abspath(started['cwd']))==os.path.normcase(os.path.abspath(lease.worktree_root))
            and 0<started['timeout_seconds']<=command['timeout_seconds'],
            'GLOBAL_COMMAND_POLICY_MISMATCH',result.gate_id)
        for phase,row in (('STARTED',started),('FINISHED',finished)):
            self.anchored(attempt['run_id'],'GLOBAL_PROCESS_'+result.gate_id+'_'+phase,key+':'+phase,row)
        require(all(started[k]==finished[k] for k in ('pid','parent_pid','argv','cwd','timeout_seconds','gate_id'))
            and started['attempt_id']==finished['attempt_id']==attempt['attempt_id'] and finished['exit_code']==0
            and finished['exit_code']==result.exit_code and not finished['timed_out']
            and finished['stdout_sha256']==sha(result.stdout) and finished['stderr_sha256']==sha(result.stderr)
            and not result.output_truncated and not finished['output_truncated'],
            'GLOBAL_PROCESS_RESULT_TAMPERED',result.gate_id)
        envelope=json.loads(result.stdout)
        require(envelope['attempt_id']==attempt['attempt_id'] and envelope['input_fingerprint']==fingerprint(attempt['input'])
            and envelope['pid']==started['pid'] and envelope['parent_pid']==started['parent_pid']
            and envelope['cwd']==started['cwd'] and envelope['gate']==result.gate_id and envelope['status']=='PASS'
            and ref.to_dict() in receipt['fresh_execution_evidence'],'GLOBAL_PROCESS_IDENTITY_INVALID',result.gate_id)

    def _verify(self,spec):
        contract=admit_global_contract(spec,self.policy)
        require(contract is not None,'GLOBAL_CONTRACT_MISSING','opt-in')
        identity,ordered,authority=self.resolve_input(spec,contract)
        if self.artifacts.has_artifact(self.key(spec,'receipt')):return self.validate(spec)
        key=self.key(spec,'attempt')
        require(not self.artifacts.has_artifact(key),'GLOBAL_ATTEMPT_INTERRUPTED','one-shot budget consumed; no automatic rerun')
        cfg=contract.to_dict();nonce=uuid.uuid4().hex
        attempt=dict(schema_version=1,attempt_id=nonce,run_id=spec.goal_id+'-global-'+nonce[:16],
            todo_id='global-verification:'+fingerprint([spec.mission_id,spec.goal_id])[:24],input=identity,artifact_key=key)
        self.put(key,attempt)
        self.runs.create_todo(Todo(attempt['todo_id'],spec.goal_id,'Fresh global integrated deterministic verification',TodoStatus.RUNNING,
            required_gates=GLOBAL_GATES))
        self.runs.create_run(Run(attempt['run_id'],spec.goal_id,attempt['todo_id'],RunStatus.RUNNING,QuotaUsage()))
        self.anchor(attempt['run_id'],attempt['todo_id'],'GLOBAL_ATTEMPT_RESERVED',key,attempt)
        lease=None
        try:
            lease=self.manager.acquire(source_repo_root=self.workspace.source_repo_root,run_id=attempt['run_id'],
                todo_id=attempt['todo_id'],base_ref=identity['final_commit'])
            require(lease.base_commit==identity['final_commit'] and self.manager.git.text(lease.worktree_root,'rev-parse','HEAD')==identity['final_commit'],
                'GLOBAL_CHECKOUT_INVALID','exact final SHA')
            self._execute(spec,contract,identity,ordered,authority,attempt,lease)
        except BaseException as exc:
            # Retain the execution cause before any independent cleanup error.
            observed=exc if isinstance(exc,Exception) else GlobalVerificationError(
                'GLOBAL_ATTEMPT_INTERRUPTED',type(exc).__name__+': '+str(exc))
            self.failure(spec,observed)
            raise
        finally:
            if lease is not None:self.manager.release(lease.lease_id,force=True,delete_branch=True)
        return self.validate(spec)

    def _execute(self,spec,contract,identity,ordered,authority,attempt,lease):
        import pytest
        cfg=contract.to_dict();catalog=dict(self.policy.command_catalog)
        pytest_root=str(Path(pytest.__file__).resolve().parent.parent)
        driver=str(Path(__file__).resolve().parents[1]/'verification/global_checks.py')
        for gate in GLOBAL_GATES:
            packet=dict(gate=gate,attempt_id=attempt['attempt_id'],input_fingerprint=fingerprint(identity),
                contract=cfg,authority=authority,trusted_pytest_root=pytest_root)
            catalog[gate]=CommandCatalogEntry(gate,VerificationGateType.TESTS,
                ('{python}',driver,canonical(packet)),cfg['max_gate_seconds'])
        policy=replace(self.policy,require_candidate_dirty=False,fail_fast=True,command_catalog={g:catalog[g] for g in GLOBAL_GATES},
            max_gate_timeout_seconds=cfg['max_gate_seconds'],max_total_timeout_seconds=cfg['max_total_seconds'])
        def observe(phase,row):
            row={**row,'attempt_id':attempt['attempt_id'],'observed_monotonic_ns':time.monotonic_ns()}
            key=attempt['artifact_key']+':'+row['gate_id']+':'+phase
            self.put(key,row);self.anchor(attempt['run_id'],attempt['todo_id'],
                'GLOBAL_PROCESS_'+row['gate_id']+'_'+phase,key,row)
        runner=VerificationRunner(manager=self.manager,policy=policy,command_observer=observe)
        plan=VerificationPlan(attempt['run_id']+':GLOBAL_INTEGRATED_PHASE7',attempt['run_id'],attempt['todo_id'],lease.lease_id,
            tuple(VerificationGateSpec(g,VerificationGateKind.COMMAND,VerificationGateType.TESTS,True,catalog_id=g) for g in GLOBAL_GATES))
        work=WorkContract(spec.goal_id,attempt['todo_id'],'Fresh global final-tree verification',{},
            tuple(cfg['required_source_paths']+cfg['required_test_paths']),('.git',),(),Quota(1,0,0,0,0))
        plan_key=attempt['artifact_key']+':plan'
        plan_value={'plan':plan.to_dict(),'policy':policy_document(policy),'input_fingerprint':fingerprint(identity)}
        plan_ref=self.put(plan_key,plan_value)
        self.anchor(attempt['run_id'],attempt['todo_id'],'GLOBAL_EXECUTION_PLAN',plan_key,plan_value)
        report=runner.run(plan=plan,contract=work)
        VerificationPublisher(run_store=self.runs,artifact_store=self.artifacts).publish(report)
        require(self.resolve_input(spec,contract)[0]==identity,'GLOBAL_INPUT_CHANGED_DURING_EXECUTION','accepted final input')
        refs=[plan_ref.to_dict()]
        for g in GLOBAL_GATES:
            key=attempt['artifact_key']+':'+g+':FINISHED'
            if self.artifacts.has_artifact(key):refs.append(self.artifacts.get_artifact(key).ref.to_dict())
        d=dict(schema_version=1,mission_id=spec.mission_id,goal_id=spec.goal_id,
            global_contract_fingerprint=cfg['fingerprint'],required_graph_fingerprint=identity['required_graph_fingerprint'],
            final_commit_sha=identity['final_commit'],final_tree_sha=identity['final_tree'],
            accepted_todo_receipt_fingerprints=[r['receipt_fingerprint'] for r in ordered],
            dependency_versions=[r['dependency_version'] for r in ordered],verification_run_id=attempt['run_id'],
            verification_policy_fingerprint=cfg['verification_policy_fingerprint'],
            trusted_test_manifest_fingerprint=cfg['baseline_manifest_fingerprint'],verification_input=identity,
            verification_input_fingerprint=fingerprint(identity),verification_attempt_id=attempt['attempt_id'],lease_id=lease.lease_id,
            fresh_execution_evidence=refs,phase7_gate_results=[r.to_dict() for r in report.results],
            pytest_report_id=report.report_id,pytest_report_fingerprint=fingerprint(report.to_dict()),
            global_verification_status=report.outcome.value)
        d['global_verification_fingerprint']=fingerprint(d)
        GlobalVerificationReceiptV1(canonical(d))
        if report.outcome is VerificationOutcome.PASSED:
            for result in report.results:self._validate_process(attempt,result,d)
        key=self.key(spec,'receipt');self.put(key,d)
        self.anchor(attempt['run_id'],attempt['todo_id'],'GLOBAL_RECEIPT_PUBLISHED',key,d)
        if report.outcome is not VerificationOutcome.PASSED:
            self.runs.update_run(replace(self.runs.get_run(attempt['run_id']),status=RunStatus.FAILED))
            failed=next(r for r in report.results if r.required and r.outcome.value!='PASSED')
            try:detail=json.loads(failed.stdout).get('error','')
            except (ValueError,AttributeError):detail=failed.stderr[-1024:]
            raise GlobalVerificationError('GLOBAL_PHASE7_FAILED',canonical(dict(gate_id=failed.gate_id,
                outcome=failed.outcome.value,exit_code=failed.exit_code,detail=failed.detail,error=detail[:2048])))
        self.runs.update_run(replace(self.runs.get_run(attempt['run_id']),status=RunStatus.COMPLETED))
        self.anchor(attempt['run_id'],attempt['todo_id'],'GLOBAL_VERIFICATION_COMMITTED',key,d)
