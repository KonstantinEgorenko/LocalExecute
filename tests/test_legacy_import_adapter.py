"""Offline semantic-layout compatibility; never loads or generates models."""
import importlib
import os
import pickle
from pathlib import Path
import subprocess
import sys
import pytest
from harness.compatibility.import_mapping import MODULES, PACKAGES
from harness.compatibility.configuration import FILENAMES, normalize_configuration_document, resolve_config_path

PAIRS = {'harness.phase20u.launcher': 'harness.gpu_acceleration.launcher', 'harness.phase20u.policy': 'harness.gpu_acceleration.policy', 'harness.phase20u.runtime': 'harness.gpu_acceleration.runtime', 'harness.phase20u': 'harness.gpu_acceleration', 'harness.phase18r2.policy': 'harness.research_contracts.policy', 'harness.phase18r2.runtime': 'harness.research_contracts.runtime', 'harness.phase18r2.service': 'harness.research_contracts.service', 'harness.phase18r2': 'harness.research_contracts', 'harness.phase15.path_domain': 'harness.workspace.diagnosis_domain', 'harness.phase15.receipts': 'harness.recovery.diagnosis_receipts', 'harness.phase20a.browser': 'harness.research_environment.browser', 'harness.phase20a.capabilities': 'harness.research_environment.capabilities', 'harness.phase20a.evidence': 'harness.research_environment.evidence', 'harness.phase20a.files': 'harness.research_environment.files', 'harness.phase20a.media': 'harness.research_environment.media', 'harness.phase20a.models': 'harness.research_environment.models', 'harness.phase20a.policy': 'harness.research_environment.policy', 'harness.phase20a.url_policy': 'harness.research_environment.url_policy', 'harness.phase20a': 'harness.research_environment', 'harness.phase18.controller': 'harness.external_evidence.controller', 'harness.phase18.evidence': 'harness.external_evidence.evidence', 'harness.phase18.models': 'harness.external_evidence.models', 'harness.phase18.phase17_bridge': 'harness.external_evidence.mission_bridge', 'harness.phase18.ports': 'harness.external_evidence.ports', 'harness.phase18.provenance': 'harness.external_evidence.provenance', 'harness.phase18.providers': 'harness.external_evidence.providers', 'harness.phase18.reasoning': 'harness.external_evidence.reasoning', 'harness.phase18.security': 'harness.external_evidence.security', 'harness.phase18.service': 'harness.external_evidence.service', 'harness.phase18.store': 'harness.external_evidence.store', 'harness.phase18': 'harness.external_evidence', 'harness.phase20g.cli': 'harness.runtime_resources.gpu_coordination.cli', 'harness.phase20g.guard': 'harness.runtime_resources.gpu_coordination.guard', 'harness.phase20g.launcher': 'harness.runtime_resources.gpu_coordination.launcher', 'harness.phase20g.lease': 'harness.runtime_resources.gpu_coordination.lease', 'harness.phase20g.policy': 'harness.runtime_resources.gpu_coordination.policy', 'harness.phase20g': 'harness.runtime_resources.gpu_coordination', 'harness.phase20r.context': 'harness.runtime_hardening.context', 'harness.phase20r.github_guard': 'harness.runtime_hardening.github_guard', 'harness.phase20r.policy': 'harness.runtime_hardening.policy', 'harness.phase20r.research': 'harness.runtime_hardening.research', 'harness.phase20r.runtime_v4': 'harness.runtime_hardening.runtime_v4', 'harness.phase20r.service': 'harness.runtime_hardening.service', 'harness.phase20r.structured': 'harness.runtime_hardening.structured', 'harness.phase20r.transport': 'harness.runtime_hardening.transport', 'harness.phase20r': 'harness.runtime_hardening', 'harness.phase20q.acquisition': 'harness.research_integrity.acquisition', 'harness.phase20q.cleaner': 'harness.research_integrity.cleaner', 'harness.phase20q.constraints': 'harness.research_integrity.constraints', 'harness.phase20q.evaluator': 'harness.research_integrity.evaluator', 'harness.phase20q.freshness': 'harness.research_integrity.freshness', 'harness.phase20q.github_primary': 'harness.research_integrity.github_primary', 'harness.phase20q.github_runtime': 'harness.research_integrity.github_runtime', 'harness.phase20q.identity': 'harness.research_integrity.identity', 'harness.phase20q.metrics': 'harness.research_integrity.metrics', 'harness.phase20q.models': 'harness.research_integrity.models', 'harness.phase20q.policy': 'harness.research_integrity.policy', 'harness.phase20q.relevance': 'harness.research_integrity.relevance', 'harness.phase20q.research': 'harness.research_integrity.research', 'harness.phase20q.research_service': 'harness.research_integrity.research_service', 'harness.phase20q.revision': 'harness.research_integrity.revision', 'harness.phase20q.revision_transport': 'harness.research_integrity.revision_transport', 'harness.phase20q.semantic': 'harness.research_integrity.semantic', 'harness.phase20q.service': 'harness.research_integrity.service', 'harness.phase20q.web_provider': 'harness.research_integrity.web_provider', 'harness.phase20q': 'harness.research_integrity', 'harness.phase20v.launcher': 'harness.model_runtime.launcher', 'harness.phase20v.policy': 'harness.model_runtime.policy', 'harness.phase20v.runtime': 'harness.model_runtime.runtime', 'harness.phase20v.telemetry': 'harness.model_runtime.telemetry', 'harness.phase20v': 'harness.model_runtime', 'harness.phase20v.bootstrap.sitecustomize': 'harness.model_runtime.bootstrap.sitecustomize', 'harness.integration.phase16_production_adapters': 'harness.integration.workflow_production_adapters', 'harness.phase13': 'harness.observability.execution_contracts', 'harness.phase13.cascade_v2': 'harness.router.cascade_v2', 'harness.phase13.evidence': 'harness.observability.execution_evidence', 'harness.phase13.execution_loop': 'harness.executor.profile_execution', 'harness.phase13.execution_profiles': 'harness.executor.profiles', 'harness.phase13.faults': 'harness.observability.fault_attribution', 'harness.phase13.finalization': 'harness.executor.finalization', 'harness.phase13.metrics': 'harness.observability.fault_metrics', 'harness.phase13.profile_client': 'harness.executor.profile_client', 'harness.phase13.prompt': 'harness.executor.profile_prompt', 'harness.phase13.protocol': 'harness.executor.profile_protocol', 'harness.phase13.router_v2_metrics': 'harness.router.policy_metrics', 'harness.phase13.router_v2_models': 'harness.router.policy_models', 'harness.phase13.router_v2': 'harness.router.advanced_router', 'harness.phase13.tool_adapter': 'harness.executor.profile_tool_adapter', 'harness.phase13.tool_capabilities': 'harness.executor.profile_tool_capabilities', 'harness.phase13.tool_models': 'harness.executor.profile_tool_models', 'harness.phase13.tool_runtime': 'harness.executor.profile_tool_runtime', 'harness.phase13.tool_schema': 'harness.executor.profile_tool_schema', 'harness.phase19.ablation': 'harness.research_quality.ablation', 'harness.phase19.entailment': 'harness.research_quality.entailment', 'harness.phase19.evaluator': 'harness.research_quality.evaluator', 'harness.phase19.identity': 'harness.research_quality.identity', 'harness.phase19.models': 'harness.research_quality.models', 'harness.phase19.policy': 'harness.research_quality.policy', 'harness.phase19.store': 'harness.research_quality.store', 'harness.phase19': 'harness.research_quality', 'harness.phase18r.policy': 'harness.research_reasoning.policy', 'harness.phase18r.runtime': 'harness.research_reasoning.runtime', 'harness.phase18r.service': 'harness.research_reasoning.service', 'harness.phase18r': 'harness.research_reasoning', 'harness.phase15.clients': 'harness.recovery.clients', 'harness.phase15.controller': 'harness.recovery.controller', 'harness.phase15.evidence': 'harness.recovery.evidence', 'harness.phase15.models': 'harness.recovery.models', 'harness.phase15.prompt': 'harness.recovery.prompt', 'harness.phase15.protocol': 'harness.recovery.protocol', 'harness.phase15.transport': 'harness.recovery.transport', 'harness.phase15': 'harness.recovery', 'harness.phase19q.derive': 'harness.research_remediation.derive', 'harness.phase19q.issues': 'harness.research_remediation.issues', 'harness.phase19q.models': 'harness.research_remediation.models', 'harness.phase19q.planner': 'harness.research_remediation.planner', 'harness.phase19q.policy': 'harness.research_remediation.policy', 'harness.phase19q.research': 'harness.research_remediation.research', 'harness.phase19q.revision': 'harness.research_remediation.revision', 'harness.phase19q.service': 'harness.research_remediation.service', 'harness.phase19q.store': 'harness.research_remediation.store', 'harness.phase19q': 'harness.research_remediation', 'harness.phase14.ablation': 'harness.router_evaluation.ablation', 'harness.phase14.comparison': 'harness.router_evaluation.comparison', 'harness.phase14.io': 'harness.router_evaluation.io', 'harness.phase14.metrics': 'harness.router_evaluation.metrics', 'harness.phase14.models': 'harness.router_evaluation.models', 'harness.phase14': 'harness.router_evaluation', 'harness.phase20b.docker_cli': 'harness.sandbox.docker_cli', 'harness.phase20b.executor': 'harness.sandbox.executor', 'harness.phase20b.models': 'harness.sandbox.models', 'harness.phase20b.policy': 'harness.sandbox.policy', 'harness.phase20b.sandbox': 'harness.sandbox.sandbox', 'harness.phase20b.security': 'harness.sandbox.security', 'harness.phase20b.store': 'harness.sandbox.store', 'harness.phase20b': 'harness.sandbox', 'harness.phase16.context_spine': 'harness.workflow.context_spine', 'harness.phase16.controller': 'harness.workflow.controller', 'harness.phase16.models': 'harness.workflow.models', 'harness.phase16.ports': 'harness.workflow.ports', 'harness.phase16.recovery_bridge': 'harness.workflow.recovery_bridge', 'harness.phase16.root_projection': 'harness.workflow.root_projection', 'harness.phase16': 'harness.workflow', 'harness.phase17.controller': 'harness.mission.controller', 'harness.phase17.models': 'harness.mission.models', 'harness.phase17.phase16_bridge': 'harness.mission.workflow_bridge', 'harness.phase17.ports': 'harness.mission.ports', 'harness.phase17.store': 'harness.mission.store', 'harness.phase17': 'harness.mission', 'harness.integration.phase16_workflow_adapter': 'harness.mission.workflow_bridge'}

