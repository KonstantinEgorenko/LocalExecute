"""Curated V1 public export decoding; canonical __all__ remains semantic."""
import sys
LEGACY_EXPORTS = {'harness.mission': {'Phase17Policy': 'MissionLifecyclePolicy', 'Phase16WorkflowBridge': 'WorkflowExecutionBridge'}, 'harness.workflow': {'Phase16Policy': 'WorkflowExecutionPolicy', 'Phase15RecoveryBridge': 'WorkflowRecoveryBridge', 'build_phase16_failure_evidence': 'build_workflow_failure_evidence'}, 'harness.external_evidence': {'Phase18Policy': 'ResearchWorkflowPolicy', 'Phase17ResearchBridge': 'MissionResearchBridge'}, 'harness.model_runtime': {'Phase20VPolicy': 'ModelLifecyclePolicy'}}

def resolve_legacy_export(module_name, name):
    semantic = LEGACY_EXPORTS.get(module_name, {}).get(name)
    module = sys.modules[module_name]
    if semantic is None or semantic not in module.__dict__:
        raise AttributeError(name)
    return module.__dict__[semantic]
