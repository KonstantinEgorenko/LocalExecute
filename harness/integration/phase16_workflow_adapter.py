"""Historical compatibility import for the canonical Workflow bridge.

Production composition resolves the authoritative bridge through
``harness.mission``. The old duplicate adapter name remains import-compatible.
"""

from harness.mission import WorkflowExecutionBridge


Phase16WorkflowAdapter = WorkflowExecutionBridge

__all__ = ["Phase16WorkflowAdapter", "WorkflowExecutionBridge"]
