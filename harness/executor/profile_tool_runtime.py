'Small additive runtime boundary for Tool Protocol V2.\n\nprofile tools intentionally does not replace CodingAgentLoop V1. execution profiles may use\nthis session object while adding execution profiles/finalization.\n'
from __future__ import annotations

from harness.workspace.models import WorkspaceSession
from harness.executor.profile_protocol import parse_agent_action_v2
from harness.executor.profile_tool_adapter import ToolProtocolV2Adapter
from harness.executor.profile_tool_capabilities import V2ToolCapabilities, capabilities_for_session
from harness.executor.profile_tool_models import V2AgentAction, V2ToolCall, V2ToolFeedback
from harness.executor.profile_tool_schema import agent_action_json_schema_v2


class ToolProtocolV2Session:
    def __init__(self, session: WorkspaceSession) -> None:
        if not isinstance(session, WorkspaceSession):
            raise ValueError("session must be WorkspaceSession")
        self.workspace_session = session
        self.capabilities: V2ToolCapabilities = capabilities_for_session(session)
        self.adapter = ToolProtocolV2Adapter(session)

    def schema(self) -> dict:
        return agent_action_json_schema_v2(self.capabilities)

    def parse(self, raw: str) -> V2AgentAction:
        return parse_agent_action_v2(raw, self.capabilities)

    def execute(self, call_id: str, call: V2ToolCall) -> V2ToolFeedback:
        return self.adapter.execute(call_id, call)
