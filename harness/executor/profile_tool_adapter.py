'Session-bound V2 adapter over the frozen workspace WorkspaceToolDispatcher.'
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from harness.workspace.dispatcher import WorkspaceToolDispatcher
from harness.workspace.errors import WorkspacePathError
from harness.workspace.models import ToolName, ToolRequest, ToolResult, ToolStatus, WorkspaceSession
from harness.workspace.workspace import normalize_relative_path
from harness.executor.profile_tool_capabilities import V2ToolCapabilities, capabilities_for_session
from harness.executor.profile_tool_models import V2ToolCall, V2ToolFaultCode, V2ToolFeedback, V2ToolName


_DIRECT_TO_V1 = {
    V2ToolName.LIST_FILES: ToolName.LIST_FILES,
    V2ToolName.READ_TEXT: ToolName.READ_TEXT,
    V2ToolName.SEARCH_TEXT: ToolName.SEARCH_TEXT,
    V2ToolName.WRITE_TEXT: ToolName.WRITE_TEXT,
    V2ToolName.REPLACE_TEXT: ToolName.REPLACE_TEXT,
    V2ToolName.DELETE_FILE: ToolName.DELETE_FILE,
    V2ToolName.RUN_COMMAND: ToolName.RUN_COMMAND,
}


@dataclass(frozen=True, slots=True)
class LastReadRevision:
    path: str
    sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.path, str) or not self.path:
            raise ValueError("path must be non-empty string")
        if (
            not isinstance(self.sha256, str)
            or len(self.sha256) != 64
            or any(ch not in "0123456789abcdefABCDEF" for ch in self.sha256)
        ):
            raise ValueError("sha256 must be 64 hexadecimal characters")


