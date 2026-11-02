"""Read-only evaluation reference authority checks, not a Git ref allowlist.

An audit is scoped to the sealed consumer closure and exact revision inputs.
Tree type alone never establishes authority. Unchanged inputs and the audited
consumer closure are prerequisites even when no refs changed.
"""
from dataclasses import dataclass
import hashlib
import re
import subprocess
from pathlib import Path

from .receipts import fingerprint

VERSION = 'REF_INTEGRITY_V3'
UUID = r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
PATTERNS = (
    rf'refs/codex/turn-diffs/captures/[0-9]{{13}}/{UUID}/base',
    rf'refs/codex/turn-diffs/checkpoints/[0-9a-f]{{32}}/[0-9a-f]{{32}}/[0-9]{{13}}/{UUID}',
)
INPUTS = ('source_identity', 'source_manifest', 'dirty_diff', 'untracked_source',
          'fixture', 'hidden', 'worktree_authority', 'consumer_audit')
OID = r'(?:[0-9a-f]{40}|[0-9a-f]{64})'


class InvalidSnapshot(ValueError):
    pass


def diagnostic(name):
    return any(re.fullmatch(pattern, name) for pattern in PATTERNS)


def parse_refs(raw):
    if type(raw) is not str or len(raw.encode('utf-8')) > 2097152:
        raise InvalidSnapshot('REF_SNAPSHOT_CONFLICT')
    rows = {}
    for line in raw.splitlines():
        match = re.fullmatch(OID + r' (refs/[^\s]+)', line)
        if not match:
            raise InvalidSnapshot('REF_SNAPSHOT_CONFLICT')
        oid, name = line.split(' ', 1)
        if (name in rows or any(x in name for x in ('..', '@{', '//', '~', '^', ':', '?', '*', '[', '\\'))
                or any(part.startswith('.') or part.endswith('.lock') for part in name.split('/'))
                or name.endswith(('/', '.')) or any(ord(x) < 32 or ord(x) == 127 for x in name)):
            raise InvalidSnapshot('REF_SNAPSHOT_CONFLICT')
        rows[name] = oid
    if len(rows) > 10000:
        raise InvalidSnapshot('REF_SNAPSHOT_CONFLICT')
    return tuple(sorted(rows.items()))


@dataclass(frozen=True)
class Snapshot:
    head: str
    branch: str
    refs: tuple[tuple[str, str], ...]
    inputs: tuple[tuple[str, str], ...]

    def document(self):
        return dict(head=self.head, branch=self.branch, refs=list(self.refs), inputs=dict(self.inputs))


def snapshot(head, branch, raw, inputs):
    if type(head) is not str or not re.fullmatch(OID, head) or type(branch) is not str or not branch:
        raise InvalidSnapshot('REF_SNAPSHOT_CONFLICT')
    if set(inputs) != set(INPUTS) or any(type(v) is not str or not v for v in inputs.values()):
        raise InvalidSnapshot('SOURCE_IDENTITY_DRIFT')
    refs = parse_refs(raw)
    if branch != 'DETACHED' and dict(refs).get('refs/heads/' + branch) != head:
        raise InvalidSnapshot('REF_SNAPSHOT_CONFLICT')
    return Snapshot(head, branch, refs, tuple(sorted(inputs.items())))


def audit_valid(policy, inputs):
    audit = policy.get('authority_audit', {})
    if (policy.get('version') != VERSION or policy.get('patterns') != list(PATTERNS)
            or type(policy.get('max_changed_refs')) is not int or not 0 < policy['max_changed_refs'] <= 64
            or fingerprint(audit) != inputs.get('consumer_audit')
            or audit.get('diagnostic_role') != 'NON_EXECUTABLE_TURN_DIFF_STORAGE'
            or audit.get('execution_revision_mode') != 'EXACT_HEAD_AND_DURABLE_COMMIT_OIDS'
            or not audit.get('consumer_files') or not audit.get('execution_inputs')):
        return False
    # The caller must supply an audited complete closure; verify every pinned
    # consumer byte on every observation. The closure itself is source sealed.
    for path, digest in audit['consumer_files'].items():
        p = Path(path)
        if not p.is_file() or hashlib.sha256(p.read_bytes()).hexdigest() != digest:
            return False
    return True


def compare(baseline, current, policy, object_type):
    try:
        return _compare(baseline, current, policy, object_type)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, subprocess.CalledProcessError) as exc:
        return dict(status='FAIL', disposition='REF_SNAPSHOT_CONFLICT',
                    error=type(exc).__name__ + ':' + str(exc), observations=[])


