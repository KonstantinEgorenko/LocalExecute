"""Exact dependency lineage in the existing ArtifactStore/RunStore ledgers.

Opt-in required graphs use prepare -> published -> Run-completed -> committed
evidence. A receipt is consumable only after scheduler success as well. These
are deliberately separate durable writes, never a claim of cross-store atomicity.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import wraps
import hashlib
import json
from pathlib import Path
import re

from harness.context_manager import Artifact, ArtifactKind, ArtifactOrigin
from harness.control_plane import RunStatus
from harness.lead.required_graph import RequiredDependencyGraphV1
from harness.scheduler import SchedulerTodoStatus
from harness.verification.acceptance import fingerprint, canonical
from .execution_responsibility import evidence_payloads, persisted_responsibility, record_once
from .planning_evidence import evidence_key


class DependencyHandoffError(RuntimeError):
    def __init__(self, code, detail):
        self.code = code
        super().__init__(f'{code}: {detail}')


def require(value, code, detail):
    if not value:
        raise DependencyHandoffError(code, detail)


@dataclass(frozen=True, slots=True)
class DependencyAcceptanceReceiptV1:
    """Immutable canonical document; every load also requires store provenance."""
    document: str

    def __post_init__(self):
        from harness.agent_loop.protocol import decode_strict_json
        value = decode_strict_json(self.document)
        keys = {'schema_version', 'mission_id', 'goal_id', 'required_graph_fingerprint',
            'producer_logical_todo_id', 'producer_physical_todo_id', 'producer_run_id',
            'producer_attempt_id', 'producer_lease_id', 'accepted_commit_sha', 'accepted_tree_sha',
            'parent_or_base_commit_sha', 'snapshot_artifact_id', 'snapshot_fingerprint',
            'verification_report_id', 'verification_fingerprint', 'judge_report_id', 'critic_report_id',
            'accepted_outcome_reference', 'accepted_outcome_fingerprint', 'acceptance_fingerprint', 'dependency_version',
            'publisher_state', 'prepared_reference', 'retention_ref', 'source_blobs', 'receipt_fingerprint'}
        require(type(value) is dict and set(value) == keys, 'DEPENDENCY_RECEIPT_INVALID', 'fields')
        require(type(value['schema_version']) is int and value['schema_version'] == 1,
                'DEPENDENCY_RECEIPT_INVALID', 'schema_version')
        require(all(type(value[k]) is str and value[k] for k in keys-{'schema_version','source_blobs'}),
                'DEPENDENCY_RECEIPT_INVALID', 'string fields')
        require(value['publisher_state'] == 'PUBLISHED', 'DEPENDENCY_RECEIPT_INVALID', 'publisher state')
        for key in ('accepted_commit_sha','accepted_tree_sha','parent_or_base_commit_sha'):
            require(re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}',value[key]), 'DEPENDENCY_RECEIPT_INVALID',key)
        require(type(value['source_blobs']) is dict and bool(value['source_blobs']),
                'DEPENDENCY_RECEIPT_INVALID','source blobs')
        require(all(type(path) is str and type(oid) is str and re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}',oid)
                    for path,oid in value['source_blobs'].items()),'DEPENDENCY_RECEIPT_INVALID','source object identities')
        body = {k:v for k,v in value.items() if k != 'receipt_fingerprint'}
        require(value['receipt_fingerprint'] == fingerprint(body), 'DEPENDENCY_RECEIPT_TAMPERED','fingerprint')
        require(canonical(value) == self.document, 'DEPENDENCY_RECEIPT_INVALID','canonical JSON')

    def to_dict(self):
        return json.loads(self.document)


def diagnostic(method):
    @wraps(method)
    def guarded(self, todo, *args, **kwargs):
        try:
            return method(self, todo, *args, **kwargs)
        except Exception as exc:
            error = exc if isinstance(exc,DependencyHandoffError) else DependencyHandoffError(
                'DEPENDENCY_HANDOFF_INVALID',f'{type(exc).__name__}: {exc}')
            key = 'dependency-first-failure:'+fingerprint([self.spec.mission_id,todo.todo_id])
            if not self.artifacts.has_artifact(key):
                self.put(key,{'code':error.code,'detail':str(error),'todo_id':todo.todo_id})
            if error is exc:
                raise
            raise error from exc
    return guarded


class DependencyHandoff:
    def __init__(self, workspace, scheduler, spec):
        self.workspace, self.scheduler, self.spec = workspace, scheduler, spec
        self.artifacts = workspace.artifact_store
        self.runs, self.manager = workspace.run_store, workspace.manager
        require(self.artifacts is not None,'DEPENDENCY_STORE_REQUIRED','ArtifactStore')
        self.graph = RequiredDependencyGraphV1.admit(spec)
        require(self.graph is not None,'DEPENDENCY_GRAPH_REQUIRED','caller admitted graph')
        proof = json.loads(self.artifacts.get_artifact(evidence_key(spec)+'-qualification').content)
        require(proof['contract'] == self.graph.to_dict(),'DEPENDENCY_GRAPH_MISMATCH','planning qualification')
        self.mapping = dict(proof['mapping'])
        self.graph_fingerprint = self.graph.to_dict()['fingerprint']

    def todo(self, todo_id):
        return self.scheduler._items(self.spec.goal_id)[todo_id]

    def node(self, todo):
        logical = next((k for k,v in self.mapping.items() if v == todo.todo_id),None)
        node = next((n for n in self.graph.todos if n.logical_id == logical),None)
        require(node is not None,'DEPENDENCY_TODO_MISMATCH',todo.todo_id)
        expected = tuple(self.mapping[k] for k in node.dependencies)
        require(set(todo.dependencies) == set(expected),'DEPENDENCY_GRAPH_MISMATCH','prerequisites')
        return node

    def version(self, todo):
        self.node(todo)
        return fingerprint(self.workspace._responsibility(todo.work_contract).to_dict())

    def key(self, todo, kind):
        return f'dependency-{kind}:'+fingerprint([self.spec.mission_id,todo.todo_id,self.version(todo)])

    def put(self, key, value, *, scope_tags=(), metadata=None):
        artifact = Artifact.from_text(artifact_id=key,kind=ArtifactKind.CONTROL_PLANE_EVIDENCE,
            origin=ArtifactOrigin.CONTROL_PLANE,content=canonical(value),media_type='application/json',
            source='DependencyHandoffV1',created_at='1970-01-01T00:00:00+00:00',
            scope_tags=scope_tags,metadata={} if metadata is None else metadata)
        if self.artifacts.has_artifact(key):
            old=self.artifacts.get_artifact(key)
            require(old == artifact,'DEPENDENCY_PUBLICATION_CONFLICT',key)
            return old.ref
        return self.artifacts.create_artifact(artifact).ref

    def load(self, key):
        artifact=self.artifacts.get_artifact(key)
        require(artifact.source == 'DependencyHandoffV1' and artifact.origin is ArtifactOrigin.CONTROL_PLANE,
                'DEPENDENCY_PROVENANCE_INVALID',key)
        return json.loads(artifact.content),artifact.ref

    def anchored(self, run_id, kind, key, payload):
        values=evidence_payloads(self.runs,run_id,kind)
        require(values == [{'artifact_id':key,'fingerprint':fingerprint(payload)}],
                'DEPENDENCY_PROVENANCE_INVALID',kind)

    def anchor(self, run_id, todo_id, kind, key, payload):
        record_once(self.runs,run_id,todo_id,kind,{'artifact_id':key,'fingerprint':fingerprint(payload)})

    def report(self, run_id, todo_id, kind, report_id, source):
        matches=[e.to_dict()['payload'] for e in self.runs.list_events(run_id)
                 if e.to_dict()['payload'].get('evidence_type') == kind
                 and e.to_dict()['payload'].get('payload',{}).get('report_id') == report_id]
        require(len(matches)==1,'DEPENDENCY_REPORT_UNANCHORED',kind)
        event=matches[0]
        require(event['source']==source and event['status']=='PASSED' and event['todo_id']==todo_id,
                'DEPENDENCY_REPORT_REJECTED',kind)
        from harness.context_manager.integration import parse_artifact_uri
        ref=parse_artifact_uri(event['reference'])
        require(ref is not None,'DEPENDENCY_REPORT_UNANCHORED',kind)
        artifact=self.artifacts.get_artifact(ref)
        require(artifact.source==source and artifact.origin is ArtifactOrigin.CONTROL_PLANE,
                'DEPENDENCY_REPORT_UNANCHORED','artifact source')
        body=json.loads(artifact.content)
        require((body['report_id'],body['run_id'],body['todo_id'])==(report_id,run_id,todo_id),
                'DEPENDENCY_REPORT_MISMATCH',kind)
        # Publisher events are an independent authority, not just a pointer to
        # a self-consistent artifact. Recheck their material report identities.
        for key in ('candidate_diff_sha256','verification_report_id','critic_report_id','plan_fingerprint'):
            if key in event['payload']:
                require(body.get(key)==event['payload'][key],'DEPENDENCY_REPORT_MISMATCH',key)
        for key in ('decision','verdict','deterministic_outcome'):
            if key in event['payload']:
                require(body['analysis'][key]==event['payload'][key],'DEPENDENCY_REPORT_MISMATCH',key)
        return body

    def accepted_reports(self, todo, run_id, lease, verification_id, judge_id, critic_id):
        from harness.verification.models import VerificationReport, VerificationOutcome
        v=VerificationReport.from_dict(self.report(run_id,todo.todo_id,'VERIFICATION_REPORT',verification_id,'VerificationRunner'))
        require(v.outcome is VerificationOutcome.PASSED and v.lease_id==lease.lease_id,
                'DEPENDENCY_VERIFICATION_FAILED','required Phase7 / lease')
        j=self.report(run_id,todo.todo_id,'JUDGE_REPORT',judge_id,'OrnithJudge')
        c=self.report(run_id,todo.todo_id,'CRITIC_REPORT',critic_id,'MinistralCritic')
        require(j['analysis']['decision']=='ACCEPT' and j['analysis']['deterministic_outcome']=='PASSED',
                'DEPENDENCY_REVIEW_REJECTED','Judge')
        require(j['critic_report_id']==critic_id and j['verification_report_id']==verification_id
                and c['verification_report_id']==verification_id
                and j['candidate_diff_sha256']==c['candidate_diff_sha256']==v.candidate_diff_sha256,
                'DEPENDENCY_REVIEW_MISMATCH','review pair/candidate')
        return v,j,c

    @diagnostic
    def prepare(self,todo,verification,review):
        run_id,lease=self.workspace.current(todo.todo_id)
        run=self.runs.get_run(run_id)
        require(run.status in (RunStatus.RUNNING,RunStatus.COMPLETED),'DEPENDENCY_RUN_NOT_ACCEPTABLE',run.status.value)
        require(review.accept and verification.passed,'DEPENDENCY_REVIEW_REJECTED','workflow observation')
        require(persisted_responsibility(self.runs,run_id)==self.workspace._responsibility(todo.work_contract),
                'DEPENDENCY_RESPONSIBILITY_MISMATCH','producer')
        v,j,c=self.accepted_reports(todo,run_id,lease,verification.report_id,
            review.raw['judge']['report_id'],review.raw['critic']['report_id'])
        snapshot=self.manager.snapshot(lease.lease_id)
        require((snapshot.head_commit,snapshot.diff_sha256)==(v.candidate_head,v.candidate_diff_sha256)
                and verification.fingerprint==v.plan_fingerprint,
                'DEPENDENCY_VERIFIED_STATE_CHANGED','candidate no longer matches report')
        git=self.manager.git;root=Path(lease.worktree_root)
        git.run(root,'add','--all')
        tree=git.text(root,'write-tree')
        require(self.manager.snapshot(lease.lease_id)==snapshot,'DEPENDENCY_VERIFIED_STATE_CHANGED','staging race')
        node=self.node(todo)
        prepared=dict(schema_version=1,mission_id=self.spec.mission_id,goal_id=self.spec.goal_id,
            required_graph_fingerprint=self.graph_fingerprint,producer_logical_todo_id=node.logical_id,
            producer_physical_todo_id=todo.todo_id,producer_run_id=run_id,producer_attempt_id=run_id,
            producer_lease_id=lease.lease_id,parent_or_base_commit_sha=v.candidate_head,
            accepted_tree_sha=tree,verification_report_id=v.report_id,verification_fingerprint=fingerprint(v.to_dict()),
            judge_report_id=j['report_id'],critic_report_id=c['report_id'],
            acceptance_fingerprint=fingerprint([j,c]),dependency_version=self.version(todo),
            responsibility=self.workspace._responsibility(todo.work_contract).to_dict())
        key=self.key(todo,'prepared');self.put(key,prepared)
        self.anchor(run_id,todo.todo_id,'DEPENDENCY_ACCEPTANCE_PREPARED',key,prepared)
        return run_id

    def git_identity(self,receipt):
        d=receipt;git=self.manager.git;root=self.workspace.source_repo_root
        for field,kind in (('accepted_commit_sha','commit'),('accepted_tree_sha','tree')):
            oid=d[field]
            require(git.text(root,'cat-file','-t',oid)==kind,'DEPENDENCY_GIT_OBJECT_INVALID',field)
        require(git.text(root,'rev-parse','--verify',d['accepted_commit_sha']+'^{commit}')==d['accepted_commit_sha']
                and git.text(root,'rev-parse',d['accepted_commit_sha']+'^{tree}')==d['accepted_tree_sha'],
                'DEPENDENCY_GIT_TREE_MISMATCH','commit/tree')
        require(git.text(root,'rev-parse','--verify',d['retention_ref'])==d['accepted_commit_sha'],
                'DEPENDENCY_COMMIT_UNREACHABLE','retention ref')
        parents=git.text(root,'show','-s','--format=%P',d['accepted_commit_sha']).split()
        require(parents==[d['parent_or_base_commit_sha']],'DEPENDENCY_GIT_PARENT_MISMATCH','single verified base')

    @diagnostic
    def publish(self,todo,snapshot,outcome_ref):
        key=self.key(todo,'prepared');p,_=self.load(key)
        run_id=p['producer_run_id'];self.anchored(run_id,'DEPENDENCY_ACCEPTANCE_PREPARED',key,p)
        lease=self.manager.get_active(p['producer_lease_id'])
        require((lease.run_id,lease.todo_id,lease.base_commit)==(run_id,todo.todo_id,p['parent_or_base_commit_sha']),
                'DEPENDENCY_LEASE_MISMATCH','producer')
        v,j,c=self.accepted_reports(todo,run_id,lease,p['verification_report_id'],p['judge_report_id'],p['critic_report_id'])
        require(fingerprint(v.to_dict())==p['verification_fingerprint'] and fingerprint([j,c])==p['acceptance_fingerprint'],
                'DEPENDENCY_ACCEPTANCE_MISMATCH','reports changed')
        require(p['responsibility']==self.workspace._responsibility(todo.work_contract).to_dict(),
                'DEPENDENCY_RESPONSIBILITY_SUPERSEDED','publisher')
        live=self.manager.snapshot(lease.lease_id)
        require(not live.dirty and live.head_commit==snapshot.head_commit,'DEPENDENCY_SNAPSHOT_MISMATCH','candidate')
        git=self.manager.git;root=Path(lease.worktree_root);commit=live.head_commit
        require(git.text(root,'rev-parse',commit+'^{tree}')==p['accepted_tree_sha'],
                'DEPENDENCY_GIT_TREE_MISMATCH','verified tree')
        pin='refs/swarm/accepted-dependencies/'+fingerprint([self.spec.mission_id,todo.todo_id,p['dependency_version']])
        old=git.run(root,'rev-parse','--verify',pin,check=False)
        require(old.returncode!=0 or old.stdout.strip()==commit,'DEPENDENCY_PUBLICATION_CONFLICT','retention ref')
        if old.returncode!=0:git.run(root,'update-ref',pin,commit,'0'*len(commit))
        snapshot_ref=self.workspace.spine.publish_project_snapshot(snapshot)
        outcome=self.artifacts.get_artifact(outcome_ref.artifact_id)
        value=json.loads(outcome.content)
        require((value['run_id'],value['todo_id'],value['verification_report_id'])==(run_id,todo.todo_id,v.report_id),
                'DEPENDENCY_OUTCOME_MISMATCH','actual accepted attempt')
        blobs={}
        for scope in self.node(todo).source_scope:
            for row in git.text(root,'ls-tree','-r',commit,'--',scope).splitlines():
                info,path=row.split('\t',1);mode,kind,oid=info.split()
                require(kind=='blob' and mode in ('100644','100755'),'DEPENDENCY_SOURCE_UNSUPPORTED',path)
                blobs[path]=oid
        require(bool(blobs),'DEPENDENCY_SOURCE_MISSING','producer sources')
        d={k:v for k,v in p.items() if k!='responsibility'}
        d.update(accepted_commit_sha=commit,snapshot_artifact_id=snapshot_ref.artifact_id,
            snapshot_fingerprint=snapshot_ref.sha256,accepted_outcome_reference=outcome_ref.artifact_id,
            accepted_outcome_fingerprint=outcome.sha256,
            prepared_reference=key,publisher_state='PUBLISHED',retention_ref=pin,source_blobs=blobs)
        d['receipt_fingerprint']=fingerprint(d)
        receipt=DependencyAcceptanceReceiptV1(canonical(d));self.git_identity(d)
        receipt_key=self.key(todo,'acceptance');self.put(receipt_key,receipt.to_dict())
        self.anchor(run_id,todo.todo_id,'DEPENDENCY_ACCEPTANCE_PUBLISHED',receipt_key,d)
        self.commit(todo)
        return receipt

    @diagnostic
    def commit(self,todo):
        d,ref=self.load(self.key(todo,'acceptance'));DependencyAcceptanceReceiptV1(canonical(d))
        self.anchored(d['producer_run_id'],'DEPENDENCY_ACCEPTANCE_PUBLISHED',ref.artifact_id,d)
        self.validate_receipt(todo,d,require_completed=False)
        run=self.runs.get_run(d['producer_run_id'])
        require(run.status in (RunStatus.RUNNING,RunStatus.COMPLETED),'DEPENDENCY_RUN_NOT_ACCEPTABLE',run.status.value)
        if run.status is RunStatus.RUNNING:
            from dataclasses import replace
            self.runs.update_run(replace(run,status=RunStatus.COMPLETED))
        self.anchor(run.run_id,todo.todo_id,'DEPENDENCY_ACCEPTANCE_COMMITTED',ref.artifact_id,d)
        return d

    def validate_receipt(self,todo,d,*,require_completed=True):
        DependencyAcceptanceReceiptV1(canonical(d))
        node=self.node(todo);run=self.runs.get_run(d['producer_run_id']);lease=self.manager.store.get(d['producer_lease_id'])
        require((d['mission_id'],d['goal_id'],d['required_graph_fingerprint'],d['producer_logical_todo_id'],d['producer_physical_todo_id'])==
                (self.spec.mission_id,self.spec.goal_id,self.graph_fingerprint,node.logical_id,todo.todo_id),
                'DEPENDENCY_RECEIPT_IDENTITY_MISMATCH','mission/goal/graph/Todo')
        require(d['dependency_version']==self.version(todo),'DEPENDENCY_VERSION_SUPERSEDED','scheduler responsibility')
        require(run.goal_id==self.spec.goal_id and run.active_todo_id==todo.todo_id
                and d['producer_attempt_id']==run.run_id and lease is not None
                and (lease.run_id,lease.todo_id,lease.base_commit)==(run.run_id,todo.todo_id,d['parent_or_base_commit_sha']),
                'DEPENDENCY_ATTEMPT_LEASE_MISMATCH','authoritative Run/lease')
        require(not require_completed or run.status is RunStatus.COMPLETED,'DEPENDENCY_RUN_NOT_COMPLETED',run.run_id)
        p,_=self.load(d['prepared_reference']);self.anchored(run.run_id,'DEPENDENCY_ACCEPTANCE_PREPARED',d['prepared_reference'],p)
        require(all(d.get(k)==v for k,v in p.items() if k!='responsibility'),
                'DEPENDENCY_PREPARATION_MISMATCH','receipt must preserve exact prepared fields')
        require(p['responsibility']==self.workspace._responsibility(todo.work_contract).to_dict(),
                'DEPENDENCY_RESPONSIBILITY_SUPERSEDED','current responsibility')
        require(persisted_responsibility(self.runs,run.run_id)==self.workspace._responsibility(todo.work_contract),
                'DEPENDENCY_RESPONSIBILITY_MISMATCH','Run ledger')
        v,j,c=self.accepted_reports(todo,run.run_id,lease,d['verification_report_id'],d['judge_report_id'],d['critic_report_id'])
        require(fingerprint(v.to_dict())==d['verification_fingerprint'] and fingerprint([j,c])==d['acceptance_fingerprint'],
                'DEPENDENCY_ACCEPTANCE_MISMATCH','authoritative reports')
        outcome=self.artifacts.get_artifact(d['accepted_outcome_reference']);o=json.loads(outcome.content)
        snapshot=self.artifacts.get_artifact(d['snapshot_artifact_id']);s=json.loads(snapshot.content)
        require(outcome.source=='Phase16VerifiedTodoOutcome' and snapshot.source=='Phase16ProjectSnapshot'
                and o['run_id']==run.run_id and o['todo_id']==todo.todo_id and o['goal_id']==self.spec.goal_id
                and o['verification_report_id']==v.report_id and o['verification_fingerprint']==v.plan_fingerprint
                and s['goal_id']==self.spec.goal_id and s['head_commit']==d['accepted_commit_sha']
                and snapshot.sha256==d['snapshot_fingerprint'] and o['epoch_after']==s['epoch']
                and outcome.sha256==d['accepted_outcome_fingerprint'],
                'DEPENDENCY_OUTCOME_MISMATCH','exact snapshot/outcome')
        self.git_identity(d)
        for path,blob in d['source_blobs'].items():
            require(self.manager.git.text(self.workspace.source_repo_root,'rev-parse',d['accepted_commit_sha']+':'+path)==blob,
                    'DEPENDENCY_SOURCE_CHANGED','accepted Git blob')

    @diagnostic
    def resolve(self,todo):
        key=self.key(todo,'acceptance')
        require(self.artifacts.has_artifact(key),'DEPENDENCY_ACCEPTANCE_MISSING',todo.todo_id)
        d,ref=self.load(key);self.validate_receipt(todo,d)
        self.anchored(d['producer_run_id'],'DEPENDENCY_ACCEPTANCE_PUBLISHED',key,d)
        self.anchored(d['producer_run_id'],'DEPENDENCY_ACCEPTANCE_COMMITTED',key,d)
        require(self.scheduler.store.get_todo(todo.todo_id).status is SchedulerTodoStatus.SUCCEEDED,
                'DEPENDENCY_SCHEDULER_NOT_COMMITTED',todo.todo_id)
        return d,ref

    @diagnostic
    def prerequisites(self,todo):
        self.node(todo)
        require(len(todo.dependencies)<=1,'DEPENDENCY_MULTI_PARENT_UNSUPPORTED','no authorized Git join contract')
        return tuple(self.resolve(self.todo(dep)) for dep in todo.dependencies)

    @diagnostic
    def bind(self,todo,run_id):
        dependencies=self.prerequisites(todo)
        if not dependencies:return None
        d,ref=dependencies[0]
        lease_id=self.manager.planned_lease_id(source_repo_root=self.workspace.source_repo_root,run_id=run_id,todo_id=todo.todo_id)
        payload=dict(schema_version=1,mission_id=self.spec.mission_id,goal_id=self.spec.goal_id,
            consumer_todo_id=todo.todo_id,consumer_run_id=run_id,consumer_lease_id=lease_id,
            consumer_responsibility=self.workspace._responsibility(todo.work_contract).to_dict(),
            required_graph_fingerprint=self.graph_fingerprint,producer_receipt_id=ref.artifact_id,
            producer_receipt_sha256=ref.sha256,producer_receipt_fingerprint=d['receipt_fingerprint'],
            dependency_version=d['dependency_version'],consumer_base_commit=d['accepted_commit_sha'])
        key='dependency-consumer:'+fingerprint([self.spec.mission_id,todo.todo_id,run_id])
        self.put(key,payload);self.anchor(run_id,todo.todo_id,'CONSUMER_DEPENDENCY_BINDING_V1',key,payload)
        return payload

    @diagnostic
    def active_lease(self,todo,run_id,lease_id):
        lease=self.manager.get_active(lease_id)
        self.validate_consumer(todo,run_id,lease)
        return lease

    @diagnostic
    def validate_consumer(self,todo,run_id,lease=None):
        if not todo.dependencies:return None
        run=self.runs.get_run(run_id)
        require((run.goal_id,run.active_todo_id)==(self.spec.goal_id,todo.todo_id)
                and run.status is not RunStatus.CANCELLED
                and persisted_responsibility(self.runs,run_id)==self.workspace._responsibility(todo.work_contract),
                'DEPENDENCY_CONSUMER_RESPONSIBILITY_MISMATCH','authoritative consumer Run')
        key='dependency-consumer:'+fingerprint([self.spec.mission_id,todo.todo_id,run_id])
        b,ref=self.load(key);self.anchored(run_id,'CONSUMER_DEPENDENCY_BINDING_V1',key,b)
        bound,_=self.load(b['producer_receipt_id'])
        self.validate_receipt(self.todo(todo.dependencies[0]),bound)
        d,producer_ref=self.prerequisites(todo)[0]
        require(b==dict(schema_version=1,mission_id=self.spec.mission_id,goal_id=self.spec.goal_id,
            consumer_todo_id=todo.todo_id,consumer_run_id=run_id,consumer_lease_id=b['consumer_lease_id'],
            consumer_responsibility=self.workspace._responsibility(todo.work_contract).to_dict(),
            required_graph_fingerprint=self.graph_fingerprint,producer_receipt_id=producer_ref.artifact_id,
            producer_receipt_sha256=producer_ref.sha256,producer_receipt_fingerprint=d['receipt_fingerprint'],
            dependency_version=d['dependency_version'],consumer_base_commit=d['accepted_commit_sha']),
            'DEPENDENCY_CONSUMER_BINDING_MISMATCH','immutable binding; no implicit rebase')
        if lease is not None:
            require((lease.lease_id,lease.run_id,lease.todo_id,lease.base_commit)==
                    (b['consumer_lease_id'],run_id,todo.todo_id,b['consumer_base_commit']),
                    'DEPENDENCY_CONSUMER_LEASE_MISMATCH','exact base/lease')
            head=self.manager.git.text(lease.worktree_root,'rev-parse','HEAD')
            require(head==b['consumer_base_commit'],'DEPENDENCY_CONSUMER_HEAD_MISMATCH','checkout')
            for path,blob in d['source_blobs'].items():
                require(self.manager.git.text(lease.worktree_root,'hash-object','--path='+path,'--',path)==blob,
                        'DEPENDENCY_SOURCE_CHANGED',path)
        return b,d,ref

    @diagnostic
    def resume(self,todo):
        if self.artifacts.has_artifact(self.key(todo,'acceptance')):
            return self.commit(todo)
        require(not self.artifacts.has_artifact(self.key(todo,'prepared')),
                'DEPENDENCY_PUBLICATION_INTERRUPTED','prepared state cannot release dependency')
        # Without an accepted receipt, a restarted partially executed producer
        # cannot be promoted or regenerated based on model output alone.
        for run in self.runs.list_runs():
            if (run.active_todo_id==todo.todo_id and run.quota_usage.worker_calls
                    and persisted_responsibility(self.runs,run.run_id)==self.workspace._responsibility(todo.work_contract)):
                require(False,'DEPENDENCY_EXECUTION_INTERRUPTED','no published acceptance; no model replay')
        return None

    @diagnostic
    def public_binding(self,todo,run_id,lease):
        from harness.verification.callable_authority import VerifiedAcceptedDependencyV1
        b,d,_=self.validate_consumer(todo,run_id,lease)
        ref=self.artifacts.get_artifact(b['producer_receipt_id']).ref
        payload={k:d[k] for k in ('required_graph_fingerprint','producer_logical_todo_id',
            'producer_physical_todo_id','producer_run_id','producer_attempt_id','producer_lease_id',
            'accepted_commit_sha','accepted_tree_sha','dependency_version','receipt_fingerprint')}
        payload.update(schema_version=1,consumer_run_id=run_id,consumer_todo_id=todo.todo_id,
            consumer_lease_id=lease.lease_id,receipt_artifact_id=ref.artifact_id,receipt_sha256=ref.sha256,
            source_sha256={path:hashlib.sha256((Path(lease.worktree_root)/path).read_text(encoding='utf8').encode('utf8')).hexdigest()
                           for path in d['source_blobs']})
        return VerifiedAcceptedDependencyV1(canonical(payload))

    @diagnostic
    def context(self,todo):
        if not todo.dependencies:return None
        run_id,lease_id=self.workspace._current[todo.todo_id]
        b,d,binding_ref=self.validate_consumer(todo,run_id,self.manager.get_active(lease_id))
        artifact=self.artifacts.get_artifact(d['snapshot_artifact_id']);raw=json.loads(artifact.content)
        from harness.workflow.models import ProjectSnapshotV1
        snapshot=ProjectSnapshotV1(raw['goal_id'],raw['epoch'],raw['head_commit'],raw['diff_sha256'],tuple(raw['dirty_paths']))
        accepted_sources={'accepted_commit_sha':d['accepted_commit_sha'],'receipt_fingerprint':d['receipt_fingerprint'],
            'sources':{path:(Path(self.manager.get_active(lease_id).worktree_root)/path).read_text(encoding='utf8')
                       for path in d['source_blobs']}}
        source_ref=self.put('dependency-source-context:'+d['receipt_fingerprint'],accepted_sources)
        refs=(self.artifacts.get_artifact(d['accepted_outcome_reference']).ref,
              self.artifacts.get_artifact(b['producer_receipt_id']).ref,binding_ref,source_ref)
        return snapshot,artifact.ref,refs

    @diagnostic
    def execution_context_refs(self,todo,run_id):
        """Project already verified evidence into this consumer's context scope.

        Artifact context scope is separate from workspace mutation authority.
        Keep ContextBuilder's scope gate unchanged and retain the original
        artifact identities/hashes in durable provenance. Never retag originals.
        """
        _,lease_id=self.workspace._current[todo.todo_id]
        b,d,binding_ref=self.validate_consumer(todo,run_id,self.manager.get_active(lease_id))
        refs=self.context(todo)[2]
        projected=[];rows=[]
        for ref in refs:
            original=self.artifacts.get_artifact(ref.artifact_id)
            metadata={**original.metadata,'dependency_original_ref':ref.to_dict(),
                'consumer_binding_ref':binding_ref.to_dict(),'receipt_fingerprint':d['receipt_fingerprint']}
            key='dependency-context-projection:'+fingerprint([run_id,ref.to_dict(),binding_ref.to_dict()])
            projected_ref=self.put(key,json.loads(original.content),
                scope_tags=tuple(todo.work_contract.allowed_scope),metadata=metadata)
            projected.append(projected_ref)
            rows.append({'original':ref.to_dict(),'projection':projected_ref.to_dict()})
        payload={'consumer_binding':binding_ref.to_dict(),'receipt_fingerprint':d['receipt_fingerprint'],'artifacts':rows}
        key='dependency-execution-context:'+fingerprint([run_id,binding_ref.to_dict()])
        self.put(key,payload);self.anchor(run_id,todo.todo_id,'CONSUMER_DEPENDENCY_EXECUTION_CONTEXT',key,payload)
        return tuple(projected)

    @diagnostic
    def record_context(self,todo,objective,pack):
        if not todo.dependencies:return
        run_id,lease_id=self.workspace._current[todo.todo_id]
        b,d,ref=self.validate_consumer(todo,run_id,self.manager.get_active(lease_id))
        text=self.workspace.spine.objective_with_pack(objective,pack)
        digest=hashlib.sha256(text.encode('utf8')).hexdigest()
        payload=dict(consumer_run_id=run_id,consumer_lease_id=lease_id,binding_id=ref.artifact_id,
            binding_sha256=ref.sha256,receipt_fingerprint=d['receipt_fingerprint'],
            context_fingerprint=pack.context_fingerprint,text_sha256=digest,
            required_artifact_ids=list(pack.required_artifact_ids))
        key='dependency-context:'+fingerprint([run_id,digest]);self.put(key,payload)
        self.anchor(run_id,todo.todo_id,'CONSUMER_DEPENDENCY_CONTEXT:'+digest,key,payload)

    @diagnostic
    def validate_context(self,todo,run_id,text):
        _,lease_id=self.workspace._current[todo.todo_id]
        b,d,ref=self.validate_consumer(todo,run_id,self.manager.get_active(lease_id))
        digest=hashlib.sha256(text.encode('utf8')).hexdigest()
        key='dependency-context:'+fingerprint([run_id,digest])
        require(self.artifacts.has_artifact(key),'DEPENDENCY_CONTEXT_MISMATCH','unrecognized model-facing context')
        payload,_=self.load(key)
        self.anchored(run_id,'CONSUMER_DEPENDENCY_CONTEXT:'+digest,key,payload)
        require((payload['consumer_run_id'],payload['consumer_lease_id'],payload['binding_id'],payload['binding_sha256'],payload['receipt_fingerprint'])==
                (run_id,lease_id,ref.artifact_id,ref.sha256,d['receipt_fingerprint']),
                'DEPENDENCY_CONTEXT_MISMATCH','model-facing context provenance')
