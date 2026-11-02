"""Opt-in metadata-only production GPU/environment parity check. No model requests."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from .resource_manager.gpu_probe import GpuProbeError
from .resource_manager.snapshot import discover_gpu
from .supervision.windows import Windows

def collect():
    try:
        gpu = discover_gpu()
        return {'status':'PASS', 'gpu':asdict(gpu), 'probe':asdict(gpu.probe)}
    except GpuProbeError as exc:
        return {'status':'FAIL', 'gpu':None, 'probe':asdict(exc.evidence)}

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--environment-manifest', type=Path,
        help='Invocation JSON containing exact replacement environment and cwd; never executes its argv.')
    parser.add_argument('--windows-job', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--child', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.child:
        args.output.write_text(json.dumps(collect(), indent=2), encoding='utf-8')
        return 0
    if args.output.exists():
        parser.error('output must be a fresh diagnostic directory')
    environment = dict(os.environ)
    cwd = Path.cwd()
    if args.environment_manifest:
        manifest = json.loads(args.environment_manifest.read_text(encoding='utf-8'))
        environment = manifest['environment']
        cwd = Path(manifest['cwd'])
        if (type(environment) is not dict or any(type(k) is not str or type(v) is not str
                or '\0' in k+v or '=' in k for k,v in environment.items())):
            parser.error('invalid replacement environment')
    args.output.mkdir(parents=True, exist_ok=False)
    child_output = args.output.resolve() / 'probe.json'
    command = [sys.executable, '-X', 'utf8', '-m', __name__.replace('__main__','harness.gpu_discovery_probe'),
               '--child', '--output', str(child_output)]
    owned = None
    lifecycle = {'command':command, 'cwd':str(cwd), 'replacement_environment':bool(args.environment_manifest),
                 'windows_job':args.windows_job, 'started_monotonic':time.monotonic()}
    try:
        if args.windows_job:
            backend = Windows()
            owned = backend.spawn(command, cwd, environment, 'GpuMetadata-'+uuid.uuid4().hex)
            lifecycle['identity'] = owned.identity
            owned.resume()
            deadline = time.monotonic()+20
            while owned.poll() is None and time.monotonic() < deadline:
                time.sleep(.05)
            code = owned.poll()
            if code is None:
                owned.terminate()
                raise TimeoutError('synthetic metadata process exceeded 20 seconds')
            lifecycle['exit_code'] = code
        else:
            completed = subprocess.run(command, cwd=cwd, env=environment, capture_output=True, timeout=20)
            lifecycle['exit_code'] = completed.returncode
        result = json.loads(child_output.read_text(encoding='utf-8'))
        lifecycle['probe_status'] = result['status']
    finally:
        if owned:
            owned.close()
        lifecycle['finished_monotonic'] = time.monotonic()
        (args.output / 'lifecycle.json').write_text(json.dumps(lifecycle, indent=2), encoding='utf-8')
    print(json.dumps({'status':result['status'], 'output':str(args.output)}))
    return 0 if result['status']=='PASS' else 1

if __name__ == '__main__':
    raise SystemExit(main())
