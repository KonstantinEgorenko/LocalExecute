"""Qualified SIMPLE discovery binding; generic scoring and lifecycle stay external."""
from dataclasses import dataclass
import time

from harness.executor import WorkerExecutor
from harness.router.scoring import DecisionType, RoutingDecision


SIMPLE_DISCOVERY_V1 = 'SIMPLE_DISCOVERY_V1'


class SimpleDiscoveryCapabilityError(RuntimeError):
    def __init__(self, code, detail):
        self.code = self.category = code
        super().__init__(f'{code}: {detail}')


@dataclass(frozen=True)
class SimpleDiscoveryBindingV1:
    capability: str = SIMPLE_DISCOVERY_V1
    role: str = 'SIMPLE_DISCOVERY'
    execution_mode: str = 'SIMPLE'
    model_id: str = 'qwen35_heavy_fast'
    model_tag: str = 'qwen36-35b-a3b-heavy-fast:latest'
    model_digest: str = '8ace0953c8cab4b373c416dcb63fdca47b4b64d99fc9d4922d2444f17bae37e0'
    num_gpu: int = 34
    num_ctx: int = 32768

    def to_dict(self):
        from dataclasses import asdict
        return asdict(self)


class SimpleDiscoveryCapability:
    def __init__(self, *, controller, registry, executor):
        self.controller, self.registry, self.executor = controller, registry, executor
        self.binding = SimpleDiscoveryBindingV1()

    def _profile(self):
        b = self.binding
        try:
            entry = self.controller.profile_store.get(b.model_id)
            definition = self.controller.resolve_definition(b.model_id)
            current = self.registry.get(b.model_id)
            valid = (entry is not None and entry.get('status') == 'CALIBRATED'
                and entry.get('model_tag') == b.model_tag
                and type(entry.get('num_gpu')) is int and entry['num_gpu'] == b.num_gpu
                and entry.get('context_default') == b.num_ctx)
            for d in (definition, current):
                valid = valid and (d.ollama_model == b.model_tag
                    and d.runtime.get('execution') == 'gpu_worker'
                    and d.runtime.get('num_gpu') == b.num_gpu
                    and d.context.get('default') == b.num_ctx)
            endpoint = self.registry.get_server(current.server).ollama_base_url.rstrip('/')
            valid = valid and endpoint == self.registry.get_server('lead').ollama_base_url.rstrip('/')
        except (AttributeError, KeyError, RuntimeError) as exc:
            raise SimpleDiscoveryCapabilityError('SIMPLE_DISCOVERY_CAPABILITY_UNAVAILABLE', str(exc)) from exc
        if not valid:
            raise SimpleDiscoveryCapabilityError('SIMPLE_DISCOVERY_CAPABILITY_UNAVAILABLE', 'qualified runtime profile mismatch')
        return endpoint

    def _identity(self, endpoint, path, *, code='SIMPLE_DISCOVERY_CAPABILITY_UNAVAILABLE'):
        b = self.binding
        try:
            document = self.executor.client._request(endpoint, path, timeout_seconds=5)
            rows = document.get('models')
            if type(rows) is not list or any(type(row) is not dict for row in rows):
                raise ValueError('malformed catalog/residency')
            exact = [row for row in rows if (row.get('name') or row.get('model')) == b.model_tag]
            if len(exact) != 1 or exact[0].get('digest') != b.model_digest:
                raise ValueError('exact qualified tag/digest unavailable')
            if path == '/api/ps' and (len(rows) != 1
                    or exact[0].get('context_length') != b.num_ctx or not exact[0].get('size_vram', 0)):
                raise ValueError('qualified single-model residency mismatch')
        except Exception as exc:
            raise SimpleDiscoveryCapabilityError(code, str(exc)) from exc

    def select(self, *, profile, router):
        endpoint = self._profile()
        self._identity(endpoint, '/api/tags')
        router = getattr(router, 'inner', router)
        candidate = router._eligibility(self.binding.model_id, profile,
            router._branch_for(profile.task_type.value), set(), None)
        if candidate.rejection_reasons:
            raise SimpleDiscoveryCapabilityError('SIMPLE_DISCOVERY_CAPABILITY_UNAVAILABLE',
                '; '.join(candidate.rejection_reasons))
        return RoutingDecision(DecisionType.WORKER, self.binding.model_id, None, 1.0,
            self.binding.capability, (), candidate.feasibility.to_dict() if candidate.feasibility else None, None)

    def guarded_executor(self, observe):
        capability = self
        class GuardedClient:
            def chat(self, base_url, model_tag, messages, keep_alive, num_ctx, *, num_gpu=None, **kwargs):
                b = capability.binding
                endpoint = capability._profile()
                if (base_url.rstrip('/') != endpoint or model_tag != b.model_tag
                        or num_ctx != b.num_ctx or num_gpu != b.num_gpu):
                    raise SimpleDiscoveryCapabilityError('SIMPLE_DISCOVERY_MODEL_ROUTING_FAILURE', 'physical request mismatch')
                capability._identity(endpoint, '/api/tags', code='SIMPLE_DISCOVERY_MODEL_ROUTING_FAILURE')
                capability._identity(endpoint, '/api/ps', code='SIMPLE_DISCOVERY_MODEL_ROUTING_FAILURE')
                observe('SIMPLE_DISCOVERY_GENERATION_ADMITTED', b.to_dict())
                return capability.executor.client.chat(base_url, model_tag, messages, keep_alive,
                    num_ctx, num_gpu=num_gpu, **kwargs)
        worker = WorkerExecutor(self.executor.lifecycle, GuardedClient(), self.executor.snapshot_provider)
        class GuardedExecutor:
            def execute(self, request):
                b = capability.binding
                if (b.execution_mode != 'SIMPLE' or b.role != 'SIMPLE_DISCOVERY'
                        or b.capability != SIMPLE_DISCOVERY_V1
                        or request.routing_decision.selected_model != b.model_id
                        or request.routing_decision.routing_reason != b.capability):
                    raise SimpleDiscoveryCapabilityError('SIMPLE_DISCOVERY_MODEL_ROUTING_FAILURE', 'logical role mismatch')
                observe('SIMPLE_DISCOVERY_INVOCATION', {**b.to_dict(), 'request_id': request.request_id})
                started = time.monotonic()
                try:
                    result = worker.execute(request)
                except Exception as exc:
                    observe('SIMPLE_DISCOVERY_INVOCATION_FAILED', {**b.to_dict(),
                        'request_id':request.request_id,'duration_ms':int((time.monotonic()-started)*1000),
                        'error_code':getattr(exc,'code',type(exc).__name__)})
                    raise
                observe('SIMPLE_DISCOVERY_INVOCATION_FINISHED', {**b.to_dict(),
                    'request_id':request.request_id,'duration_ms':int((time.monotonic()-started)*1000)})
                return result
        return GuardedExecutor()
