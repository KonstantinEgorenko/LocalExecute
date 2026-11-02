"""Parent-owned lifecycle with explicit deadline; never writes child state."""
from dataclasses import dataclass
import hashlib
import math
import os
from pathlib import Path
import time
import uuid

from ..evaluation.store import FileEvaluationStore
from .receipts import owned_receipts, Receipts, fingerprint, utc, SupervisionConflict
from .windows import Windows
from .diagnostics import unavailable
from ..agent_loop.protocol import decode_strict_json


class StartDeadlineExceeded(TimeoutError):
    """The child never received permission to execute after its deadline."""


def positive(value, name):
    if type(value) not in (int,float) or not math.isfinite(value) or value <= 0:
        raise ValueError(name+' must be finite and positive')


@dataclass(frozen=True)
class Deadline:
    wall_start: float
    mono_start: float
    seconds: float

    def observe(self, wall, mono):
        wall_elapsed=wall-self.wall_start;mono_elapsed=mono-self.mono_start
        discrepancy=wall_elapsed-mono_elapsed
        # A backward clock jump fails closed; forward/sleep time cannot extend.
        anomaly=mono_elapsed<0 or wall_elapsed<0 or discrepancy < -2
        return {'wall_elapsed':wall_elapsed,'monotonic_elapsed':mono_elapsed,
            'clock_discrepancy':discrepancy,'clock_anomaly':abs(discrepancy)>2 or anomaly,
            'sleep_resume_evidence':'NOT_OBSERVED',
            'expired':anomaly or max(wall_elapsed,mono_elapsed)>=self.seconds}


def terminal(rows):
    values=[r for r in rows if r['payload'].get('terminal') is True]
    if len(values)>1:
        raise SupervisionConflict('multiple terminal receipts')
    return values[0] if values else None


def prepare(base, evaluation_id, manifest_fingerprint, command, deadline, grace, heartbeat, cwd, *, attempt_claim=None):
    for name,value in [('deadline',deadline),('grace',grace),('heartbeat',heartbeat)]:positive(value,name)
    if heartbeat>60 or heartbeat>deadline:
        raise ValueError('heartbeat must be <=60 seconds and <=deadline')
    if not evaluation_id or type(evaluation_id) is not str or len(evaluation_id.encode('utf-8'))>256:
        raise ValueError('evaluation identity required')
    if type(manifest_fingerprint) is not str or len(manifest_fingerprint)!=64 or any(c not in '0123456789abcdef' for c in manifest_fingerprint):
        raise ValueError('manifest fingerprint must be SHA256')
    if not command or any(type(x) is not str or '\0' in x for x in command):
        raise ValueError('exact command required')
    if sum(len(x.encode('utf-8')) for x in command)>65536:
        raise ValueError('command exceeds bounded manifest size')
    executable=Path(command[0]).resolve()
    if not Path(command[0]).is_absolute():
        raise ValueError('absolute executable required; no PATH lookup')
    command=[str(executable),*command[1:]]
    invocation=uuid.uuid4().hex
    root=Path(base).resolve()/invocation
    # Fresh reservation alone gives ownership. No collision reset/cleanup.
    identity={'schema_version':1,'evaluation_id':evaluation_id,'invocation_id':invocation,
        'manifest_fingerprint':manifest_fingerprint,'executable':str(executable),
        'executable_sha256':hashlib.sha256(executable.read_bytes()).hexdigest() if executable.is_file() else None,
        'command':command,'argv_fingerprint':fingerprint(command),
        'deadline_seconds':deadline,'grace_seconds':grace,'heartbeat_seconds':heartbeat,
        'cwd':str(Path(cwd).resolve()),'owner_id':uuid.uuid4().hex,
        'utc_created':utc(),'wall_start':time.time(),'monotonic_start':time.monotonic(),
        'parent':{'pid':os.getpid(),'creation_filetime':None},'retention':'RETAIN_ALL_CHILD_RESOURCES'}
    try:identity['parent']=Windows().parent_identity()
    except Exception:pass # Unknown parent creation is explicit; child backend still fails closed.
    if attempt_claim is not None:
        from .evaluation import bind_claim
        identity['attempt_authority']=bind_claim(attempt_claim,base,evaluation_id,
            manifest_fingerprint,command,invocation,identity['parent'],identity['owner_id'])
    root.mkdir(parents=True,exist_ok=False)
    with owned_receipts(root) as journal:
        journal.append('INVOCATION_PREPARED',identity,resources_created=[str(root)],worktrees_created=[])
    return root


