"""Opt-in local native-chat observations, never server/GPU acceptance claims."""
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import os
from pathlib import Path
import time
import uuid

from .receipts import Receipts, fingerprint
from ..evaluation.store import FileEvaluationStore
from .diagnostics import unavailable
from .errors import SupervisionObservationHalt

_attempt = ContextVar('supervised_protocol_attempt',default=None)


@contextmanager
def protocol_attempt(**identity):
    token=_attempt.set(identity)
    try:yield
    finally:_attempt.reset(token)


class TransportObservation:
    def __init__(self, journal, identity):
        self.journal,self.identity=journal,identity
        self.started=time.monotonic()

    def progress(self, row):
        attempt=self.identity['protocol_attempt'] or {}
        try:
            FileEvaluationStore._write(self.journal.root.parent.parent/'transport-progress.json',{
                'evaluation_invocation_id':self.identity['evaluation_invocation_id'],
                'transport_invocation_id':self.identity['transport_invocation_id'],
                'request_id':attempt.get('request_id'),'stage':attempt.get('role','UNKNOWN'),
                'event':row['event'],'sequence':row['sequence'],'utc':row['utc'],
                'receipt_fingerprint':row['fingerprint']})
        except Exception as exc:
            self.progress_gap=unavailable(self.journal,self.identity,'transport-progress.json',exc)

    def entered(self):
        row=self.journal.append('TRANSPORT_CALL_ENTERED',self.identity,
            source='NATIVE_URLLIB_CLIENT',server_acceptance='NOT_OBSERVED',gpu_execution='NOT_OBSERVED')
        self.progress(row)

    def response(self, raw, value):
        metadata={key:value[key] for key in ('prompt_eval_count','eval_count','prompt_eval_duration','eval_duration')
            if type(value.get(key)) is int and value[key]>=0}
        row=self.journal.append('RESPONSE_OBSERVED',self.identity,
            source='NATIVE_URLLIB_CLIENT',
            response_fingerprint=hashlib.sha256(raw).hexdigest(),response_bytes=len(raw),
            returned_metadata=metadata,elapsed_seconds=time.monotonic()-self.started,
            completion_scope='CLIENT_RESPONSE',server_acceptance='NOT_INDEPENDENTLY_OBSERVED',
            gpu_execution='NOT_OBSERVED',terminal=True)
        self.progress(row)

    def error(self, exc):
        from ..transport_diagnostics import error_projection
        row=self.journal.append('TRANSPORT_ERROR_OBSERVED',self.identity,
            source='NATIVE_URLLIB_CLIENT',
            exception_type=type(exc).__name__,elapsed_seconds=time.monotonic()-self.started,
            server_acceptance='UNKNOWN',gpu_execution='NOT_OBSERVED',terminal=True,
            **error_projection(exc))
        self.progress(row)


def begin(base_url, path, payload):
    root=os.environ.get('SWARM_SUPERVISION_ROOT')
    if not root or path!='/api/chat' or not payload or not payload.get('messages'):
        return None # Discovery, preload and unload are not generation calls.
    parent=Receipts(root)
    if not parent.rows:raise RuntimeError('supervised transport missing invocation owner')
    owner=parent.rows[0]['identity']
    if owner['invocation_id']!=os.environ.get('SWARM_SUPERVISION_INVOCATION'):
        raise RuntimeError('supervised transport owner mismatch')
    call_id=uuid.uuid4().hex
    directory=Path(root)/'transport'/call_id
    directory.mkdir(parents=True,exist_ok=False)
    identity={'schema_version':1,'evaluation_id':owner['evaluation_id'],
        'evaluation_invocation_id':owner['invocation_id'],'transport_invocation_id':call_id,
        'protocol_attempt':_attempt.get(),
        'correlation_status':'EXPLICIT_PROTOCOL_OWNER' if _attempt.get() else 'ROLE_RUN_TODO_NOT_OBSERVED',
        'request_fingerprint':fingerprint({'endpoint':base_url,'path':path,'payload':payload}),
        'model_tag':payload.get('model'),'process_pid':os.getpid(),
        'accounting':'ONE_LOCAL_TRANSPORT_CALL;NOT_ADDITIONAL_TO_PROTOCOL_OR_MANIFEST'}
    journal=Receipts(directory)
    try:
        journal.append('DISPATCH_INTENT_PERSISTED',identity,source='PROTOCOL_CLIENT',network_dispatch='NOT_OBSERVED')
    except Exception as exc:
        raise SupervisionObservationHalt('DISPATCH_INTENT',exc,identity=identity) from exc
    return TransportObservation(journal,identity)


def summarize(root):
    """Unique local IDs, not a sum of lifecycle/protocol/manifest records."""
    results=[]
    for path in sorted((Path(root)/'transport').glob('*')):
        journal=Receipts(path)
        if not journal.rows:continue
        identity=journal.rows[0]['identity']
        terminal=[r for r in journal.rows if r['event'] in ('RESPONSE_OBSERVED','TRANSPORT_ERROR_OBSERVED')]
        if len(terminal)>1:raise RuntimeError('conflicting transport terminals')
        results.append({'transport_invocation_id':identity['transport_invocation_id'],
            'protocol_attempt':identity['protocol_attempt'],'status':terminal[0]['event'] if terminal else 'NO_TERMINAL_OBSERVATION',
            'entered':any(r['event']=='TRANSPORT_CALL_ENTERED' for r in journal.rows)})
    if len({r['transport_invocation_id'] for r in results})!=len(results):
        raise RuntimeError('duplicate transport invocation identity')
    return {'local_calls_entered':sum(r['entered'] for r in results),
        'responses_observed':sum(r['status']=='RESPONSE_OBSERVED' for r in results),
        'calls':results,'server_gpu_acceptance':'NOT_PROVEN'}
