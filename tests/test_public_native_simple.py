"""Standalone real native SIMPLE pipeline; no private experiment material."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

import pytest


def git(root, *args):
    env = {**os.environ, 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1',
           'GIT_AUTHOR_NAME': 'Public Fixture', 'GIT_COMMITTER_NAME': 'Public Fixture',
           'GIT_AUTHOR_EMAIL': 'fixture@example.invalid', 'GIT_COMMITTER_EMAIL': 'fixture@example.invalid'}
    return subprocess.run(['git', '-c', 'core.autocrlf=false', '-C', str(root), *args],
        env=env, check=True, capture_output=True, text=True, timeout=30).stdout.strip()


@pytest.mark.skipif(os.name != 'nt', reason='Windows native Job qualification')
def test_standalone_native_simple_completion():
    from harness.supervision.windows import Windows
    source = Path(__file__).resolve().parents[1]
    root = Path(tempfile.gettempdir())/('p'+uuid.uuid4().hex[:4])
    root.mkdir(exist_ok=False)
    config = root/'config-root'
    shutil.copytree(source/'config', config/'config')
    git(config, 'init', '-b', 'main')
    git(config, '-c', 'commit.gpgsign=false', 'commit', '--allow-empty', '-m', 'public configuration identity')
    subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(config/'.venv')],
                   check=True, timeout=45)
    import pytest as pytest_module
    (config/'.venv/Lib/site-packages/test_dependencies.pth').write_text(
        str(Path(pytest_module.__file__).resolve().parents[1])+'\n', encoding='utf8')
    cache = config/'state/evaluation/phase20v/runtime_profiles.json'
    cache.parent.mkdir(parents=True)
    shutil.copyfile(source/'tests/public_runtime/synthetic_profiles.json', cache)
    target = root/'target'
    target.mkdir()
    fixture = json.loads((source/'tests/public_runtime/fixture.json').read_text())
    for name, value in fixture.items():
        path = target/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding='utf8')
    git(target, 'init', '-b', 'main')
    git(target, 'add', '--all')
    git(target, '-c', 'commit.gpgsign=false', 'commit', '-m', 'public fixture')
    base = git(target, 'rev-parse', 'HEAD')
    env = {**os.environ, 'PYTHONUTF8': '1', 'PYTHONPATH': os.pathsep.join([str(source), *sys.path]),
           'SWARM_MODEL_RUNTIME': '0', 'SWARM_ORNITH_GPU_BURST': '0',
           'GPU_HEAVY_LOCK_PATH': str(root/'gpu.lock'), 'MODEL_RUNTIME_LOCK_PATH': str(root/'runtime.lock')}
    # A Windows venv executable is a redirector. Pin the real child image rather
    # than weakening the native image-identity check.
    python = getattr(sys, '_base_executable', sys.executable)
    windows = Windows()
    child = windows.spawn([python, '-S', '-m', 'tests.public_runtime.native_child', str(root)], source, env, uuid.uuid4().hex)
    try:
        (root/'ownership.json').write_text(json.dumps(dict(
            child=child.identity, parent=windows.parent_identity())), encoding='utf8')
        child.resume()
        deadline = time.monotonic()+240
        while child.poll() is None and time.monotonic() < deadline:
            time.sleep(.1)
        assert child.poll() == 0, f'Native child failed; evidence retained at {root}'
    finally:
        child.close()
    result = json.loads((root/'result.json').read_text())
    assert result['mission']['state'] == 'COMPLETED'
    assert result['real_generations'] == 0 and result['network_forbidden']
    assert result['locks_free'] and result['residency_empty'] and result['single_owner']
    assert 'SIMPLE_DISCOVERY' in result['simulated_roles']
    assert 'SOURCE' in result['simulated_roles'] and 'TEST' in result['simulated_roles']
    assert 'CRITIC' in result['simulated_roles'] and 'JUDGE' in result['simulated_roles']
    assert result['report']['final_summary'].strip()
    assert result['verification'] and all(x['outcome'] == 'PASSED' for x in result['verification'])
    assert result['judge'] and all(x['verdict'] == 'ACCEPT' for x in result['judge'])
    assert len(result['exports']) == 1
    exported = result['exports'][0]
    assert len(exported['content_sha256']) == 64 and exported['files']
    assert git(target, 'rev-parse', 'HEAD') == base and not git(target, 'status', '--porcelain')
