"""Regression for concurrent legacy alias/canonical reload metadata corruption."""
import os
from pathlib import Path
import subprocess
import sys
import pytest

@pytest.mark.parametrize('case', ['concurrent', 'initializing', 'legacy-first', 'canonical-first'])
def test_fresh_import_reload_scenario(case, tmp_path):
    source = Path(__file__).resolve().parents[1]
    env = {**os.environ, 'PYTHONDONTWRITEBYTECODE':'1', 'PYTHONPATH':str(source), 'SWARM_MODEL_RUNTIME':'0'}
    p = subprocess.run([sys.executable, '-B', str(source/'tests/compatibility_scenarios.py'), case], cwd=tmp_path, env=env, capture_output=True, timeout=35)
    assert p.returncode == 0, (p.stdout, p.stderr)
    assert b'"status": "PASS"' in p.stdout

def test_empty_workload_report_uses_explicit_missing_values():
    from harness.model_performance_benchmark import render
    report = dict(models=[dict(profile=dict(profile_id='offline-profile', model='offline-model', endpoint='http://127.0.0.1:9', num_ctx=4096, num_gpu=1), status='NOT_RUN', quantization=None, workloads=[])], speed_ranking=[], stability_ranking=[])
    text = render(report)
    assert '| offline-profile | N/A | NOT_RUN | N/A | N/A | N/A | N/A | N/A |' in text
    assert 'Р' not in text and '\ufffd' not in text
