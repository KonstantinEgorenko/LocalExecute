"""Durable, bounded public interpretation. Final Judge retains acceptance authority."""
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import time

from ..agent_loop.protocol import decode_strict_json
from ..context_manager.models import Artifact, ArtifactKind, ArtifactOrigin
from ..judge.protocol import parse_public_interpretations
from ..verification.acceptance import (AcceptanceObligationsV1, PublicInterpretationV1,
    canonical, fingerprint, independent_obligation_refs)
from .public_interpreter_contract import (
    PublicSemanticGroupV1, authority_request, compact_envelope,
    parse_authority, semantic_groups, witness_request, FRESH_INSTRUCTION, recovery_state)
from ..verification.witness_domain import validate_witness_inputs

REVISION = 'DECOMPOSED_PUBLIC_INTERPRETER_V1'
ROLE = 'PUBLIC_INTERPRETER'
MAX_SEMANTIC_GROUPS = 1
MAX_MODEL_CALLS = 3
AUTHORITY = 'PUBLIC_AUTHORITY_RESOLUTION'
WITNESS = 'PUBLIC_WITNESS_SYNTHESIS'
RESYNTHESIS = 'POST_REJECTION_RESYNTHESIS'


class PublicInterpreterError(RuntimeError):
    def __init__(self, code, detail=''):
        self.code = code
        super().__init__(code + (': ' + detail if detail else ''))


@dataclass(frozen=True, slots=True)
class PublicInterpreterModelV1:
    model_id: str = 'qwen27_heavy_quality'
    model_tag: str = 'qwen36-27b-heavy-quality:latest'
    model_digest: str = '6f131b48efc62e4f3feed925a07c729ecbcf3564338f0a65d8f8e46905fa1a9c'

    def __post_init__(self):
        if (not self.model_id or not self.model_tag or len(self.model_digest) != 64
                or any(c not in '0123456789abcdef' for c in self.model_digest)):
            raise ValueError('invalid interpreter generation provenance')


@dataclass(frozen=True, slots=True)
class PublicAuthorityResolutionV1:
    group_id: str
    status: str
    public_refs: tuple[str, ...]

    def __post_init__(self):
        if (not self.group_id or self.status not in ('SUPPORTED','CONFLICTING','AMBIGUOUS')
                or type(self.public_refs) is not tuple or not self.public_refs):
            raise ValueError('invalid authority resolution')


@dataclass(frozen=True, slots=True)
class PublicInterpreterAttemptV1:
    run_id: str
    todo_id: str
    root_acceptance_fingerprint: str
    interpreter_revision: str
    group_fingerprint: str
    stage: str
    attempt_index: int
    model: PublicInterpreterModelV1
    request_fingerprint: str
    rejected_proposal_fingerprint: str = ''
    validation_fingerprint: str = ''

    def to_dict(self):
        return asdict(self)


