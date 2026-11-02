"""Instance-local coding worker binding for the opt-in E2E evaluation only."""
from contextlib import contextmanager
import math

from .executor.executor import WorkerExecutor
from .executor.lifecycle import PreparedWorker
from .router.scoring import DecisionType, RoutingDecision
from .model_performance_benchmark import profiles_from_runtime


class FixedWorkerRouter:
    def __init__(self, model_id, production_router, registry):
        self.model_id, self.production_router, self.registry = model_id, production_router, registry
        registry.get(model_id)  # No unresolved or fuzzy worker substitution.

    def route(self, task_profile):
        admission = None
        definition = self.registry.get(self.model_id)
        if definition.runtime.get('execution') == 'gpu_worker':
            status = next((s for s in task_profile.resource_state.model_statuses if s.logical_id == self.model_id), None)
            feasibility = self.production_router._feasibility(self.model_id, task_profile, status)
            if feasibility is None or not feasibility.allowed:
                return RoutingDecision(DecisionType.NO_ELIGIBLE_MODEL, None, None, 0,
                                       'forced worker resource admission failed', (), None, None)
            admission = feasibility.to_dict()
        return RoutingDecision(DecisionType.WORKER, self.model_id, None, 1,
                               'diagnostic forced coding worker', (), admission, None)


class PlacementLifecycle:
    def __init__(self, *, common, profile, recorder, burst_manager=None):
        self.common, self.profile, self.recorder, self.burst_manager = common, profile, recorder, burst_manager
        self.client = common.executor.client
        self.digest = None

    def observe(self, stage):
        p = self.profile
        try:
            data = self.client._request(p.endpoint, '/api/ps', timeout_seconds=5)
            models = data['models']
            matches = [m for m in models if m.get('name', m.get('model')) == p.model]
            if len(matches) != 1 or any(m.get('size_vram', 0) > 0 for m in models if m not in matches):
                raise ValueError('selected model absent or unexpected GPU resident')
            resident = matches[0]
            tags = self.client._request(p.endpoint, '/api/tags', timeout_seconds=5)['models']
            exact = [m for m in tags if m.get('name', m.get('model')) == p.model]
            if (len(exact) != 1 or not exact[0].get('digest') or resident.get('digest') != exact[0]['digest'] or
                    resident.get('context_length') != p.num_ctx or
                    type(resident.get('size_vram')) not in (int,float) or not math.isfinite(resident['size_vram']) or not resident['size_vram'] > 0 or
                    ('num_gpu' in resident and resident['num_gpu'] != p.num_gpu)):
                raise ValueError('exact model/digest/context/GPU placement mismatch')
            if self.digest is not None and self.digest != resident['digest']:
                raise ValueError('model digest changed during invocation')
            self.digest = resident['digest']
            self.recorder.record('SHOOTOUT_WORKER_PLACEMENT', actor=p.profile_id,
                payload={'stage': stage, 'selected': p.effective_placement, 'observed': resident})
            return resident
        except Exception as exc:
            self.recorder.record('SHOOTOUT_PLACEMENT_FAILED', actor=p.profile_id, payload={'error': str(exc)})
            raise RuntimeError('RUNTIME_PLACEMENT_MISMATCH') from exc

    @contextmanager
    def prepared(self, decision):
        p = self.profile
        if decision.selected_model != p.profile_id:
            self.recorder.record('SHOOTOUT_CONTAMINATION', payload={'observed': decision.selected_model})
            raise RuntimeError('WORKER_MODEL_CONTAMINATION')
        if self.burst_manager is None:
            with self.common.executor.lifecycle.prepared(decision) as prepared:
                if (prepared.configured_num_gpu, prepared.requested_context_length, prepared.model_tag) != (p.num_gpu,p.num_ctx,p.model):
                    self.recorder.record('SHOOTOUT_PLACEMENT_FAILED', payload={'error': 'prepared options mismatch'})
                    raise RuntimeError('RUNTIME_PLACEMENT_MISMATCH')
                self.observe('before')
                yield prepared
                self.observe('after')
        else:
            with self.burst_manager.burst('model-e2e-shootout-worker') as state:
                if state.mode != 'gpu' or state.num_gpu != p.num_gpu:
                    self.recorder.record('SHOOTOUT_PLACEMENT_FAILED', payload={'error': 'CPU fallback rejected'})
                    raise RuntimeError('RUNTIME_PLACEMENT_MISMATCH: CPU fallback rejected')
                observed = self.observe('before')
                prepared = PreparedWorker(p.profile_id,p.model,p.endpoint,(),self.common.snapshot_provider(),
                    p.num_ctx,observed['context_length'],p.num_gpu)
                yield prepared
                self.observe('after')

    def mark_failure(self, error, stage, prepared):
        self.common.executor.lifecycle.mark_failure(error, stage, prepared)


def bind_worker(runtime, root, model_id, recorder):
    """Replace only two loop-owned references; preserve every production wrapper."""
    profile = profiles_from_runtime(root, 'production', model_id)[0]
    common = runtime.common
    loop = runtime.workflow_controller.executor.agent.loop
    from .integration.progress_budget import ProgressBudgetExecutor
    if not isinstance(loop.executor, ProgressBudgetExecutor):
        raise RuntimeError('unsupported production coding executor seam')
    shared = (common.router, common.executor, common.lead, common.critic, common.judge,
              runtime.mission_controller.planner, runtime.workflow_controller.router,
              runtime.workflow_controller.recovery)
    manager = None
    if common.registry.get(model_id).runtime.get('execution') != 'gpu_worker':
        from .gpu_acceleration.runtime import current_manager
        manager = current_manager()
        if manager is None or manager.policy.model_tag != profile.model:
            raise RuntimeError('requested worker runtime unresolved')
    production_router = common.router.inner
    loop.router = FixedWorkerRouter(model_id, production_router, common.registry)
    from .integration.structured_worker_client import ProductionStructuredWorkerClient
    client = ProductionStructuredWorkerClient(common.executor.client.timeouts)
    # One identical bounded coding output contract for both candidates. Instance
    # attribute only; shared production transport and non-worker clients are untouched.
    client.STRICT_JSON_OVERRIDES = {profile.model: {"think": False, "num_predict": 1024}}
    recorder.record('SHOOTOUT_WORKER_BINDING', actor=model_id,
        payload={'selected': profile.effective_placement, 'think': False, 'num_predict': 1024})
    loop.executor.inner = WorkerExecutor(PlacementLifecycle(common=common, profile=profile, recorder=recorder,
        burst_manager=manager), client, common.snapshot_provider)
    assert shared == (common.router, common.executor, common.lead, common.critic, common.judge,
                      runtime.mission_controller.planner, runtime.workflow_controller.router,
                      runtime.workflow_controller.recovery)
    return profile
