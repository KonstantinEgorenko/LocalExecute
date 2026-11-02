"""Stage-aware evaluation ownership. No Mission, scheduling or inference.

The prepared contract must be independently sealed by the operator. A stage
string is never authority: claims, receipt chain and native child ownership
must also agree. Claims survive crashes and are never reset or removed here.
"""
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time
import uuid

from ..agent_loop.protocol import decode_strict_json
from .receipts import Receipts, SupervisionConflict, fingerprint
from .windows import Windows


class EvaluationOwnershipError(SupervisionConflict):
    pass


class Stage(str, Enum):
    PRE_SPAWN = 'PRE_SPAWN'
    CHILD_STARTED = 'CHILD_STARTED'
    ACTIVE = 'ACTIVE'
    TERMINAL = 'TERMINAL'


def require(condition, code):
    if not condition:
        raise EvaluationOwnershipError(code)


def read(path):
    content = Path(path).read_bytes()
    require(len(content) <= 2097152, 'OWNERSHIP_DOCUMENT_TOO_LARGE')
    return decode_strict_json(content.decode('utf-8'))


def no_reparse(path):
    """Check physical ancestors without resolving away caller-visible links."""
    p = Path(path).absolute()
    for item in (p, *p.parents):
        if item.exists() or item.is_symlink():
            metadata = item.lstat()
            require(not stat.S_ISLNK(metadata.st_mode) and not (
                getattr(metadata, 'st_file_attributes', 0) & 0x400), 'REPARSE_PATH_ESCAPE')
    return p