class ToolProtocolV2Adapter:
    """Translate ergonomic V2 calls into unchanged frozen V1 tool requests.

    The adapter never weakens workspace safety. LAST_READ is resolved only from
    a successful READ_TEXT performed through this exact adapter instance and
    WorkspaceSession. The frozen dispatcher remains the mutation authority.
    """

    def __init__(
        self,
        session: WorkspaceSession,
        dispatcher: WorkspaceToolDispatcher | None = None,
    ) -> None:
        if not isinstance(session, WorkspaceSession):
            raise ValueError("session must be WorkspaceSession")
        self.session = session
        self.dispatcher = dispatcher or WorkspaceToolDispatcher()
        self.capabilities: V2ToolCapabilities = capabilities_for_session(session)
        self._last_reads: dict[str, LastReadRevision] = {}

    @property
    def guarded_paths(self) -> tuple[str, ...]:
        return tuple(sorted(self._last_reads, key=str.casefold))

    def has_last_read(self, path: str) -> bool:
        try:
            logical = normalize_relative_path(path)
        except WorkspacePathError:
            return False
        return logical in self._last_reads

    def _feedback(
        self,
        *,
        call_id: str,
        tool: V2ToolName,
        status: ToolStatus,
        fault_code: V2ToolFaultCode,
        recoverable: bool,
        output: str,
        metadata: Mapping[str, Any] | None = None,
        changed_paths: tuple[str, ...] = (),
        underlying_tool: ToolName | None = None,
        required_next_action: str | None = None,
    ) -> V2ToolFeedback:
        return V2ToolFeedback(
            call_id=call_id,
            tool=tool,
            underlying_tool=None if underlying_tool is None else underlying_tool.value,
            status=status,
            fault_code=fault_code,
            recoverable=recoverable,
            output=output,
            metadata={} if metadata is None else metadata,
            changed_paths=changed_paths,
            required_next_action=required_next_action,
        )

    @staticmethod
    def _safe_metadata(
        result: ToolResult,
        *,
        read_guard_ready: bool = False,
    ) -> dict[str, Any]:
        # SHA is intentionally internal to the adapter and never model-facing.
        metadata = {
            key: value
            for key, value in dict(result.metadata).items()
            if key != "sha256"
        }
        if read_guard_ready:
            metadata["guard_state"] = "LAST_READ_READY"
        return metadata

    def _normalized_path(
        self,
        call_id: str,
        call: V2ToolCall,
    ) -> tuple[str | None, V2ToolFeedback | None]:
        raw = call.arguments.get("path")
        try:
            return normalize_relative_path(raw), None
        except WorkspacePathError as exc:
            return None, self._feedback(
                call_id=call_id,
                tool=call.tool,
                status=ToolStatus.INVALID,
                fault_code=V2ToolFaultCode.INVALID_TOOL_REQUEST,
                recoverable=True,
                output=str(exc),
            )

    def _require_last_read(
        self,
        call_id: str,
        call: V2ToolCall,
    ) -> tuple[str | None, str | None, V2ToolFeedback | None]:
        logical, error = self._normalized_path(call_id, call)
        if error is not None or logical is None:
            return None, None, error
        revision = self._last_reads.get(logical)
        if revision is None:
            return (
                logical,
                None,
                self._feedback(
                    call_id=call_id,
                    tool=call.tool,
                    status=ToolStatus.INVALID,
                    fault_code=V2ToolFaultCode.LAST_READ_REQUIRED,
                    recoverable=True,
                    output=(
                        f"{call.tool.value} requires a successful READ_TEXT of "
                        f"{logical} in the current V2 tool session"
                    ),
                    required_next_action="READ_TEXT",
                ),
            )
        return logical, revision.sha256, None

    def _translate(
        self,
        call_id: str,
        call: V2ToolCall,
    ) -> tuple[ToolRequest | None, V2ToolFeedback | None, str | None]:
        if not self.capabilities.contains(call.tool):
            return (
                None,
                self._feedback(
                    call_id=call_id,
                    tool=call.tool,
                    status=ToolStatus.DENIED,
                    fault_code=V2ToolFaultCode.TOOL_NOT_AVAILABLE,
                    recoverable=False,
                    output=f"{call.tool.value} is not available in this session",
                ),
                None,
            )

        args = dict(call.arguments)
        if call.tool is V2ToolName.CREATE_TEXT:
            logical, error = self._normalized_path(call_id, call)
            if error is not None or logical is None:
                return None, error, logical
            request = ToolRequest(
                call_id,
                self.session.run_id,
                self.session.todo_id,
                ToolName.WRITE_TEXT,
                {"path": logical, "content": args["content"]},
            )
            return request, None, logical

        if call.tool in {
            V2ToolName.WRITE_TEXT,
            V2ToolName.REPLACE_TEXT,
            V2ToolName.DELETE_FILE,
        }:
            logical, digest, error = self._require_last_read(call_id, call)
            if error is not None or logical is None or digest is None:
                return None, error, logical
            translated: dict[str, Any] = {
                key: value
                for key, value in args.items()
                if key != "guard"
            }
            translated["path"] = logical
            translated["expected_sha256"] = digest
            request = ToolRequest(
                call_id,
                self.session.run_id,
                self.session.todo_id,
                _DIRECT_TO_V1[call.tool],
                translated,
            )
            return request, None, logical

        request = ToolRequest(
            call_id,
            self.session.run_id,
            self.session.todo_id,
            _DIRECT_TO_V1[call.tool],
            args,
        )
        return request, None, None

    def _map_result(
        self,
        call: V2ToolCall,
        result: ToolResult,
        logical_mutation_path: str | None,
    ) -> V2ToolFeedback:
        if result.status is ToolStatus.SUCCESS:
            if call.tool is V2ToolName.READ_TEXT:
                path = result.metadata.get("path")
                digest = result.metadata.get("sha256")
                if isinstance(path, str) and isinstance(digest, str):
                    self._last_reads[path] = LastReadRevision(path, digest)
                return self._feedback(
                    call_id=result.call_id,
                    tool=call.tool,
                    underlying_tool=result.tool,
                    status=result.status,
                    fault_code=V2ToolFaultCode.NONE,
                    recoverable=False,
                    output=result.output,
                    metadata=self._safe_metadata(
                        result, read_guard_ready=True
                    ),
                    changed_paths=result.changed_paths,
                )

            if logical_mutation_path is not None:
                self._last_reads.pop(logical_mutation_path, None)
            return self._feedback(
                call_id=result.call_id,
                tool=call.tool,
                underlying_tool=result.tool,
                status=result.status,
                fault_code=V2ToolFaultCode.NONE,
                recoverable=False,
                output=result.output,
                metadata=self._safe_metadata(result),
                changed_paths=result.changed_paths,
            )

        if logical_mutation_path is not None and result.status in {
            ToolStatus.CONFLICT,
            ToolStatus.NOT_FOUND,
        }:
            self._last_reads.pop(logical_mutation_path, None)

        if result.status is ToolStatus.DENIED:
            code = V2ToolFaultCode.WORKSPACE_POLICY_DENIED
            recoverable = True
            next_action = None
        elif result.status is ToolStatus.CONFLICT:
            if call.tool is V2ToolName.CREATE_TEXT:
                code = V2ToolFaultCode.CREATE_TARGET_EXISTS
            else:
                code = V2ToolFaultCode.STALE_OR_CONFLICTING_REVISION
            recoverable = True
            next_action = "READ_TEXT"
        elif result.status is ToolStatus.INVALID:
            code = V2ToolFaultCode.INVALID_TOOL_REQUEST
            recoverable = True
            next_action = None
        elif result.status is ToolStatus.NOT_FOUND:
            code = V2ToolFaultCode.NOT_FOUND
            recoverable = True
            next_action = None
        elif result.status is ToolStatus.TIMEOUT:
            code = V2ToolFaultCode.TIMEOUT
            recoverable = True
            next_action = None
        else:
            code = V2ToolFaultCode.EXECUTION_ERROR
            recoverable = True
            next_action = None

        return self._feedback(
            call_id=result.call_id,
            tool=call.tool,
            underlying_tool=result.tool,
            status=result.status,
            fault_code=code,
            recoverable=recoverable,
            output=result.output,
            metadata=self._safe_metadata(result),
            required_next_action=next_action,
        )

    def execute(self, call_id: str, call: V2ToolCall) -> V2ToolFeedback:
        if not isinstance(call_id, str) or not call_id.strip():
            raise ValueError("call_id must be non-empty string")
        if not isinstance(call, V2ToolCall):
            raise ValueError("call must be V2ToolCall")
        request, early, logical_mutation_path = self._translate(call_id, call)
        if early is not None:
            return early
        assert request is not None
        result = self.dispatcher.execute(self.session, request)
        return self._map_result(call, result, logical_mutation_path)
