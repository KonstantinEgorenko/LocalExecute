"""Native child: real production composition, deterministic physical backend only."""
import json
import os
from pathlib import Path
import socket
import sys
from contextlib import ExitStack
from unittest.mock import patch

from tests.public_runtime.responses import response, proof


def main(root):
    root = Path(root)
    from importlib.abc import MetaPathFinder
    class NoPrivateEvaluator(MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname.startswith(('harness.integration.independent_acceptance',
                                    'harness.verification.independent_')):
                raise AssertionError('PRIVATE_EVALUATOR_IMPORT_FORBIDDEN: '+fullname)
    sys.meta_path.insert(0, NoPrivateEvaluator())
    from harness.supervision.windows import Windows
    ownership = json.loads((root/'ownership.json').read_text())
    proof['ownership'] = Windows().verify_child(ownership['child'], ownership['parent'])
    def denied(*a, **kw):
        raise AssertionError('REAL_NETWORK_FORBIDDEN')
    socket.create_connection = denied
    socket.socket.connect = denied
    from harness.model_runtime.runtime import MemoryTelemetry
    from harness.gpu_acceleration.runtime import GpuMemory
    from harness.executor.client import OllamaNativeClient
    from harness.integration.production_launcher import main as launch
    import harness.lead.runtime as lead
    from harness.config import ConfigLoader
    from harness.integration.single_endpoint import install_single_endpoint_overlay
    install_single_endpoint_overlay()
    from harness.models import ModelRegistry
    registry = ModelRegistry.from_loader(ConfigLoader(root/'config-root'))
    from harness.integration.simple_discovery_capability import SimpleDiscoveryBindingV1
    discovery_binding = SimpleDiscoveryBindingV1()
    from harness.judge.public_interpreter import PublicInterpreterModelV1
    interpreter = PublicInterpreterModelV1()
    digests = {discovery_binding.model_tag: discovery_binding.model_digest,
               interpreter.model_tag: interpreter.model_digest}
    rows = {}

    class Backend:
        def ps(self, url, **kw): return tuple(rows.values())
        def models(self, url): return tuple(rows.values())
        def tags(self, url, **kw): return tuple(registry.get(name).ollama_model for name in registry.logical_ids)
        def show(self, url, tag): return {'model_info': {'test.block_count': 40}}
        def block_count(self, url, tag): return 40
        def resident(self, url, tag): return rows.get(tag)
        def unload(self, url, tag): rows.pop(tag, None)
        def preload(self, url, tag, num_ctx, num_gpu):
            assert not rows, 'COMPETING_MODEL_RESIDENCY'
            rows[tag] = dict(name=tag, digest=digests.get(tag, '0'*64), context_length=num_ctx, size=10*1024**3, size_vram=8*1024**3)
            return {'done': True}
        def chat_probe(self, *a): return {'done': True, 'message': {'content': 'PHASE20V_OK'}}
    backend = Backend()

    class Placement:
        def __init__(self, policy): self.policy = policy
        def models(self, url): return backend.ps(url)
        def resident(self, url, tag): return backend.resident(url, tag)
        def unload(self, url, tag): return backend.unload(url, tag)
        def place_lead(self, layers):
            backend.preload('', self.policy.model_tag, 65536, layers)
            return backend.resident('', self.policy.model_tag)
    class Memory:
        def sample(self): return MemoryTelemetry(48000, 17536, 65536, 2000, 14384, 16384)
    class Gpu:
        def sample(self): return GpuMemory(2000, 14384, 16384)
    class Response:
        def __init__(self, data): self.data = data
        def read(self, *a): return self.data
        def __enter__(self): return self
        def __exit__(self, *a): pass
    def wire(client, url, suffix, *, timeout_seconds, payload=None):
        if suffix == '/api/ps': value = {'models': list(backend.ps(url))}
        elif suffix == '/api/tags': value = {'models': [{'name': tag, 'digest': digests.get(tag, '0'*64)} for tag in backend.tags(url)]}
        elif suffix == '/api/generate' and not payload.get('prompt'):
            if payload.get('keep_alive') == 0:
                backend.unload(url, payload['model'])
            elif not backend.resident(url, payload['model']):
                backend.preload(url, payload['model'], payload['options']['num_ctx'], payload['options']['num_gpu'])
            value = {'done': True, 'model': payload['model']}
        else:
            assert suffix == '/api/chat' and payload.get('messages')
            value = response(payload)
        return value, json.dumps(value).encode()
    def physical(request, timeout=None):
        return Response(json.dumps(response(json.loads(request.data))).encode())
    def discovery(url, timeout=None):
        assert url.endswith('/api/ps')
        return Response(json.dumps({'models': backend.ps(url)}).encode())

    def mission(*a, **kw):
        from harness.integration.runtime_factory import create_production_swarm_runtime
        from harness.mission import MissionSpecV1
        from harness.integration.plan_shape_normalization import SINGLE_IMPLEMENTATION_TODO_CONSTRAINT
        runtime = create_production_swarm_runtime(swarm_root=root/'config-root', workspace=root/'target', state_root=root/'state')
        objective = ('Customers report incorrect invoice totals when discounts and tax are combined. '
                     'Fix the behavior while preserving the existing public API and rounding contract. '
                     'Add or update appropriate tests.')
        spec = MissionSpecV1('public-mission', 'public-goal', objective, objective, {
            'workspace': str(root/'target'), 'research_required': False,
            'SEQUENTIAL_MODEL_EXECUTION': True, 'NO_AUTO_MERGE': True,
            'TYPED_SCENARIO_EXECUTION': True, SINGLE_IMPLEMENTATION_TODO_CONSTRAINT: True,
            'DETERMINISTIC_TEST_CONTRACT': dict(version=1, preserve_baseline=True,
                differential=True, deterministic_callables=['invoice_total']),
        })
        runtime.run_or_resume(spec)
        checkpoint = runtime.mission_store.load(spec.mission_id)
        proof['mission'] = checkpoint.to_dict()
        assert checkpoint.state.value == 'COMPLETED', checkpoint.failure_reason
        proof['report'] = runtime.mission_store.load_report(spec.mission_id).to_dict()
        assert proof['report']['final_summary'].strip()
        events = [json.loads(line) for line in
                  (root/'state/traces/public-mission/mission_trace.jsonl').read_text().splitlines()]
        proof['verification'] = [e['payload'] for e in events if e['event_type'] == 'VERIFICATION_FINISHED']
        proof['judge'] = [e['payload'] for e in events if e['event_type'] == 'JUDGE_FINISHED']
        proof['exports'] = [e['payload'] for path in (root/'state/control-plane/runs').glob('*/state.json')
                            for e in json.loads(path.read_text())['records']['evidence']
                            if e['evidence_type'] == 'ACCEPTED_EXPORT_V1']
        assert not any('independent_' in name for name in sys.modules if name.startswith('harness.'))

    with ExitStack() as stack:
        for name, value in (
            ('harness.model_runtime.runtime.OllamaRuntimeBackend', lambda *a: backend),
            ('harness.model_runtime.runtime.MemoryProbe', Memory),
            ('harness.model_runtime.telemetry.MemoryProbe', Memory),
            ('harness.gpu_acceleration.runtime.OllamaPlacementBackend', Placement),
            ('harness.gpu_acceleration.runtime.NvidiaSmiMemoryProbe', Gpu),
            ('harness.runtime.urlopen', discovery),
            ('runpy.run_module', mission),
        ): stack.enter_context(patch(name, value))
        from harness.resource_manager.snapshot import GpuSnapshot, RamSnapshot
        stack.enter_context(patch('harness.resource_manager.manager.discover_gpu', lambda: GpuSnapshot('Synthetic', 16, 2, 14, 0)))
        stack.enter_context(patch('harness.resource_manager.manager.discover_ram', lambda: RamSnapshot(64, 16, 48)))
        stack.enter_context(patch.object(OllamaNativeClient, '_request_response', wire))
        stack.enter_context(patch.object(lead, 'urlopen', physical))
        try:
            launch(['--root', str(root/'config-root'), '--module', 'tests.public_runtime.native_child'])
        finally:
            from harness.runtime_resources.gpu_coordination.guard import current_lease
            from harness.model_runtime import current_controller
            proof.update(network_forbidden=True, real_generations=0, residency_empty=not rows,
                         locks_free=not current_lease().status().locked,
                         single_owner=current_controller().gpu_lease is current_lease())
            (root/'result.json').write_text(json.dumps(proof, indent=2), encoding='utf8')


if __name__ == '__main__':
    try:
        main(sys.argv[1])
    except BaseException:
        import traceback
        (Path(sys.argv[1])/'child-error.txt').write_text(traceback.format_exc(), encoding='utf8')
        raise