def exclusive_json(path, value):
    no_reparse(Path(path).parent)
    try:
        with Path(path).open('x', encoding='utf-8', newline='\n') as stream:
            json.dump(value, stream, sort_keys=True, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError as exc:
        raise EvaluationOwnershipError('ATTEMPT_ALREADY_CLAIMED') from exc


def claim_attempt(contract, command, *, purpose):
    require(purpose in ('SYNTHETIC_REHEARSAL', 'AUTHORIZED_SINGLE_LIVE'), 'INVALID_CLAIM_PURPOSE')
    verify_stage(contract, Stage.PRE_SPAWN)
    claim = dict(schema_version=1, evaluation_id=contract['evaluation_id'],
                 manifest_fingerprint=contract['manifest_fingerprint'], source_identity=contract['source_identity'],
                 contract_fingerprint=fingerprint(contract), command_fingerprint=fingerprint(command),
                 supervision_root=str(Path(contract['supervision_root']).resolve()),
                 attempt_id=uuid.uuid4().hex, purpose=purpose)
    exclusive_json(Path(contract['physical_root']) / 'attempt-claim.json', claim)
    return claim


def bind_claim(path, base, evaluation_id, manifest, command, invocation, parent, owner_id):
    """Called by the supervisor exactly once, before it can spawn any child."""
    claim = read(no_reparse(path))
    require(claim['evaluation_id'] == evaluation_id and claim['manifest_fingerprint'] == manifest,
            'CLAIM_MANIFEST_MISMATCH')
    require(claim['command_fingerprint'] == fingerprint(command) and
            claim['supervision_root'] == str(Path(base).resolve()), 'CLAIM_COMMAND_OR_ROOT_MISMATCH')
    binding = dict(claim_fingerprint=fingerprint(claim), invocation_id=invocation, parent=parent, owner_id=owner_id)
    exclusive_json(Path(path).with_name('attempt-binding.json'), binding)
    return dict(claim_path=str(Path(path).absolute()), claim_fingerprint=fingerprint(claim),
                binding_fingerprint=fingerprint(binding))


def _names(root):
    require(root.is_dir(), 'OWNED_ROOT_MISSING')
    values = set()
    for child in root.iterdir():
        no_reparse(child)
        values.add(child.name)
    return values


def _owner(root, contract):
    require(read(root / 'owner.json') == contract['owner'], 'ROOT_OWNER_MISMATCH')


def _rows(invocation, stage, *, require_running=True):
    # Supervisor holds its observer lock while monitoring. Child readers do
    # not take that lock; bounded stable snapshots tolerate atomic publication.
    bound = time.monotonic() + 2
    while True:
        try:
            first = Receipts(invocation).rows
            receipt_root = invocation / 'receipts'
            require(_names(receipt_root) == {'index.json', 'objects'}, 'FOREIGN_RECEIPT_ARTIFACT')
            index = read(receipt_root / 'index.json')
            expected_objects = {v['sha256'] for v in index['artifacts']}
            require(_names(receipt_root / 'objects') == expected_objects, 'FOREIGN_RECEIPT_OBJECT')
            second = Receipts(invocation).rows
            require(first == second and first, 'RECEIPT_SNAPSHOT_CONFLICT')
            if require_running and stage != Stage.TERMINAL:
                require(any(r['event'] == 'CHILD_RUNNING' for r in first), 'CHILD_NOT_RESUMED')
            return first
        except (OSError, EvaluationOwnershipError):
            if time.monotonic() >= bound:
                raise
            time.sleep(.025)


def verify_stage(contract, stage, *, invocation_root=None, backend=None):
    try:
        return _verify_stage(contract, stage, invocation_root=invocation_root, backend=backend)
    except EvaluationOwnershipError:
        raise
    except Exception as exc:
        raise EvaluationOwnershipError('OWNERSHIP_VERIFICATION_FAILED:' + type(exc).__name__ + ':' + str(exc)) from exc


def _verify_stage(contract, stage, *, invocation_root=None, backend=None):
    stage = Stage(stage)
    physical = no_reparse(contract['physical_root'])
    evaluation = no_reparse(contract['evaluation_root'])
    supervision = no_reparse(contract['supervision_root'])
    require(evaluation.parent == supervision.parent == physical and evaluation != supervision, 'ROOT_AUTHORITY_MISMATCH')
    require(contract['owner']['evaluation_id'] == contract['evaluation_id'] and
            contract['owner']['source_identity'] == contract['source_identity'], 'SOURCE_SEAL_MISMATCH')
    for root in (physical, evaluation, supervision):
        _owner(root, contract)
    expected = {evaluation.name, supervision.name, 'owner.json'}
    if stage == Stage.PRE_SPAWN:
        require(_names(physical) == expected, 'REUSED_EVALUATION_OR_ATTEMPT')
        require(_names(evaluation) == _names(supervision) == {'owner.json'}, 'REUSED_OWNED_ROOT')
        return dict(status='PASS', stage=stage.value, attempt_claimed=False)
    require(_names(physical) == expected | {'attempt-claim.json', 'attempt-binding.json'}, 'FOREIGN_PHYSICAL_ARTIFACT')
    claim = read(physical / 'attempt-claim.json')
    binding = read(physical / 'attempt-binding.json')
    require(claim['contract_fingerprint'] == fingerprint(contract) and
            claim['evaluation_id'] == contract['evaluation_id'] and
            claim['manifest_fingerprint'] == contract['manifest_fingerprint'] and
            claim['source_identity'] == contract['source_identity'], 'ATTEMPT_IDENTITY_MISMATCH')
    require(binding['claim_fingerprint'] == fingerprint(claim), 'ATTEMPT_BINDING_MISMATCH')
    invocation = no_reparse(invocation_root or supervision / binding['invocation_id'])
    require(invocation.parent == supervision and invocation.name == binding['invocation_id'], 'WRONG_SUPERVISOR_ROOT')
    require(_names(supervision) == {'owner.json', invocation.name}, 'FOREIGN_SUPERVISOR_INVOCATION')
    allowed = {'receipts', 'observer.lock', 'heartbeat.json', 'transport-progress.json', 'transport'}
    names = _names(invocation)
    # Heartbeat is optional diagnostic evidence. Its transient atomic writer
    # file is not authority and is allowed only during a running stage.
    transient = {n for n in names if n.startswith(('.heartbeat.json.', '.transport-progress.json.')) and n.endswith('.tmp')}
    require(names <= allowed | (transient if stage != Stage.TERMINAL else set()), 'FOREIGN_INVOCATION_ARTIFACT')
    for child in invocation.rglob('*'):
        no_reparse(child)
    rows = _rows(invocation, stage)
    identity = rows[0]['identity']
    require(rows[0]['event'] == 'INVOCATION_PREPARED' and identity['evaluation_id'] == contract['evaluation_id'] and
            identity['manifest_fingerprint'] == contract['manifest_fingerprint'] and
            identity['invocation_id'] == binding['invocation_id'] and identity['parent'] == binding['parent'] and
            identity['owner_id'] == binding['owner_id'],
            'SUPERVISOR_IDENTITY_MISMATCH')
    authority = identity.get('attempt_authority', {})
    require(authority.get('claim_fingerprint') == fingerprint(claim) and
            authority.get('binding_fingerprint') == fingerprint(binding) and
            authority.get('claim_path') == str(physical / 'attempt-claim.json') and
            identity['argv_fingerprint'] == claim['command_fingerprint'], 'SUPERVISOR_CLAIM_MISMATCH')
    spawned = [r for r in rows if r['event'] == 'CHILD_SPAWNED']
    require(len(spawned) == 1, 'CHILD_IDENTITY_UNPROVEN')
    child = spawned[0]['payload']['child']
    require(child['ownership'] == 'SUSPENDED_ASSIGNED_BEFORE_RESUME' and child['job_association'] == 'OS_VERIFIED' and
            child['job_name'] == 'Local\\SwarmEval-' + binding['invocation_id'], 'CHILD_JOB_UNPROVEN')
    transport_root = invocation / 'transport'
    transport_ids = []
    if transport_root.exists():
        calls = _names(transport_root)
        require(len(calls) <= 1024, 'TRANSPORT_EVIDENCE_BOUND_EXCEEDED')
        for call_id in sorted(calls):
            require(re.fullmatch(r'[0-9a-f]{32}', call_id), 'FOREIGN_TRANSPORT_INVOCATION')
            call_root = transport_root / call_id
            require(_names(call_root) == {'receipts'}, 'FOREIGN_TRANSPORT_ARTIFACT')
            call_rows = _rows(call_root, stage, require_running=False)
            owner = call_rows[0]['identity']
            require(call_rows[0]['event'] == 'DISPATCH_INTENT_PERSISTED' and
                    owner['evaluation_id'] == contract['evaluation_id'] and
                    owner['evaluation_invocation_id'] == binding['invocation_id'] and
                    owner['transport_invocation_id'] == call_id and owner['process_pid'] == child['pid'],
                    'TRANSPORT_OWNER_MISMATCH')
            require(all(r['event'] in ('DISPATCH_INTENT_PERSISTED','TRANSPORT_CALL_ENTERED','RESPONSE_OBSERVED',
                                      'TRANSPORT_ERROR_OBSERVED','DIAGNOSTIC_OBSERVATION_UNAVAILABLE') for r in call_rows),
                    'FOREIGN_TRANSPORT_EVENT')
            require(sum(r['event'] in ('RESPONSE_OBSERVED','TRANSPORT_ERROR_OBSERVED') for r in call_rows) <= 1,
                    'TRANSPORT_TERMINAL_CONFLICT')
            transport_ids.append(call_id)
    terminals = [r for r in rows if r['payload'].get('terminal') is True]
    if stage == Stage.TERMINAL:
        require(len(terminals) == 1 and rows[-1] == terminals[0], 'TERMINAL_RECEIPT_UNPROVEN')
    else:
        require(not terminals, 'STALE_EXECUTION_STAGE')
        (backend or Windows()).verify_child(child, binding['parent'])
    evidence_names = {'owner.json'} if stage == Stage.CHILD_STARTED else {'owner.json', 'evidence'}
    require(_names(evaluation) <= evidence_names, 'FOREIGN_EVALUATION_ARTIFACT')
    if stage == Stage.ACTIVE:
        require('evidence' in _names(evaluation), 'EVIDENCE_NOT_CREATED')
    if (evaluation / 'evidence').exists():
        _owner(evaluation / 'evidence', contract)
        for path in (evaluation / 'evidence').rglob('*'):
            no_reparse(path)
        # The runner declares exact permitted relative artifacts in the sealed
        # contract. Unknown siblings never acquire ownership by naming alone.
        require(_names(evaluation / 'evidence') <= set(contract['evidence_entries']) | {'owner.json'},
                'FOREIGN_EVIDENCE_ARTIFACT')
    return dict(status='PASS', stage=stage.value, evaluation_id=contract['evaluation_id'],
                attempt_id=claim['attempt_id'], invocation_id=identity['invocation_id'],
                receipt_count=len(rows), latest_receipt=rows[-1]['fingerprint'], child=child, supervisor=identity,
                transport_invocation_ids=transport_ids)
