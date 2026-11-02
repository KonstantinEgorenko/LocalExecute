"""Additive deterministic ACI feedback at the production tool boundary."""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Any

from ..context_manager import ArtifactKind, ArtifactOrigin
from ..context_manager.integration import ControlPlaneArtifactBridge
from ..control_plane import EvidenceStatus
from ..workspace.models import ToolName, ToolStatus

_MAX_VISIBLE_BYTES = 1536


def _visible(payload: dict[str, Any], *, max_bytes: int) -> str:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    data = text.encode("utf-8")[:max_bytes]
    while True:
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            data = data[:exc.start]


def _failed_tool_guidance(request, result) -> str | None:
    if request.tool is ToolName.SEARCH_TEXT and request.arguments.get("path") == "relative/path or empty":
        return (
            "The SEARCH_TEXT path value 'relative/path or empty' is descriptive placeholder text, not a valid path. "
            "Use path=\"\" for repository root or a real repository-relative path; do not copy the placeholder literally."
        )
    output = result.output.casefold()
    if request.tool is ToolName.WRITE_TEXT:
        if "expected_sha256 must be omitted when creating a new file" in output:
            return "Active V1 has no CREATE_TEXT. Create a new file with WRITE_TEXT and omit expected_sha256 entirely."
        if "overwriting an existing file requires expected_sha256" in output or "expected_sha256 does not match current file" in output:
            return (
                "READ_TEXT the SAME path again, copy metadata.sha256 from that NEW successful read exactly, "
                "and use it only for that same path. Never invent a SHA or use a SHA read from another path."
            )
    if request.tool is ToolName.REPLACE_TEXT and ("expected_sha256" in output or "old_text occurrence count mismatch" in output):
        return (
            "READ_TEXT the same file again; copy the fresh same-path metadata.sha256 and exact current old_text "
            "from that new read. Use a small unique current fragment, never invent hashes, and do not perform a no-op edit."
        )
    return None


def _search_text_no_match_guidance(request, result) -> str | None:
    if request.tool is not ToolName.SEARCH_TEXT:
        return None
    match_count = result.metadata.get("match_count")
    if not isinstance(match_count, int) or isinstance(match_count, bool) or match_count != 0:
        return None
    return (
        "SEARCH_TEXT found no matches. Do not repeat the same discovery call; "
        "use a concrete symbol or known path, LIST_FILES for bounded orientation, "
        "or READ_TEXT a grounded file before retrying search."
    )

def _failure_message(guidance: str) -> str:
    data = ("\nACI_TOOL_FAILURE_GUIDANCE_V1\n" + guidance).encode("utf-8")[:_MAX_VISIBLE_BYTES]
    while True:
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            data = data[:exc.start]

class ProductionAciFeedbackDispatcher:
    """Decorates the existing dispatcher; it neither verifies nor mutates itself."""

    def __init__(self, inner: Any, *, run_store, artifact_store) -> None:
        self.inner, self.run_store = inner, run_store
        self._bridge = ControlPlaneArtifactBridge(run_store, artifact_store)

    def _events(self, run_id: str, todo_id: str):
        return [event for event in self.run_store.list_events(run_id) if event.event_type.value == "EVIDENCE_RECORDED" and event.to_dict()["payload"].get("todo_id") == todo_id]

    def _record(self, run_id: str, todo_id: str, evidence_type: str, identity: str, payload: dict[str, Any]) -> None:
        content = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        self._bridge.record_text_evidence(run_id, artifact_id=f"{run_id}:{identity}:artifact", todo_id=todo_id,
            evidence_id=f"{run_id}:{identity}", evidence_type=evidence_type, source="ProductionAciFeedback",
            content=content, kind=ArtifactKind.CONTROL_PLANE_EVIDENCE, origin=ArtifactOrigin.CONTROL_PLANE,
            media_type="application/json", status=EvidenceStatus.PASSED, evidence_payload=payload,
            created_at="1970-01-01T00:00:00+00:00")

    def execute(self, session, request):
        result = self.inner.execute(session, request)
        if result.status is not ToolStatus.SUCCESS:
            guidance = _failed_tool_guidance(request, result)
            return result if guidance is None else replace(result, output=result.output + _failure_message(guidance))
        guidance = _search_text_no_match_guidance(request, result)
        if guidance is not None:
            return replace(result, output=result.output + "\nACI_SEARCH_TEXT_GUIDANCE_V1\n" + guidance)
        events = self._events(request.run_id, request.todo_id)
        if request.tool in {ToolName.WRITE_TEXT, ToolName.REPLACE_TEXT, ToolName.DELETE_FILE} and result.changed_paths:
            epoch = events[-1].sequence + 1 if events else 0
            payload = {"schema_version": 1, "run_id": request.run_id, "todo_id": request.todo_id,
                       "tool_call_id": request.call_id, "tool_name": request.tool.value, "status": result.status.value,
                       "changed_paths": sorted(result.changed_paths), "mutation_epoch": epoch,
                       "path_identity_hashes": {path: hashlib.sha256(path.encode()).hexdigest() for path in sorted(result.changed_paths)}}
            payload["fingerprint"] = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            self._record(request.run_id, request.todo_id, "ACI_MUTATION_FEEDBACK", f"aci-mutation:{request.call_id}", payload)
            instruction = (
                "Mutation is already applied. Successful WRITE is authoritative. "
                "Do not reread changed files merely to confirm; take a distinct required step, "
                "a necessary targeted check, configured verification when appropriate, or FINAL only when the Todo is complete."
            )
            prefix = "\nACI_MUTATION_FEEDBACK_V1\n"
            detail_limit = _MAX_VISIBLE_BYTES - len((prefix + "\n" + instruction).encode("utf-8"))
            message = prefix + _visible(payload, max_bytes=max(0, detail_limit)) + "\n" + instruction
            return replace(result, output=result.output + message)
        if request.tool is ToolName.READ_TEXT:
            path = request.arguments.get("path")
            if not isinstance(path, str):
                return result
            latest = None
            for event in events:
                payload = event.to_dict()["payload"]
                if payload.get("evidence_type") == "ACI_MUTATION_FEEDBACK":
                    data = payload.get("payload", {})
                    if isinstance(data, dict) and path in data.get("changed_paths", []):
                        latest = data
            if latest is None:
                return result
            epoch = latest["mutation_epoch"]
            guidance_id = f"aci-guidance:{epoch}:{hashlib.sha256(path.encode()).hexdigest()[:16]}"
            if any(event.to_dict()["payload"].get("evidence_id") == f"{request.run_id}:{guidance_id}" for event in events):
                return result
            payload = {"schema_version": 1, "run_id": request.run_id, "todo_id": request.todo_id,
                       "path": path, "mutation_epoch": epoch, "trigger_call_id": request.call_id}
            payload["fingerprint"] = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            self._record(request.run_id, request.todo_id, "ACI_POST_MUTATION_GUIDANCE", guidance_id, payload)
            message = "\nPOST_MUTATION_GUIDANCE_V1\nThis path was already changed successfully and no later mutation changed it. Do not repeat confirmation reads; choose a distinct action or FINAL only if the Todo is complete."
            return replace(result, output=result.output + message)
        return result
