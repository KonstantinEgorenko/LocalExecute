"""Native SIMPLE checkpoints in the existing RunStore, never acceptance authority."""
import hashlib
import json
from urllib.error import HTTPError

from harness.control_plane import Evidence, EvidenceStatus
from harness.errors import ExecutionHttpError
from harness.simple_worker_v2.edit_intent import TypedEditIntentV1
from harness.workspace import Workspace


class NativeRecoveryError(ValueError):
    def __init__(self, category):
        self.category = category
        super().__init__(category)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False).encode('utf-8')).hexdigest()


def http_server_failure(exc):
    """Only an acknowledged server error at generation, not a timeout/parse error."""
    cause = exc.__cause__
    context = getattr(exc, 'failure_context', None)
    return (isinstance(exc, ExecutionHttpError) and isinstance(cause, HTTPError)
            and 500 <= cause.code <= 599
            and exc.response_error_kind == 'HTTP_SERVER_ERROR'
            and getattr(getattr(context, 'stage', None), 'value', None) == 'generation')


class NativeTransportCheckpoint:
    def __init__(self, *, store, session, contract, lease_id, model, reopen_semantic_failure=True):
        self.store, self.session = store, session
        self.reopen_semantic_failure = reopen_semantic_failure
        self.binding = {'run_id':session.run_id, 'todo_id':session.todo_id,
            'contract':fingerprint(contract.to_dict()), 'lease_id':lease_id, 'model':model}
        self.prefix = session.run_id + ':native-transport:' + fingerprint({
            'todo_id':session.todo_id,'lease_id':lease_id})
        self.generation=None
        records=self.records()
        progress=[e.to_dict()['payload']['payload'] for e in store.list_events(session.run_id)
            if e.to_dict()['payload'].get('evidence_type')=='NATIVE_RECOVERY_PROGRESS'
            and e.to_dict()['payload']['payload'].get('binding')==self.binding]
        self.generation=max((r.get('generation',0) for r in (*records,*progress)),default=0)
        current=[r for r in records if r.get('generation',0)==self.generation]
        # A known semantic/ACI failure continues through the existing outer
        # recovery budget. Ambiguous and exhausted HTTP attempts do not reopen.
        if reopen_semantic_failure and any(r['key'].endswith(':FAILED') and r.get('category') not in {
                    'HTTP_SERVER_ERROR','TOOL_CALL_PROTOCOL_ERROR','TOOL_ARGUMENT_PARSE_ERROR',
                    'TRANSPORT_HTTP_5XX','TRANSPORT_HTTP_4XX','TRANSPORT_TIMEOUT','TRANSPORT_CONNECTION_ERROR',
                    'AMBIGUOUS_NATIVE_INVOCATION','STALE_NATIVE_TEST_REQUEST',
                    'TEST_REPAIR_CONTEXT_BUDGET_EXCEEDED','TEST_REPAIR_CONTEXT_INVALID'}
               for r in current):
            failed = next(r for r in reversed(current) if r['key'].endswith(':FAILED'))
            versions = failed.get('versions', ())
            if versions:
                self.guard(versions)
            report = (failed.get('result') or {}).get('report') or {}
            report = {**report, 'results':report.get('results', [])}
            from .source_repair import repair_evidence
            observed = repair_evidence('', {}, '', report, 0)
            frontier = fingerprint({'binding':self.binding, 'versions':versions,
                'failure_kind':failed.get('category'),
                'failed_tests':observed['failed_test_names'],
                'expected_actual':observed['expected_actual_observations'],
                'pytest_stderr':observed['pytest_stderr'],
                'failed_gates':[(g.get('gate_id'), g.get('outcome')) for g in report.get('results', [])]})
            if any(r.get('fingerprint')==frontier for r in progress):
                raise NativeRecoveryError('NO_PROGRESS')
            source = next((r for r in reversed(current) if r['key']=='SOURCE:PASS'), None)
            initial = next((r for r in reversed(current) if r['key']=='INTENT'), None)
            retained = None
            if failed['key']=='TEST:FAILED' and source is not None and initial is not None:
                source_paths = {v[0] for v in initial['intent']['source_versions']}
                expected = [v for v in source['versions'] if v[0] in source_paths]
                self.guard(expected)
                raw_hashes = dict((source['result'].get('report') or {}).get('candidate_file_sha256', ()))
                for path in source_paths:
                    if raw_hashes and (path not in raw_hashes or hashlib.sha256(
                            Workspace(session)._candidate(path)[1].read_bytes()).hexdigest() != raw_hashes[path]):
                        raise NativeRecoveryError('STALE_NATIVE_TEST_REQUEST')
                retained = (initial, source)
            self.generation+=1
            self.store.record_evidence(self.session.run_id, Evidence(
                self.prefix+':frontier:'+str(self.generation),self.session.todo_id,
                'NATIVE_RECOVERY_PROGRESS','NativeSimpleRecovery',EvidenceStatus.PASSED,
                {'binding':self.binding,'generation':self.generation,'fingerprint':frontier,
                 'failure_kind':failed.get('category')}))
            if retained is not None:
                initial, source = retained
                self.put('INTENT', intent=initial['intent'])
                # Retain a proven applied mutation, not a planned edit or a
                # current PASS. Final TEST verification must run on the new candidate.
                result = {**source['result'], 'calls':0,
                    'status':'SOURCE_MUTATION_RETAINED',
                    'requires_fresh_final_verification':True,
                    'source_report_provenance':'HISTORICAL_SOURCE_SUBSTEP_ONLY'}
                self.put('SOURCE:PASS', versions=self.versions([v[0] for v in versions]), result=result)
                self.put('SOURCE_RETAINED_PROOF', parent_generation=source.get('generation',0),
                    source_result_fingerprint=fingerprint(source['result']), versions=expected)

    def records(self):
        records=[e.to_dict()['payload']['payload'] for e in self.store.list_events(self.session.run_id)
                if e.to_dict()['payload'].get('evidence_type') == 'NATIVE_TRANSPORT_CHECKPOINT'
                and e.to_dict()['payload'].get('evidence_id', '').startswith(self.prefix + ':')]
        if any(r['binding']!=self.binding for r in records):
            raise NativeRecoveryError('STALE_NATIVE_TEST_REQUEST')
        return [r for r in records if self.generation is None or r.get('generation',0)==self.generation]

    def get(self, key):
        return next((r for r in reversed(self.records()) if r['key'] == key), None)

    def put(self, key, **payload):
        status=EvidenceStatus.FAILED if key.endswith((':FAILED',':HTTP_SERVER_ERROR',':PROTOCOL_ERROR')) else EvidenceStatus.PASSED
        self.store.record_evidence(self.session.run_id, Evidence(self.prefix+':'+str(self.generation)+':'+key,
            self.session.todo_id, 'NATIVE_TRANSPORT_CHECKPOINT', 'NativeSimpleRecovery',
            status, {'key':key,'binding':self.binding,'generation':self.generation,**payload}))

    def versions(self, paths):
        workspace=Workspace(self.session)
        result=[]
        for path in paths:
            _,target=workspace._candidate(path, mutation=True)
            canonical=target.resolve(strict=True).relative_to(workspace.root).as_posix()
            _,text=workspace.read_text(path)
            result.append([path,canonical,hashlib.sha256(text.encode('utf-8')).hexdigest()])
        return result

    def guard(self, versions):
        if self.versions([v[0] for v in versions]) != versions:
            raise NativeRecoveryError('STALE_NATIVE_TEST_REQUEST')

    def initialize(self, intent):
        if self.get('INTENT') is None:
            self.put('INTENT', intent=intent.to_dict())

    def resume_intent(self):
        initial=self.get('INTENT'); source=self.get('SOURCE:PASS')
        if initial is None or source is None:
            # A started SOURCE cannot be silently regenerated after a crash.
            if any(r['key'].startswith('SOURCE:') for r in self.records()):
                raise NativeRecoveryError('AMBIGUOUS_NATIVE_INVOCATION')
            return None
        test=self.get('TEST:PASS')
        self.guard((test or source)['versions'])
        if test is None:
            started=[r for r in self.records() if r['key'].startswith('TEST:') and r['key'].endswith(':STARTED')]
            if started:
                last=started[-1]; attempt=last['attempt']
                if attempt != 0 or (self.get('TEST:0:HTTP_SERVER_ERROR') is None
                                   and self.get('TEST:0:PROTOCOL_ERROR') is None):
                    raise NativeRecoveryError('AMBIGUOUS_NATIVE_INVOCATION')
        raw=initial['intent']; versions=(test or source)['versions']
        source_paths={v[0] for v in raw['source_versions']}
        return TypedEditIntentV1(raw['schema_version'],raw['run_id'],raw['todo_id'],
            raw['responsibility_fingerprint'],raw['objective'],
            tuple(tuple(v) for v in versions if v[0] in source_paths),
            tuple(tuple(v) for v in versions if v[0] not in source_paths),
            tuple(raw['relevant_facts']),tuple(raw['verification_obligations']))

    def stage_start(self, stage, paths):
        failed=self.get(stage+':FAILED')
        if not self.reopen_semantic_failure and failed and failed.get('result') is not None:
            self.guard(failed['versions'])
            return failed['result'],2
        completed=self.get(stage+':PASS')
        if completed:
            self.guard((self.get('TEST:PASS') or completed)['versions'])
            return completed['result'],2
        started=[r for r in self.records() if r['key'].startswith(stage+':') and r['key'].endswith(':STARTED')]
        if not started: return None,0
        last=started[-1]
        failure=(self.get(stage+':0:HTTP_SERVER_ERROR') or self.get(stage+':0:PROTOCOL_ERROR')
                 or ((self.get(stage+':0:VERIFICATION_FAILED') or self.get(stage+':0:PRE_CAS_REJECTED')) if not self.reopen_semantic_failure else None))
        if last['attempt']!=0 or failure is None:
            raise NativeRecoveryError('AMBIGUOUS_NATIVE_INVOCATION')
        self.guard(failure['versions'])
        return None,1