def observation_failed(journal, identity, child, execution_state, exc, event):
    """Contain only the owned job; OS facts and durable facts stay separate.

    Reload before each recovery publication: a failed append may have reached
    disk. Never invent a terminal or overwrite a conflicting receipt chain.
    """
    gaps=[]
    def publish(name, **payload):
        try:
            fresh=Receipts(journal.root)
            existing=terminal(fresh.rows)
            if existing:
                return existing
            return fresh.append(name,identity,**payload)
        except Exception as failure:
            gaps.append({'event':name,'error_type':type(failure).__name__})
            return None
    detail={'execution_state':execution_state,'child':child.identity,
        'exception_type':type(exc).__name__,'failed_event':event,
        'mission_state':'NOT_OBSERVED','evaluation_outcome':'INTERRUPTED'}
    publish('STARTUP_OBSERVATION_FAILED' if event=='STARTUP' else 'LIFECYCLE_OBSERVATION_FAILED',
        terminal=False,**detail)
    publish('TERMINATION_REQUESTED',mechanism='OWNED_JOB',terminal=False,**detail)
    code=None
    try:
        code=child.poll()
        if code is None:
            child.terminate()
            bound=time.monotonic()+5
            while code is None and time.monotonic()<bound:
                time.sleep(.025)
                code=child.poll()
    except Exception as failure:
        gaps.append({'event':'OS_TERMINATION','error_type':type(failure).__name__})
    payload={**detail,'terminal':code is not None,'exit_code':code,
        'process_state':'INTERRUPTED' if code is not None else 'UNRESOLVED',
        'termination_confirmed':code is not None,'publication_gaps':gaps}
    # Deadline without a resume is an execution fact, not a death claim.
    if isinstance(exc,StartDeadlineExceeded) and execution_state=='SPAWNED_SUSPENDED':
        payload['process_state']='NOT_EXECUTED_DEADLINE'
    result=publish('TERMINATION_OBSERVED' if code is not None else 'RECONCILIATION_REQUIRED',**payload)
    if result is not None:
        return result
    # This return is explicitly NOT a receipt; reconcile using latest durable
    # CHILD_SPAWNED identity. No fingerprint/sequence or invented durable fact.
    return {'event':'RECONCILIATION_REQUIRED','identity':identity,
        'publication_status':'UNAVAILABLE','payload':{**payload,'terminal':False,
            'process_state':'UNRESOLVED','durable_termination':'NOT_PUBLISHED'}}


def heartbeat(root, journal, identity, child, observation):
    """Optional bounded snapshot; malformed/racing I/O is not child failure."""
    progress=None
    status='NOT_OBSERVED'
    try:
        path=Path(root)/'transport-progress.json'
        if path.is_file():
            with path.open('rb') as stream:
                content=stream.read(8193)
            if len(content)>8192:
                raise ValueError('progress exceeds bound')
            value=decode_strict_json(content.decode('utf-8'))
            if (type(value) is not dict or value.get('evaluation_invocation_id')!=identity['invocation_id']
                    or type(value.get('stage')) is not str or 'request_id' not in value):
                raise ValueError('invalid diagnostic progress snapshot')
            progress=value
            status='OBSERVED'
    except Exception as exc:
        status='UNAVAILABLE'
        unavailable(journal,identity,'transport-progress.json',exc)
    try:
        FileEvaluationStore._write(Path(root)/'heartbeat.json',{
            'invocation_id':identity['invocation_id'],'observer_utc':utc(),
            'latest_event_sequence':journal.rows[-1]['sequence'],'child':child.identity,
            'current_stage':progress['stage'] if progress else 'UNKNOWN',
            'last_durable_child_event':progress,
            'last_request_id':progress['request_id'] if progress else None,
            'transport_progress':status,'clock':observation})
    except Exception as exc:
        unavailable(journal,identity,'heartbeat.json',exc)