@pytest.mark.parametrize('old,new', PAIRS.items())
def test_qualified_import_pair(old,new):
    a=importlib.import_module(old); b=importlib.import_module(new)
    assert importlib.import_module(old) is a
    if old in MODULES:
        assert a is b
        assert a.__spec__.name == new
        assert a.__package__ == new.rpartition('.')[0]
    else:
        for name in getattr(a,'__all__',()):
            if old == 'harness.integration.phase16_workflow_adapter' and name == 'Phase16WorkflowAdapter':
                assert getattr(a,name) is b.WorkflowExecutionBridge
            else: assert getattr(a,name) is getattr(b,name)

@pytest.mark.parametrize('name', list(PACKAGES))
def test_package_from_import_and_unknown(name):
    package=importlib.import_module(name)
    for export in getattr(package,'__all__',()):
        scope={};exec('from '+name+' import '+export,scope)
        assert scope[export] is getattr(package,export)
    with pytest.raises(ModuleNotFoundError): importlib.import_module(name+'.not_a_registered_module')
    with pytest.raises(AttributeError): getattr(package,'not_a_registered_export')

def test_pickle_legacy_global_and_canonical_identity():
    cls=pickle.loads(b'charness.phase17.models\nPhase17Policy\n.')
    from harness.mission.models import MissionLifecyclePolicy
    assert cls is MissionLifecyclePolicy
    assert importlib.reload(importlib.import_module('harness.phase17.models')) is importlib.import_module('harness.mission.models')