def recovery_projection(frozen, group, checked, initial, request=None):
    """Verify the complete typed rejection binding before creating model context."""
    if replace(checked,public_interpretations=()) != frozen:
        raise PublicInterpreterError('PUBLIC_INTERPRETER_RECOVERY_PROJECTION_VIOLATION','snapshot changed')
    rows = {r.obligation_id:r for r in checked.public_interpretations}
    if len(rows) != len(checked.public_interpretations) or set(rows) != set(group.obligation_ids):
        raise PublicInterpreterError('PUBLIC_INTERPRETER_RECOVERY_PROJECTION_VIOLATION','group coverage')
    states = []
    for oid in group.obligation_ids:
        row = rows[oid]
        v = row.input_validation
        obligation = next(o for o in frozen.obligations if o.obligation_id == oid)
        if (v is None or row.witness is None or row.unresolved
                or v.status != 'INVALID' or v.reason != 'ORIGINAL_CALL_NOT_ADMISSIBLE'
                or v.run_id != frozen.run_id or v.todo_id != frozen.todo_id
                or v.public_snapshot_fingerprint != fingerprint(frozen.to_dict())
                or v.obligation_id != oid or v.public_refs != row.public_refs
                or set(row.public_refs) != set(independent_obligation_refs(frozen,obligation))
                or (v.origin_path,v.origin_symbol,v.signature_fingerprint) !=
                   (group.module_path,group.callable_name,group.signature_fingerprint)
                or v.arguments_fingerprint != fingerprint(decode_strict_json(row.witness.arguments_json))):
            raise PublicInterpreterError('PUBLIC_INTERPRETER_RECOVERY_PROJECTION_VIOLATION','rejection binding')
        states.append(recovery_state(v))
    if not states or any(s != states[0] for s in states):
        raise PublicInterpreterError('PUBLIC_INTERPRETER_RECOVERY_PROJECTION_VIOLATION','mixed rejection class')
    # Reconstruct from the frozen production projection, never from a rejected proposal.
    expected_initial = witness_request(frozen,group)
    if initial != expected_initial:
        raise PublicInterpreterError('PUBLIC_INTERPRETER_RECOVERY_PROJECTION_VIOLATION','initial drift')
    packet = decode_strict_json(initial[1])
    packet['recovery_state'] = states[0]
    expected = (initial[0]+FRESH_INSTRUCTION,canonical(packet),initial[2])
    if request is not None and request != expected:
        raise PublicInterpreterError('PUBLIC_INTERPRETER_RECOVERY_PROJECTION_VIOLATION','packet drift')
    return expected, {'authority_projection_matches_initial':True,
        'added_top_level_fields':['recovery_state'],'response_schema_matches_initial':True,
        'rejected_semantics_exposed_to_model':False}


