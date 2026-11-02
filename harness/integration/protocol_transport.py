'Additive production structured-output transport overlay for production integration.2.\n\nFrozen Judge and recovery coordinators continue to request generic ``json``.\nAt the production composition boundary this adapter replaces that transport\nhint with an exact request-derived JSON Schema. Strict parsers remain unchanged.\n'
from __future__ import annotations

import copy
import json

from ..protocol_transport import ollama_compatible_schema
from typing import Any

from .single_endpoint import SingleEndpointOrnithRuntime


def _request_payload(marker: str, user_prompt: str) -> dict[str, Any]:
    prefix = marker + "\n"
    if not isinstance(user_prompt, str) or not user_prompt.startswith(prefix):
        raise ValueError(f"expected {marker} request payload")
    value = json.loads(user_prompt[len(prefix):])
    if not isinstance(value, dict):
        raise ValueError(f"{marker} payload must be an object")
    return value


def structured_schema_for_prompt(
    *,
    system_prompt: str,
    user_prompt: str,
    response_schema: dict[str, Any] | str | None,
) -> dict[str, Any] | str | None:
    """Convert the frozen generic JSON hint into the exact request schema."""

    if user_prompt.startswith("JUDGE_REQUEST\n"):
        from ..judge.transport import project_judge_contract
        payload = _request_payload("JUDGE_REQUEST", user_prompt)
        if type(payload.get('response_contract_fingerprint')) is not str:
            raise ValueError('Judge schema fingerprint is required')
        schema = project_judge_contract(payload['response_contract'], payload['response_contract_fingerprint'])
        if response_schema != 'json' and response_schema != schema:
            raise ValueError('Judge requires the canonical structured-output contract')
        return schema

    if response_schema != "json":
        return response_schema

    if user_prompt.startswith("REPLAN_REQUEST\n"):
        payload = _request_payload("REPLAN_REQUEST", user_prompt)
        schema = ollama_compatible_schema(copy.deepcopy(payload["response_contract"]))
        mutable = sorted(set(payload["workspace_paths"]) - set(payload["immutable_paths"]))
        if not mutable:
            raise ValueError("replan structured output requires a mutable workspace path")
        schema["properties"]["source_task_id"] = {"type": "string", "enum": [payload["task_id"]]}
        schema["properties"]["target_files"]["items"] = {"type": "string", "enum": mutable}
        schema["properties"]["target_files"]["maxItems"] = min(
            int(payload["policy"]["max_target_files"]), len(mutable)
        )
        schema["properties"]["required_evidence"]["items"] = {
            "type": "string", "enum": sorted(payload["allowed_evidence_ids"]),
        }
        return schema

    return response_schema


def diagnosis_schema_for_messages(messages: tuple[Any, ...], response_schema: Any) -> Any:
    from ..recovery.transport import constrain_diagnosis_semantics
    if response_schema != "json":
        return response_schema
    user = next((item.content for item in messages if getattr(item, "role", None) == "user"), None)
    payload = _request_payload("FAILURE_DIAGNOSIS_REQUEST", user)
    schema = ollama_compatible_schema(copy.deepcopy(payload["response_contract"]))
    task = payload["task"]
    schema["properties"]["evidence"]["items"] = {
        "type": "string", "enum": sorted(task["allowed_evidence_ids"]),
    }
    paths = sorted(task["workspace_paths"])
    if paths:
        schema["properties"]["implicated_files"]["items"] = {
            "type": "string", "enum": paths,
        }
    else:
        schema["properties"]["implicated_files"]["maxItems"] = 0
    return constrain_diagnosis_semantics(schema)


class SchemaConstrainedOrnithRuntime:
    'Transparent wrapper used by production Judge and recovery Replan.'

    def __init__(self, inner: Any) -> None:
        self.inner = inner

    def complete(self, system_prompt: str, user_prompt: str, response_schema: Any = None):
        try:
            schema = structured_schema_for_prompt(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                response_schema=response_schema,
            )
        except (ValueError, KeyError, TypeError) as exc:
            if not user_prompt.startswith('JUDGE_REQUEST\n'):
                raise
            from ..lead.runtime import LeadModelError
            raise LeadModelError('Judge structured-output contract unavailable: ' + str(exc)) from exc
        return self.inner.complete(system_prompt, user_prompt, schema)


class SchemaConstrainedNativeClient:
    'Transparent native transport wrapper used by recovery Diagnosis.'

    def __init__(self, inner: Any) -> None:
        self.inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def chat(
        self,
        base_url: str,
        model_tag: str,
        messages: tuple[Any, ...],
        keep_alive: int | str,
        num_ctx: int,
        *,
        num_gpu: int | None = None,
        format_schema: dict[str, Any] | str | None = None,
        temperature: float | None = None,
    ) -> dict[str, Any]:
        schema = diagnosis_schema_for_messages(messages, format_schema)
        return self.inner.chat(
            base_url,
            model_tag,
            messages,
            keep_alive,
            num_ctx,
            num_gpu=num_gpu,
            format_schema=schema,
            temperature=temperature,
        )


def constrain_recovery_bridge(bridge: Any) -> Any:
    'Apply production transport adapters to a WorkflowRecoveryBridge.\n\n    recovery constructs its frozen OrnithReplanClient with the historical\n    OrnithRuntime, which rejects equal logical lead/worker URLs. Production\n    owns a single physical Ollama endpoint under the shared Phase20 lifecycle,\n    so upgrade only that runtime instance to the existing single-endpoint\n    compatibility adapter before applying structured-output constraints.\n    '

    bridge.diagnosis_client.native = SchemaConstrainedNativeClient(
        bridge.diagnosis_client.native
    )

    runtime = bridge.replan_client.runtime
    registry = runtime.registry
    lead_url = registry.get_server("lead").ollama_base_url.rstrip("/")
    worker_url = registry.get_server("worker").ollama_base_url.rstrip("/")

    if lead_url == worker_url and not isinstance(runtime, SingleEndpointOrnithRuntime):
        runtime = SingleEndpointOrnithRuntime(
            registry,
            runtime.locks,
            timeout_seconds=runtime.timeout_seconds,
            output_tokens=runtime.output_tokens,
            model_id=runtime.model_id,
        )

    bridge.replan_client.runtime = SchemaConstrainedOrnithRuntime(runtime)
    return bridge

# Verified compatibility aliases for historical Python interfaces.
constrain_phase15_bridge = constrain_recovery_bridge