@pytest.mark.parametrize('old,new',FILENAMES.items())
def test_legacy_filename_resolves_semantic(tmp_path,old,new):
    semantic=tmp_path/new;semantic.write_text('canonical')
    assert resolve_config_path(tmp_path/old)==semantic
    semantic.unlink();legacy=tmp_path/old;legacy.write_text('legacy')
    assert resolve_config_path(legacy)==legacy

def test_legacy_key_conflict_is_closed():
    with pytest.raises(ValueError): normalize_configuration_document({'phase14':{},'routing_evaluation':{}})

def test_no_physical_legacy_payload():
    root=Path(__file__).resolve().parents[1]
    assert not list((root/'harness').glob('phase*'))
    assert not list((root/'config').glob('phase*.yaml'))
    assert (root/'harness/model_runtime/bootstrap/sitecustomize.py').is_file()

@pytest.mark.parametrize('legacy',['harness.phase20g.cli','harness.phase20g.launcher','harness.phase20u.launcher','harness.phase20v.launcher'])
def test_legacy_module_help(legacy):
    env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1','SWARM_MODEL_RUNTIME':'0','SWARM_GPU_HEAVY_GUARD':'0','SWARM_ORNITH_GPU_BURST':'0'}
    p=subprocess.run([sys.executable,'-B','-m',legacy,'--help'],env=env,capture_output=True,timeout=20)
    assert p.returncode==0,(p.stdout,p.stderr)

