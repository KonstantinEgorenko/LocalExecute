"""Source/ref authority shared by pre-spawn and child-stage verification."""
import hashlib
import subprocess
from pathlib import Path

from .evaluation import Stage, read, require, verify_stage
from .receipts import fingerprint
from .ref_integrity import snapshot, bounded_check


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def authority_inputs(prepared):
    root = Path(prepared['repository'])
    manifest = read(prepared['source_manifest'])
    for p, digest in manifest['files'].items():
        require(Path(p).is_file() and sha(p) == digest, 'SOURCE_BYTE_DRIFT:' + p)
    # New source files are just as authoritative as changed existing bytes.
    inventory = sorted(str(p.resolve()) for top in manifest['inventory_roots']
                       for p in (root / top).rglob('*') if p.is_file()
                       and '__pycache__' not in p.parts and p.suffix != '.pyc')
    require(inventory == manifest['project_paths'], 'SOURCE_INVENTORY_DRIFT')
    require(fingerprint(manifest['files']) == prepared['runtime_source_identity'], 'SOURCE_IDENTITY_DRIFT')
    def git(*args):
        return subprocess.check_output(['git', *args], cwd=root, stderr=subprocess.DEVNULL)
    untracked = git('ls-files', '--others', '--exclude-standard', '-z').split(b'\0')
    paths = sorted(p.decode('utf-8') for p in untracked if p.startswith(tuple(
        (top + '/').encode() for top in manifest['inventory_roots'])))
    for k in ('fixture', 'hidden'):
        require(sha(prepared['case'][k + '_path']) == prepared['case'][k + '_sha256'], k.upper() + '_BYTE_DRIFT')
    return dict(source_identity=prepared['runtime_source_identity'], source_manifest=sha(prepared['source_manifest']),
                dirty_diff=hashlib.sha256(git('diff', '--binary', 'HEAD')).hexdigest(),
                untracked_source=fingerprint({p:sha(root / p) for p in paths}),
                fixture=prepared['case']['fixture_sha256'], hidden=prepared['case']['hidden_sha256'],
                worktree_authority=hashlib.sha256(git('worktree', 'list', '--porcelain')).hexdigest(),
                consumer_audit=fingerprint(read(prepared['ref_policy'])['authority_audit']))


def verify_frozen(path, stage, *, invocation_root=None):
    """Mandatory checks remain effective with Python optimization enabled."""
    prepared = read(path)
    contract = dict(prepared['ownership_contract'])
    require('manifest_fingerprint' not in contract, 'CIRCULAR_MANIFEST_BINDING')
    contract['manifest_fingerprint'] = sha(path)
    # The full prepared file is hashed externally by the claim and supervisor.
    # Its payload fingerprint additionally detects accidental field rewrites.
    payload = {k:v for k,v in prepared.items() if k not in ('ownership_contract', 'evaluation_payload_fingerprint')}
    require(fingerprint(payload) == prepared['evaluation_payload_fingerprint'], 'EVALUATION_MANIFEST_DRIFT')
    require(contract['evaluation_id'] == prepared['evaluation_id'] and
            contract['source_identity'] == prepared['runtime_source_identity'], 'EVALUATION_IDENTITY_DRIFT')
    require(sha(prepared['python']) == prepared['python_sha256'], 'PYTHON_IDENTITY_DRIFT')
    for p, digest in prepared['frozen_dependencies'].items():
        require(sha(p) == digest, 'FROZEN_DEPENDENCY_DRIFT:' + p)
    baseline = read(prepared['ref_baseline'])
    policy = read(prepared['ref_policy'])
    base = snapshot(baseline['head'], baseline['branch'], '\n'.join(o+' '+n for n,o in baseline['refs']), baseline['inputs'])
    result = bounded_check(prepared['repository'], base, policy, lambda:authority_inputs(prepared))
    require(result['status'] == 'PASS', 'REF_INTEGRITY_V3:' + result['disposition'])
    ownership = verify_stage(contract, stage, invocation_root=invocation_root)
    if Stage(stage) != Stage.PRE_SPAWN:
        identity = ownership['supervisor']
        require(all(identity[k] == prepared[k] for k in ('deadline_seconds','grace_seconds','heartbeat_seconds')) and
                identity['cwd'] == str(Path(prepared['repository']).resolve()), 'SUPERVISOR_EXECUTION_CONTRACT_DRIFT')
        if 'child_argv' in prepared:
            require(identity['command'] == prepared['child_argv'], 'SUPERVISOR_FROZEN_COMMAND_DRIFT')
    # Recheck source bytes after observation to bound consumer/source races.
    require(authority_inputs(prepared) == dict(base.inputs), 'POST_OBSERVATION_SOURCE_DRIFT')
    return dict(status='PASS', evaluation_id=prepared['evaluation_id'],
                runtime_source_identity=prepared['runtime_source_identity'], ref_integrity=result, ownership=ownership)
