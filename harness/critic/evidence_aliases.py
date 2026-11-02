"""Invocation-local evidence names; canonical provenance remains authoritative."""
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from ..agent_loop.protocol import decode_strict_json
from ..context_manager.integration import parse_artifact_uri
from ..context_manager.models import Artifact, ArtifactKind, ArtifactOrigin
from .models import canonical_json
from .errors import CriticConformanceError
from .protocol import CRITIC_RESPONSE_SCHEMA

ENCODING_CONSTRAINT = 'CRITIC_EVIDENCE_ENCODING'
ALIAS_ENCODING = 'LOCAL_ALIASES_V1'
GENERATION_LIMIT_CONSTRAINT = 'CRITIC_GENERATION_LIMIT'
_TYPES = ('WORKER_RESULT', 'VERIFICATION_REPORT', 'CANDIDATE_DIFF', 'TOOL_RESULT')

def alias_mode(bundle):
    value = bundle.work_contract.get('constraints', {}).get(ENCODING_CONSTRAINT)
    if value is None:
        return False
    if value != ALIAS_ENCODING:
        raise CriticConformanceError('unsupported explicit Critic evidence encoding')
    limit = bundle.work_contract['constraints'].get(GENERATION_LIMIT_CONSTRAINT)
    if type(limit) is not int or limit != 1:
        raise CriticConformanceError('alias contract requires exact CRITIC_GENERATION_LIMIT=1')
    return True

def _hash(text): return hashlib.sha256(text.encode('utf-8')).hexdigest()
def _require(value, message):
    if not value: raise CriticConformanceError(message)

@dataclass(frozen=True, slots=True)
class CriticAliasManifest:
    """Immutable canonical JSON; no mutable nested state escapes."""
    canonical: str

    def __post_init__(self):
        try:
            doc = decode_strict_json(self.canonical)
            _require(type(doc) is dict and type(doc.get('schema_version')) is int and doc['schema_version'] == 1,
                     'invalid alias manifest version')
            _require(doc['encoding'] == ALIAS_ENCODING and type(doc['generation_limit']) is int and doc['generation_limit'] == 1,
                     'invalid alias manifest contract')
            rows = doc['entries']
            _require(type(rows) is list and 0 < len(rows) <= 32, 'invalid alias manifest entries')
            _require(all(set(row) == {'alias','canonical_evidence_id','evidence_type','content_sha256','artifact_id'}
                         and row['alias'] == 'E' + str(i + 1) and row['evidence_type'] in _TYPES
                         and type(row['canonical_evidence_id']) is str and row['canonical_evidence_id']
                         and type(row['content_sha256']) is str and len(row['content_sha256']) == 64
                         for i,row in enumerate(rows)), 'invalid/conflicting alias entry')
            _require(len({row['canonical_evidence_id'] for row in rows}) == len(rows), 'duplicate canonical alias target')
            _require(doc['evidence_manifest_fingerprint'] == _hash(canonical_json({k:v for k,v in doc.items() if k != 'evidence_manifest_fingerprint'})),
                     'alias manifest fingerprint mismatch')
        except (KeyError, TypeError, ValueError) as exc:
            raise CriticConformanceError('malformed alias manifest') from exc

    def to_dict(self): return decode_strict_json(self.canonical)
    @property
    def fingerprint(self): return self.to_dict()['evidence_manifest_fingerprint']
    @property
    def aliases(self): return tuple(row['alias'] for row in self.to_dict()['entries'])