def supervise(root, *, backend=None):
    with owned_receipts(root) as journal:
        if not journal.rows or journal.rows[0]['event']!='INVOCATION_PREPARED':
            raise SupervisionConflict('missing preparation')
        if len(journal.rows)!=1:
            raise SupervisionConflict('invocation already started; reconcile instead')
        identity=journal.rows[0]['identity']
        clock=Deadline(identity['wall_start'],identity['monotonic_start'],identity['deadline_seconds'])
        child=None
        execution_state='NOT_SPAWNED'
        journal.append('SPAWN_ATTEMPTED',identity)
        try:
            observation=clock.observe(time.time(),time.monotonic())
            if observation['expired']:
                journal.append('DEADLINE_EXCEEDED',identity,clock=observation,cause='BEFORE_SPAWN')
                raise StartDeadlineExceeded('deadline expired before spawn')
            backend=backend or Windows()
            executable=Path(identity['executable'])
            if identity['executable_sha256'] is not None and (
                    not executable.is_file() or hashlib.sha256(executable.read_bytes()).hexdigest()!=identity['executable_sha256']):
                raise SupervisionConflict('executable identity changed before spawn')
            env=os.environ.copy()
            env['SWARM_SUPERVISION_ROOT']=str(Path(root).resolve())
            env['SWARM_SUPERVISION_INVOCATION']=identity['invocation_id']
            child=backend.spawn(identity['command'],identity['cwd'],env,identity['invocation_id'])
            execution_state='SPAWNED_SUSPENDED'
            ownership=journal.append('CHILD_SPAWNED',identity,child=child.identity,
                handle_ownership='OWNED',execution_state='SUSPENDED',
                observer=backend.parent_identity() if hasattr(backend,'parent_identity') else {'pid':os.getpid(),'creation_filetime':None})
            observation=clock.observe(time.time(),time.monotonic())
            if observation['expired']:
                journal.append('DEADLINE_EXCEEDED',identity,clock=observation,cause='BEFORE_RESUME')
                journal.append('TERMINATION_REQUESTED',identity,child=child.identity,
                    mechanism='OWNED_JOB_CLOSE;STILL_SUSPENDED',termination_confirmed=False)
                raise StartDeadlineExceeded('deadline expired before child resume')
            journal.append('RESUME_REQUESTED',identity,child=child.identity,
                execution_state='RESUME_REQUESTED')
            # Publication latency must not extend the execution permission.
            if clock.observe(time.time(),time.monotonic())['expired']:
                raise StartDeadlineExceeded('deadline expired during resume publication')
            execution_state='RESUME_OUTCOME_UNKNOWN'
            child.resume()
            execution_state='RESUME_CONFIRMED'
            journal.append('RESUME_CONFIRMED',identity,child=child.identity,
                execution_state=execution_state)
            journal.append('CHILD_RUNNING',identity,child=child.identity,
                ownership_receipt_fingerprint=ownership['fingerprint'])
        except Exception as exc:
            if child is not None:
                execution_state=getattr(child,'resume_state',execution_state)
                try:
                    return observation_failed(journal,identity,child,execution_state,exc,'STARTUP')
                finally:
                    child.close()
            return journal.append('SPAWN_FAILED',identity,terminal=True,
                process_state='NOT_EXECUTED_DEADLINE' if isinstance(exc,StartDeadlineExceeded) else 'SPAWN_FAILED',
                exception_type=type(exc).__name__,
                error=str(exc).encode()[:2048].decode('utf-8','ignore'),mission_state='NOT_OBSERVED')
        except BaseException as exc:
            if child is not None:
                try:
                    observation_failed(journal,identity,child,
                        getattr(child,'resume_state',execution_state),exc,'STARTUP')
                finally:
                    child.close()
            raise
        try:
            next_heartbeat=0
            last_discrepancy=None
            while True:
                code=child.poll()
                observation=clock.observe(time.time(),time.monotonic())
                if code is not None:
                    if observation['expired']:
                        journal.append('DEADLINE_EXCEEDED',identity,clock=observation,
                            cause='EXIT_OBSERVED_AFTER_DEADLINE;ACTUAL_EXIT_TIME_NOT_OBSERVED')
                    return journal.append('CHILD_EXIT_OBSERVED',identity,terminal=True,
                        process_state='EXITED',exit_code=code,child=child.identity,
                        evaluation_outcome='DEADLINE_INTERRUPTED' if observation['expired'] else ('PROCESS_EXIT_ZERO' if code==0 else 'PROCESS_EXIT_NONZERO'),
                        termination_actor='UNKNOWN',mission_state='NOT_OBSERVED',clock=observation)
                if observation['clock_anomaly'] and last_discrepancy is None:
                    journal.append('CLOCK_DISCONTINUITY',identity,clock=observation)
                    last_discrepancy=observation['clock_discrepancy']
                if observation['expired']:
                    journal.append('DEADLINE_EXCEEDED',identity,clock=observation)
                    # No generic child graceful-shutdown protocol is assumed.
                    journal.append('TERMINATION_REQUESTED',identity,child=child.identity,
                        mechanism='OWNED_JOB',graceful_shutdown='UNSUPPORTED',grace_seconds=identity['grace_seconds'])
                    grace_end=time.monotonic()+identity['grace_seconds']
                    while child.poll() is None and time.monotonic()<grace_end:time.sleep(.025)
                    try:
                        if child.poll() is None:child.terminate()
                    except Exception as exc:
                        return journal.append('RECONCILIATION_REQUIRED',identity,terminal=False,
                            process_state='UNRESOLVED',termination_confirmed=False,
                            error_type=type(exc).__name__,mission_state='NOT_OBSERVED')
                    bound=time.monotonic()+5
                    while child.poll() is None and time.monotonic()<bound:time.sleep(.025)
                    code=child.poll()
                    return journal.append('TERMINATION_OBSERVED' if code is not None else 'RECONCILIATION_REQUIRED',identity,
                        terminal=code is not None,process_state='INTERRUPTED' if code is not None else 'UNRESOLVED',
                        termination_confirmed=code is not None,exit_code=code,child=child.identity,
                        mission_state='NOT_OBSERVED',cause='DEADLINE')
                if time.monotonic()>=next_heartbeat:
                    heartbeat(root,journal,identity,child,observation)
                    next_heartbeat=time.monotonic()+identity['heartbeat_seconds']
                time.sleep(min(.1,identity['heartbeat_seconds']))
        except Exception as exc:
            return observation_failed(journal,identity,child,execution_state,exc,'MONITOR')
        except BaseException as exc:
            try:
                journal.append('OBSERVER_INTERRUPTED',identity,exception_type=type(exc).__name__,
                    process_state='RECONCILIATION_REQUIRED')
            except Exception as publication:
                exc.add_note('Observer interruption publication unavailable: '+type(publication).__name__)
            raise
        finally:
            # Kill-on-close is confined to this invocation's proven job.
            child.close()


def reconcile(root, *, backend=None):
    with owned_receipts(root) as journal:
        if not journal.rows:raise SupervisionConflict('missing invocation')
        existing=terminal(journal.rows)
        if existing:return existing
        identity=journal.rows[0]['identity']
        spawned=[r for r in journal.rows if r['event']=='CHILD_SPAWNED']
        if not spawned:
            observation={'state':'UNKNOWN','exit_code':None,'cause':'NO_DURABLE_CHILD_IDENTITY'}
        else:
            observation=(backend or Windows()).probe(spawned[-1]['payload']['child'])
        dead=observation['state'] in ('DEAD','PID_REUSED')
        payload={'observation':observation,'terminal':dead,
            'process_state':'INTERRUPTED' if dead else 'UNRESOLVED',
            'mission_state':'NOT_OBSERVED','inference_replay':False,'worktrees':'RETAINED'}
        event='RECONCILED' if dead else 'RECONCILIATION_REQUIRED'
        if journal.rows[-1]['event']==event and journal.rows[-1]['payload']==payload:
            return journal.rows[-1]
        return journal.append(event,identity,**payload)