def _compare(baseline, current, policy, object_type):
    result = dict(policy_version=VERSION, policy_fingerprint=fingerprint(policy),
                  status='FAIL', disposition='REF_SNAPSHOT_CONFLICT', observations=[])
    try:
        for s in (baseline, current):
            if snapshot(s.head, s.branch, '\n'.join(o + ' ' + n for n, o in s.refs), dict(s.inputs)) != s:
                return result
    except (InvalidSnapshot, AttributeError, TypeError, ValueError):
        return result
    result['before'] = baseline.document()
    result['after'] = current.document()
    if baseline.inputs != current.inputs:
        result['disposition'] = 'SOURCE_IDENTITY_DRIFT'
        return result
    old, new = dict(baseline.refs), dict(current.refs)
    changes = [dict(name=n, old_oid=old.get(n), new_oid=new.get(n),
                    change='ADDED' if n not in old else 'DELETED' if n not in new else 'UPDATED',
                    namespace_valid=bool(diagnostic(n)))
               for n in sorted(old.keys() | new.keys()) if old.get(n) != new.get(n)]
    result.update(observations=changes[:65], changed_ref_count=len(changes), evidence_truncated=len(changes) > 65)
    if baseline.head != current.head or baseline.branch != current.branch:
        result['disposition'] = 'PROTECTED_REF_DRIFT'
        return result
    authority = set(policy.get('authority_refs', []))
    audit = policy.get('authority_audit', {})
    authority.update(x['ref'] for x in audit.get('execution_inputs', []) if x.get('ref'))
    if any(not c['namespace_valid'] or c['name'] in authority for c in changes):
        result['disposition'] = 'PROTECTED_REF_DRIFT'
        return result
    if not audit_valid(policy, dict(current.inputs)):
        result['disposition'] = 'UNPROVEN_DIAGNOSTIC_DRIFT'
        return result
    if len(changes) > policy['max_changed_refs']:
        result['disposition'] = 'UNPROVEN_DIAGNOSTIC_DRIFT'
        return result
    for row in changes:
        objects = []
        for oid in (row['old_oid'], row['new_oid']):
            if oid:
                kind = object_type(oid)
                objects.append(dict(oid=oid, object_type=kind, available=kind is not None))
        row.update(objects=objects, authority_audit='PINNED_CONSUMER_CLOSURE;NO_REF_CONSUMER',
                   rationale='Exact diagnostic name add/delete only; source and every execution input unchanged')
        if row['change'] == 'UPDATED' or not objects or any(x['object_type'] != 'tree' for x in objects):
            result['disposition'] = 'UNPROVEN_DIAGNOSTIC_DRIFT'
            return result
    result.update(status='PASS', disposition='APPROVED_NONAUTHORITATIVE_DRIFT' if changes else 'UNCHANGED')
    return result


def capture(root, inputs):
    def git(*args):
        return subprocess.check_output(['git', *args], cwd=root, stderr=subprocess.DEVNULL, text=True).strip()
    head = git('rev-parse', 'HEAD')
    branch = git('rev-parse', '--abbrev-ref', 'HEAD')
    branch = 'DETACHED' if branch == 'HEAD' else branch
    raw = git('show-ref')
    if git('rev-parse', 'HEAD') != head:
        raise InvalidSnapshot('REF_SNAPSHOT_CONFLICT')
    return snapshot(head, branch, raw, inputs() if callable(inputs) else inputs)


def git_object_type(root, oid):
    if not re.fullmatch(OID, oid):
        return None
    try:
        return subprocess.check_output(['git', 'cat-file', '-t', oid], cwd=root, stderr=subprocess.DEVNULL, text=True).strip()
    except subprocess.CalledProcessError:
        return None


def bounded_check(root, baseline, policy, inputs, observer=capture):
    """Exactly two observations; no retry/reseal and no mutation of Git."""
    reports, snapshots = [], []
    pinned = fingerprint(policy), fingerprint(baseline.document())
    try:
        for _ in range(2):
            current = observer(root, inputs)
            snapshots.append(current)
            reports.append(compare(baseline, current, policy, lambda oid: git_object_type(root, oid)))
        reports.append(compare(snapshots[0], snapshots[1], policy, lambda oid: git_object_type(root, oid)))
        if pinned != (fingerprint(policy), fingerprint(baseline.document())):
            raise InvalidSnapshot('REF_SNAPSHOT_CONFLICT: policy/baseline mutated')
    except (InvalidSnapshot, OSError, subprocess.CalledProcessError) as exc:
        return dict(status='FAIL', disposition='REF_SNAPSHOT_CONFLICT', error=str(exc), reports=reports,
                    snapshots=[s.document() for s in snapshots])
    failure = next((r['disposition'] for r in reports if r['status'] != 'PASS'), None)
    return dict(status='FAIL' if failure else 'PASS', disposition=failure or (
        'APPROVED_NONAUTHORITATIVE_DRIFT' if any(r['disposition'] != 'UNCHANGED' for r in reports) else 'UNCHANGED'),
        reports=reports, snapshots=[s.document() for s in snapshots])