def expected_manifest(bundle, lease_id, *, manager, run_store, artifact_store):
    """Re-prove ownership and content from current authoritative stores."""
    _require(alias_mode(bundle), 'alias mode was not explicitly enabled')
    lease = manager.get_active(lease_id)
    snapshot = manager.snapshot(lease_id)
    report = bundle.verification_report
    _require(lease.run_id == bundle.run_id and lease.todo_id == bundle.todo_id, 'alias lease Run/Todo mismatch')
    _require(report['lease_id'] == lease_id and report['report_id'] == bundle.verification_report_id
             and report['run_id'] == bundle.run_id and report['todo_id'] == bundle.todo_id,
             'alias verification report identity mismatch')
    _require(snapshot.head_commit == bundle.candidate_head and snapshot.diff_sha256 == bundle.candidate_diff_sha256,
             'alias candidate snapshot is stale')
    evidence = {}
    for event in run_store.list_events(bundle.run_id):
        if event.event_type.value != 'EVIDENCE_RECORDED': continue
        row = event.to_dict()['payload']; key = row.get('evidence_id')
        if key in evidence: _require(evidence[key] == row, 'conflicting canonical evidence records')
        evidence[key] = row
    entries = []
    items = sorted(bundle.evidence_items, key=lambda x: (_TYPES.index(x.evidence_type) if x.evidence_type in _TYPES else 99, x.evidence_id))
    _require(0 < len(items) <= 32, 'alias evidence count outside bounds')
    for item in items:
        _require(item.evidence_type in _TYPES, 'unauthorized alias evidence type')
        artifact_id = None
        if item.evidence_type == 'CANDIDATE_DIFF':
            _require(item.evidence_id == 'candidate_diff', 'candidate diff canonical identity mismatch')
            content = snapshot.diff_text
        else:
            row = evidence.get(item.evidence_id)
            _require(row is not None and row['todo_id'] == bundle.todo_id and row['evidence_type'] == item.evidence_type,
                     'missing or cross-Todo alias evidence')
            _require(row.get('reference') == item.reference, 'alias reference identity mismatch')
            artifact_id = parse_artifact_uri(item.reference)
            if item.evidence_type in ('WORKER_RESULT', 'VERIFICATION_REPORT'):
                _require(artifact_id is not None, 'missing referenced alias artifact')
            artifact = None
            if artifact_id is not None:
                _require(artifact_store.has_artifact(artifact_id), 'missing referenced alias artifact')
                artifact = artifact_store.get_artifact(artifact_id)
            if item.evidence_type == 'VERIFICATION_REPORT':
                _require(canonical_json(decode_strict_json(artifact.content)) == canonical_json(dict(report)), 'alias report content mismatch')
            if item.evidence_type == 'WORKER_RESULT':
                _require(artifact.kind is ArtifactKind.WORKER_RESULT, 'alias worker artifact type mismatch')
            content = canonical_json({'ledger_evidence': row, 'artifact_content': None if artifact is None else artifact.content,
                                      'artifact_sha256': None if artifact is None else artifact.sha256})
        entries.append({'alias': 'E' + str(len(entries) + 1), 'canonical_evidence_id': item.evidence_id,
                        'evidence_type': item.evidence_type, 'content_sha256': _hash(content), 'artifact_id': artifact_id})
    _require(len({e['canonical_evidence_id'] for e in entries}) == len(entries), 'duplicate canonical alias target')
    doc = {'schema_version': 1, 'encoding': ALIAS_ENCODING, 'request_id': bundle.request_id,
           'request_fingerprint': bundle.request_fingerprint, 'run_id': bundle.run_id, 'todo_id': bundle.todo_id,
           'lease_id': lease_id, 'candidate_head': bundle.candidate_head, 'candidate_diff_sha256': bundle.candidate_diff_sha256,
           'verification_report_id': bundle.verification_report_id, 'verification_report_sha256': _hash(canonical_json(dict(report))),
           'generation_limit': 1, 'entries': entries}
    doc['evidence_manifest_fingerprint'] = _hash(canonical_json(doc))
    return CriticAliasManifest(canonical_json(doc))

def _artifact(artifact_store, artifact_id, document):
    content = canonical_json(document)
    if artifact_store.has_artifact(artifact_id):
        existing = artifact_store.get_artifact(artifact_id)
        _require(existing.source == 'CriticEvidenceAliasesV1' and existing.kind is ArtifactKind.CONTROL_PLANE_EVIDENCE
                 and existing.content == content, 'conflicting or corrupt alias artifact')
    else:
        artifact_store.create_artifact(Artifact.from_text(artifact_id, ArtifactKind.CONTROL_PLANE_EVIDENCE,
            ArtifactOrigin.CONTROL_PLANE, content, 'application/json', 'CriticEvidenceAliasesV1',
            datetime.now(timezone.utc).isoformat()))

def manifest_id(bundle): return bundle.request_id + ':alias-manifest:0'
def prepare_manifest(bundle, lease_id, **stores):
    manifest = expected_manifest(bundle, lease_id, **stores)
    _verify_durable_evidence(bundle, lease_id, manifest, stores)
    _artifact(stores['artifact_store'], manifest_id(bundle), manifest.to_dict())
    return manifest

