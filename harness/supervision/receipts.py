"""Existing create-only artifact semantics, serialized by an OS lock."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time

from ..agent_loop.protocol import decode_strict_json
from ..context_manager.models import Artifact, ArtifactKind, ArtifactOrigin
from ..context_manager.store import FileArtifactStore
from ..context_manager.errors import ArtifactStoreCorruptionError
from ..runtime_resources.gpu_coordination.lease import _OsFileLock


class SupervisionConflict(RuntimeError):
    pass


class SupervisionArtifactStore(FileArtifactStore):
    def _atomic_write_json(self, path, payload):
        # Windows readers may briefly deny delete/replace sharing. Retry the
        # same atomic publication only; never alter identity or partial content.
        deadline=time.monotonic()+1
        while True:
            try:
                return super()._atomic_write_json(path,payload)
            except OSError as exc:
                if getattr(exc,'winerror',None) not in (5,32,33) or time.monotonic()>=deadline:
                    raise
                time.sleep(.01)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def utc():
    return datetime.now(timezone.utc).isoformat()


class Receipts:
    """Use only within the owning OS lock, including reload and append."""
    def __init__(self, root):
        self.root = Path(root)
        bound = time.monotonic() + .25
        while True:
            try:
                self.store = SupervisionArtifactStore(self.root / 'receipts')
                self.rows = [decode_strict_json(self.store.get_artifact(ref.artifact_id).content)
                             for ref in self.store.list_refs()]
                break
            except ArtifactStoreCorruptionError as exc:
                # Atomic Windows replacement can briefly deny read sharing.
                # Retry only that IO cause, never malformed bytes/hash/chain.
                if os.name != 'nt' or not isinstance(exc.__cause__, PermissionError) or time.monotonic() >= bound:
                    raise
                time.sleep(.01)
        self.rows.sort(key=lambda r: r['sequence'])
        previous = None
        identity = None
        for i, row in enumerate(self.rows):
            body = {k:v for k,v in row.items() if k != 'fingerprint'}
            if (type(row.get('sequence')) is not int or row['sequence'] != i
                    or type(row.get('schema_version')) is not int or row.get('schema_version') != 1
                    or row.get('previous') != previous or fingerprint(body) != row.get('fingerprint')):
                raise SupervisionConflict('receipt chain conflict')
            if i == 0:
                identity = row['identity']
            if row['identity'] != identity:
                raise SupervisionConflict('invocation identity conflict')
            previous = row['fingerprint']

    def append(self, event, identity, *, source='EXTERNAL_OBSERVER', **payload):
        if self.rows and identity != self.rows[0]['identity']:
            raise SupervisionConflict('invocation identity conflict')
        row = {'schema_version':1, 'sequence':len(self.rows), 'event':event,
            'identity':identity, 'utc':utc(), 'source':source,
            'previous':self.rows[-1]['fingerprint'] if self.rows else None,
            'payload':payload}
        row['fingerprint'] = fingerprint(row)
        self.store.create_artifact(Artifact.from_text(
            artifact_id=f"event:{row['sequence']:08d}", kind=ArtifactKind.OTHER,
            origin=ArtifactOrigin.CONTROL_PLANE, content=json.dumps(row,sort_keys=True,allow_nan=False),
            media_type='application/json', source='EvaluationSupervisionV1',created_at=row['utc']))
        self.rows.append(row)
        return row


@contextmanager
def owned_receipts(root):
    lock = _OsFileLock(Path(root)/'observer.lock', poll_interval_seconds=.05)
    lock.acquire(0)
    try:
        yield Receipts(root)
    finally:
        lock.release()