class PublicInterpreterCoordinator:
    """Create-only stage artifacts, not a scheduler or conversation store.

    Pending without complete means dispatch is ambiguous: restart fails closed.
    A completed raw response can be reparsed without repeating physical inference.
    """
    def __init__(self, *, artifact_store, runtime, runner, observer=None):
        self.artifacts = artifact_store
        self.runtime = runtime
        self.runner = runner
        self.model = runtime.model
        self.observer = observer

    def _identity(self,frozen,group,stage,attempt,request,checked=None):
        return PublicInterpreterAttemptV1(frozen.run_id,frozen.todo_id,
            fingerprint(frozen.to_dict()),REVISION,fingerprint(asdict(group)) if group else '',
            stage,attempt,self.model,fingerprint(request),
            fingerprint([asdict(replace(r,input_validation=None)) for r in checked.public_interpretations]) if checked else '',
            fingerprint([asdict(r.input_validation) for r in checked.public_interpretations]) if checked else '')

    def _id(self,identity,suffix):
        return f'{identity.run_id}:public-interpreter:{fingerprint(identity.to_dict())}:{suffix}'

    def _load(self,identity,suffix):
        aid = self._id(identity,suffix)
        if not self.artifacts.has_artifact(aid):
            return None
        artifact = self.artifacts.get_artifact(aid)
        row = decode_strict_json(artifact.content)
        if artifact.source != ROLE or row['identity'] != identity.to_dict():
            raise PublicInterpreterError('PUBLIC_INTERPRETER_CACHE_IDENTITY_BUG')
        return row['value']

    def _save(self,identity,suffix,value):
        old = self._load(identity,suffix)
        if old is not None:
            if canonical(old) != canonical(value):
                raise PublicInterpreterError('PUBLIC_INTERPRETER_CACHE_IDENTITY_BUG','artifact conflict')
            return
        self.artifacts.create_artifact(Artifact.from_text(artifact_id=self._id(identity,suffix),
            kind=ArtifactKind.OTHER,origin=ArtifactOrigin.CONTROL_PLANE,
            content=canonical({'identity':identity.to_dict(),'value':value}),
            media_type='application/json',source=ROLE,
            created_at=datetime.now(timezone.utc).isoformat(),metadata=identity.to_dict()))

    def _stage(self,identity,request=None,operation=None):
        if request is not None and (identity.stage,identity.attempt_index) not in (
                (AUTHORITY,0),(WITNESS,0),(RESYNTHESIS,1)):
            raise PublicInterpreterError('PUBLIC_INTERPRETER_CALL_BUDGET_VIOLATION')
        completed = self._load(identity,'complete')
        if completed is not None:
            if self.observer is not None and request is not None:
                self.observer(identity.to_dict(),completed)
            return completed
        if self._load(identity,'pending') is not None:
            raise PublicInterpreterError('PUBLIC_INTERPRETER_DISPATCH_AMBIGUOUS',identity.stage)
        self._save(identity,'pending',{'request':request,'disposition':'PENDING'})
        start = time.monotonic()
        try:
            value = operation() if operation is not None else self.runtime.complete(
                *request,identity=identity.to_dict())
            result = {'disposition':'COMPLETE','result':value,
                'wall_seconds':time.monotonic()-start}
        except Exception as exc:
            result = {'disposition':'FAILED','error_code':getattr(exc,'code','PUBLIC_INTERPRETER_UNAVAILABLE'),
                'exception_type':type(exc).__name__,'detail':str(exc)[:2048],
                'wall_seconds':time.monotonic()-start}
        if request is not None:
            result['generation_dispatched'] = bool(getattr(self.runtime,'dispatch_started',True))
            result['generation_metadata'] = getattr(self.runtime,'last_generation_metadata',{})
        self._save(identity,'complete',result)
        if self.observer is not None and request is not None:
            self.observer(identity.to_dict(),result)
        return result

    @staticmethod
    def _result(value):
        if value['disposition'] != 'COMPLETE':
            raise PublicInterpreterError(value['error_code'],value['detail'])
        return value['result']

    def _domain(self,frozen,group,rows,attempt):
        identity = self._identity(frozen,group,'DOMAIN_VALIDATION',attempt,
            [asdict(r) for r in rows])
        def validate():
            checked = replace(frozen,public_interpretations=validate_witness_inputs(
                frozen,rows,self.runner,decode=decode_strict_json))
            return checked.to_dict()
        result = self._result(self._stage(identity,operation=validate))
        checked = AcceptanceObligationsV1.from_dict(result)
        if replace(checked,public_interpretations=()) != frozen:
            raise PublicInterpreterError('PUBLIC_INTERPRETER_CACHE_IDENTITY_BUG','domain snapshot drift')
        return checked

    def _unresolved(self,frozen,code,group=None):
        ids = set(group.obligation_ids) if group else {o.obligation_id for o in frozen.obligations if o.kind != 'VERIFICATION'}
        rows = tuple(PublicInterpretationV1(o.obligation_id,code,
            tuple(sorted(independent_obligation_refs(frozen,o))),True)
            for o in frozen.obligations if o.obligation_id in ids)
        return replace(frozen,public_interpretations=rows,
            unresolved_semantics=(*frozen.unresolved_semantics,code))

    def _interpret_bounded_groups(self,frozen,groups):
        """One authority request and one witness per frozen group; no retry.

        The explicit multi-API contract retains the complete normative texts
        in mandatory API rows. Candidate bodies never enter these requests.
        Existing domain validation and final Judge retain semantic authority.
        """
        from ..verification.acceptance import public_callable_surface
        from ..verification.public_witness_contract import callable_witness_contract
        from ..verification.callable_authority import from_frozen, declaration_for, observe_interface
        declared = from_frozen(frozen) is not None
        for group in groups:
            surface=next(s for s in public_callable_surface(frozen) if (s['module_path'],s['callable_name'])==(group.module_path,group.callable_name))
            try:
                capability=callable_witness_contract(frozen,surface)
                readiness=capability.readiness()
                # Actual production runtime qualifies the exact projected schema
                # without preload. Test runtimes lacking a compiler cannot claim
                # backend compatibility from a synthetic model response.
                qualify=getattr(self.runtime,'qualify_witness_schema',None)
                if declared and qualify is None:
                    raise ValueError('PUBLIC_WITNESS_GRAMMAR_COMPILER_UNAVAILABLE')
                if qualify is not None:
                    schema=witness_request(frozen,group)[2]
                    proof=qualify(schema)
                    from ..verification.witness_grammar import schema_bytes,project_witness_schema,PROJECTION_REVISION
                    import hashlib
                    if (proof.get('status')!='NATIVE_COMPILER_PROVEN'
                            or proof.get('projection_revision')!=PROJECTION_REVISION
                            or proof.get('authoritative_schema_sha256')!=hashlib.sha256(schema_bytes(schema)).hexdigest()
                            or proof.get('backend_schema_sha256')!=hashlib.sha256(schema_bytes(project_witness_schema(schema))).hexdigest()):
                        raise ValueError('PUBLIC_WITNESS_GRAMMAR_QUALIFICATION_DRIFT')
                    readiness['BACKEND_GRAMMAR_COMPATIBLE']='NATIVE_COMPILER_PROVEN'
                    readiness['WITNESS_READY_FOR_DISPATCH']=True
                    readiness['backend_grammar']=proof
                readiness['binding']=asdict(group)
                import hashlib,ast
                from ..verification.acceptance import SemanticProvenanceV1,public_signature
                source=next(e for e in frozen.evidence if e.provenance is SemanticProvenanceV1.OBSERVED_IMPLEMENTATION and e.path==group.module_path)
                source_sha=hashlib.sha256(source.content.encode('utf-8')).hexdigest()
                if source.content_sha256!=source_sha:raise ValueError('PUBLIC_WITNESS_SOURCE_FINGERPRINT_MISMATCH')
                if declared:
                    row = declaration_for(frozen,group.module_path,group.callable_name)
                    observed = observe_interface(source.content,group.module_path,group.callable_name,row['kind'])
                    if observed.present and observed.signature_fingerprint != group.signature_fingerprint:
                        raise ValueError('PUBLIC_WITNESS_SIGNATURE_MISMATCH')
                    readiness['original_interface'] = asdict(observed)
                    readiness['declared_absence_permitted'] = row['original_presence']=='DECLARED_ABSENT'
                else:
                    node=next(n for n in ast.parse(source.content).body if isinstance(n,ast.FunctionDef) and n.name==group.callable_name)
                    if fingerprint(public_signature(node))!=group.signature_fingerprint:raise ValueError('PUBLIC_WITNESS_SIGNATURE_MISMATCH')
                readiness['source_sha256']=source_sha
                # Binding was frozen from public origins. Revalidate every
                # obligation; no candidate output supplies readiness authority.
                from ..verification.acceptance import validate_witness_binding
                for oid in group.obligation_ids:
                    obligation=next(o for o in frozen.obligations if o.obligation_id==oid)
                    validate_witness_binding(frozen,obligation,group.module_path,group.callable_name)
                identity=self._identity(frozen,group,'WITNESS_READINESS',0,readiness)
                self._save(identity,'complete',readiness)
            except ValueError as exc:
                if 'GRAMMAR' in str(exc):
                    raise PublicInterpreterError('PUBLIC_WITNESS_BACKEND_GRAMMAR_INCOMPATIBLE',str(exc)) from exc
                raise PublicInterpreterError('PUBLIC_WITNESS_CAPABILITY_UNSUPPORTED',str(exc)) from exc
        request=authority_request(frozen,groups)
        identity=self._identity(frozen,None,AUTHORITY,0,request)
        authority=parse_authority(self._result(self._stage(identity,request)),groups)['groups']
        combined=[];unresolved=[]
        for group in groups:
            if authority[group.group_id]['status']!='SUPPORTED':
                checked=self._unresolved(frozen,'PUBLIC_AUTHORITY_'+authority[group.group_id]['status'],group)
            else:
                subset=replace(frozen,obligations=tuple(o for o in frozen.obligations
                    if o.obligation_id in group.obligation_ids or o.kind=='VERIFICATION'))
                request=witness_request(subset,group)
                identity=self._identity(frozen,group,WITNESS,0,request)
                raw=self._result(self._stage(identity,request))
                try:
                    rows=parse_public_interpretations(compact_envelope(raw,group,subset),subset)
                    checked=self._domain(frozen,group,rows,0)
                except (ValueError,TypeError,KeyError) as exc:
                    self._save(identity,'protocol-failure',{'error_code':'PUBLIC_INTERPRETER_PROTOCOL_INVALID',
                        'group':asdict(group),'raw_response':raw,'parser_error':str(exc)[:2048],
                        'public_snapshot_fingerprint':fingerprint(frozen.to_dict()),'repairable':False})
                    raise PublicInterpreterError('PUBLIC_INTERPRETER_PROTOCOL_INVALID',str(exc)) from exc
                if not all(r.input_validation is not None and r.input_validation.status=='VALID'
                        for r in checked.public_interpretations):
                    checked=self._unresolved(frozen,'PUBLIC_INTERPRETATION_UNRESOLVED',group)
            combined.extend(checked.public_interpretations)
            unresolved.extend(checked.unresolved_semantics)
        expected={o.obligation_id for o in frozen.obligations if o.kind!='VERIFICATION'}
        if {r.obligation_id for r in combined}!=expected or len(combined)!=len(expected):
            raise PublicInterpreterError('PUBLIC_INTERPRETER_TOPOLOGY_UNSUPPORTED')
        return replace(frozen,public_interpretations=tuple(combined),
            unresolved_semantics=tuple(dict.fromkeys(unresolved)))

    def interpret_public_requirements(self,frozen):
        if frozen.public_interpretations or frozen.unresolved_semantics:
            return frozen
        # Persist the original snapshot before the first physical call. The
        # acceptance bridge publishes the enriched final snapshot afterwards;
        # restart must not establish a different original baseline in between.
        snapshot_id = f'{frozen.run_id}:public-interpreter:root-snapshot:{frozen.contract_fingerprint}'
        if self.artifacts.has_artifact(snapshot_id):
            snapshot = self.artifacts.get_artifact(snapshot_id)
            if snapshot.source != ROLE or canonical(decode_strict_json(snapshot.content)) != canonical(frozen.to_dict()):
                raise PublicInterpreterError('PUBLIC_INTERPRETER_SNAPSHOT_CHANGED')
        else:
            self.artifacts.create_artifact(Artifact.from_text(artifact_id=snapshot_id,
                kind=ArtifactKind.OTHER,origin=ArtifactOrigin.CONTROL_PLANE,
                content=canonical(frozen.to_dict()),media_type='application/json',source=ROLE,
                created_at=datetime.now(timezone.utc).isoformat(),
                metadata={'stage':'ROOT_SNAPSHOT','run_id':frozen.run_id,'todo_id':frozen.todo_id,
                    'root_acceptance_fingerprint':fingerprint(frozen.to_dict())}))
        grouping_error = None
        try:
            groups = semantic_groups(frozen)
        except ValueError:
            groups = ()
            grouping_error = 'PUBLIC_INTERPRETER_TOPOLOGY_UNSUPPORTED'
        group = groups[0] if len(groups) == MAX_SEMANTIC_GROUPS else None
        final_identity = self._identity(frozen,group,'FINAL_INTERPRETATION',0,REVISION)
        if group is None:
            final_identity = replace(final_identity,
                group_fingerprint=fingerprint([asdict(g) for g in groups]))
        final = self._load(final_identity,'complete')
        failure=self._load(final_identity,'failure')
        if failure is not None:
            raise PublicInterpreterError(failure['error_code'],failure['detail'])
        if final is not None:
            if self.observer is not None:
                for ref in self.artifacts.list_refs():
                    if not ref.artifact_id.endswith(':complete'):
                        continue
                    artifact = self.artifacts.get_artifact(ref.artifact_id)
                    metadata = artifact.metadata
                    if (artifact.source == ROLE and metadata.get('stage') in (AUTHORITY,WITNESS,RESYNTHESIS)
                            and metadata.get('run_id') == frozen.run_id
                            and metadata.get('root_acceptance_fingerprint') == fingerprint(frozen.to_dict())
                            and metadata.get('model') == asdict(self.model)):
                        row = decode_strict_json(artifact.content)
                        self.observer(row['identity'],row['value'])
            result = AcceptanceObligationsV1.from_dict(final)
            if replace(result,public_interpretations=(),unresolved_semantics=frozen.unresolved_semantics) != frozen:
                raise PublicInterpreterError('PUBLIC_INTERPRETER_CACHE_IDENTITY_BUG','final snapshot drift')
            return result
        try:
            from ..verification.acceptance import public_group_limit
            group_limit=public_group_limit(frozen)
            from ..verification.callable_authority import from_frozen
            declared = from_frozen(frozen) is not None
            if group_limit==3 or declared:
                if grouping_error or not 1<=len(groups)<=group_limit:
                    raise PublicInterpreterError('PUBLIC_INTERPRETER_TOPOLOGY_UNSUPPORTED')
                result=self._interpret_bounded_groups(frozen,groups)
                self._save(final_identity,'complete',result.to_dict())
                return result
            if grouping_error or len(groups) != MAX_SEMANTIC_GROUPS:
                raise PublicInterpreterError(grouping_error or 'PUBLIC_INTERPRETER_TOPOLOGY_UNSUPPORTED')
            group, = groups
            request = authority_request(frozen,groups)
            identity = self._identity(frozen,group,AUTHORITY,0,request)
            raw = self._result(self._stage(identity,request))
            row = parse_authority(raw,groups)['groups'][group.group_id]
            authority = PublicAuthorityResolutionV1(row['group_id'],row['status'],tuple(row['public_refs']))
            if authority.status != 'SUPPORTED':
                result = self._unresolved(frozen,'PUBLIC_AUTHORITY_'+authority.status,group)
            else:
                request = witness_request(frozen,group)
                identity = self._identity(frozen,group,WITNESS,0,request)
                raw = self._result(self._stage(identity,request))
                rows = parse_public_interpretations(compact_envelope(raw,group,frozen),frozen)
                checked = self._domain(frozen,group,rows,0)
                validations = [r.input_validation for r in checked.public_interpretations]
                if all(v is not None and v.status == 'VALID' for v in validations):
                    result = checked
                elif all(v is not None and v.status == 'INVALID' and
                         v.reason == 'ORIGINAL_CALL_NOT_ADMISSIBLE' for v in validations):
                    fresh,proof = recovery_projection(frozen,group,checked,request)
                    identity = self._identity(frozen,group,RESYNTHESIS,1,fresh,checked)
                    self._save(identity,'rejected',{'checked':checked.to_dict(),'packet_proof':proof})
                    recovery_projection(frozen,group,checked,request,fresh)
                    raw = self._result(self._stage(identity,fresh))
                    rows = parse_public_interpretations(compact_envelope(raw,group,frozen),frozen)
                    checked = self._domain(frozen,group,rows,1)
                    result = checked if all(r.input_validation is not None and
                        r.input_validation.status == 'VALID' for r in checked.public_interpretations) else self._unresolved(
                            frozen,'PUBLIC_INTERPRETATION_UNRESOLVED',group)
                else:
                    result = self._unresolved(frozen,'PUBLIC_INTERPRETATION_UNRESOLVED',group)
        except PublicInterpreterError as exc:
            if group_limit==3 or declared:
                self._save(final_identity,'failure',{'error_code':exc.code,'detail':str(exc)[:2048],'repairable':False})
                raise
            result = self._unresolved(frozen,exc.code,group)
        except (ValueError,TypeError,KeyError) as exc:
            if group_limit==3 or declared:
                self._save(final_identity,'failure',{'error_code':'PUBLIC_INTERPRETER_PROTOCOL_INVALID','detail':str(exc)[:2048],'repairable':False})
                raise PublicInterpreterError('PUBLIC_INTERPRETER_PROTOCOL_INVALID',str(exc)) from exc
            result = self._unresolved(frozen,'PUBLIC_INTERPRETER_PROTOCOL_INVALID',group)
        self._save(final_identity,'complete',result.to_dict())
        return result