def _verify_durable_evidence(bundle, lease_id, expected, stores):
    # FileArtifactStore deliberately caches validated objects. Re-open the
    # existing durable store at this boundary; do not let a valid cache conceal
    # missing/corrupted disk evidence or repair the index by writing through it.
    from ..context_manager.store import FileArtifactStore
    if isinstance(stores['artifact_store'], FileArtifactStore):
        disk = FileArtifactStore(stores['artifact_store'].root)
        current = expected_manifest(bundle, lease_id, **{**stores,'artifact_store':disk})
        _require(current.canonical == expected.canonical, 'cached and durable alias evidence differ')
        return disk
    return stores['artifact_store']

def verify_manifest(bundle, lease_id, **stores):
    expected = expected_manifest(bundle, lease_id, **stores)
    store = stores['artifact_store']; key = manifest_id(bundle)
    _require(store.has_artifact(key), 'missing alias manifest')
    artifact = store.get_artifact(key)
    try: value = decode_strict_json(artifact.content)
    except (TypeError, ValueError) as exc: raise CriticConformanceError('malformed alias manifest') from exc
    _require(artifact.source == 'CriticEvidenceAliasesV1' and artifact.kind is ArtifactKind.CONTROL_PLANE_EVIDENCE,
             'alias manifest artifact authority mismatch')
    _require(canonical_json(value) == expected.canonical, 'alias manifest content/identity/fingerprint mismatch')
    disk = _verify_durable_evidence(bundle, lease_id, expected, stores)
    _require(disk.has_artifact(key), 'missing durable alias manifest')
    durable = disk.get_artifact(key)
    _require(durable.content == artifact.content and durable.sha256 == artifact.sha256
             and durable.source == artifact.source, 'cached and durable alias manifest differ')
    return expected

def resolve_alias_response(content, manifest):
    try: payload = decode_strict_json(content)
    except (TypeError, ValueError) as exc: raise CriticConformanceError('invalid alias response JSON') from exc
    _require(type(payload) is dict and set(payload) == set(CRITIC_RESPONSE_SCHEMA['required']), 'alias response keys must match Critic schema')
    refs = payload.get('evidence')
    _require(type(refs) is list and 0 < len(refs) <= 32 and all(type(x) is str for x in refs), 'invalid alias evidence list')
    _require(len(set(refs)) == len(refs), 'duplicate alias reference')
    mapping = {e['alias']: e['canonical_evidence_id'] for e in manifest.to_dict()['entries']}
    _require(all(ref in mapping for ref in refs), 'unknown evidence alias; canonical IDs are not accepted in alias mode')
    return canonical_json({**payload, 'evidence': [mapping[x] for x in refs]})

def record_resolution(bundle, *, artifact_store, manifest, raw_content, resolved_content, error):
    _artifact(artifact_store, bundle.request_id + ':alias-resolution:0',
        {'request_id': bundle.request_id, 'manifest_fingerprint': manifest.fingerprint,
         'raw_response_sha256': _hash(raw_content), 'raw_turn_artifact_id': bundle.request_id + ':turn:0',
         'resolved_response': resolved_content, 'parser_result': 'PASS' if error is None else 'INVALID',
         'first_causal_error': error})

def dispatch_id(bundle): return bundle.request_id + ':alias-dispatch:0'

def claim_dispatch(bundle, manifest, artifact_store):
    _require(not artifact_store.has_artifact(dispatch_id(bundle)), 'single Critic generation already reserved; no replay')
    _artifact(artifact_store, dispatch_id(bundle), {'request_id':bundle.request_id,
        'manifest_fingerprint':manifest.fingerprint, 'generation_limit':1})

def verify_dispatch(bundle, manifest, artifact_store):
    _require(artifact_store.has_artifact(dispatch_id(bundle)), 'missing Critic dispatch ownership receipt')
    artifact=artifact_store.get_artifact(dispatch_id(bundle))
    _require(artifact.source=='CriticEvidenceAliasesV1' and artifact.content==canonical_json(
        {'request_id':bundle.request_id,'manifest_fingerprint':manifest.fingerprint,'generation_limit':1}),
        'Critic dispatch receipt mismatch')