def _bootstrap_env(root):
    env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1','SWARM_MODEL_RUNTIME':'1','SWARM_MODEL_RUNTIME_ROOT':str(root),'GPU_HEAVY_LOCK_PATH':str(root/'gpu.lock'),'MODEL_RUNTIME_LOCK_PATH':str(root/'runtime.lock')}
    source=Path(__file__).resolve().parents[1]
    env['PYTHONPATH']=os.pathsep.join([str(source/'harness/model_runtime/bootstrap'),str(source)])
    return env

def test_canonical_bootstrap_automatic_fail_closed(tmp_path):
    # No model/server access: root validation fails before runtime construction.
    env=_bootstrap_env(tmp_path);env['SWARM_MODEL_RUNTIME_ROOT']=''
    p=subprocess.run([sys.executable,'-B','-c','raise AssertionError("CHILD_RAN")'],cwd=tmp_path,env=env,capture_output=True,timeout=20)
    assert p.returncode==88
    assert b'BOOTSTRAP FAILED' in p.stderr and b'CHILD_RAN' not in p.stderr

def test_canonical_bootstrap_installed_before_child(tmp_path):
    import shutil
    source=Path(__file__).resolve().parents[1]
    shutil.copytree(source/'config',tmp_path/'config')
    env=_bootstrap_env(tmp_path)
    code="import sys; assert 'sitecustomize' in sys.modules; assert 'harness.model_runtime.runtime' in sys.modules; from harness.model_runtime import current_controller; assert current_controller() is not None; from harness.runtime_resources.gpu_coordination.guard import current_lease; assert current_lease() is not None; print('GUARDS_BEFORE_CHILD')"
    p=subprocess.run([sys.executable,'-B','-c',code],cwd=tmp_path,env=env,capture_output=True,timeout=25)
    assert p.returncode==0,(p.stdout,p.stderr)
    assert b'GUARDS_BEFORE_CHILD' in p.stdout
