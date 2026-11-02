"""Runtime-only hardening for frozen OrnithRuntime calls."""
from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request

from .context import compact_synthesis_user_prompt
from .policy import RuntimeHardeningPolicy
from .structured import synthesis_json_schema, synthesis_output_instruction

_LOCK = threading.Lock()
_INSTALLED = False
_ORIGINAL = None
_WRAPPER = None
_OBSERVATION_ACTIVE = False
_POLICY: RuntimeHardeningPolicy | None = None
_METRICS = {
    "chat_requests": 0,
    "cpu_enforcements": 0,
    "json_thinking_disabled": 0,
    "synthesis_schema_enforcements": 0,
    "synthesis_output_bound_enforcements": 0,
    "timeout_extensions": 0,
    "max_effective_timeout_seconds": 0,
    "synthesis_compactions": 0,
    "max_original_synthesis_bytes": 0,
    "max_final_synthesis_bytes": 0,
    "evidence_dropped": 0,
    "sources_dropped": 0,
    "http_errors": 0,
    "last_http_error": "",
}


class RuntimeHardeningTransportError(OSError):
    pass


def reset_runtime_hardening_metrics() -> None:
    with _LOCK:
        for key in list(_METRICS):
            _METRICS[key] = "" if key == "last_http_error" else 0


def runtime_hardening_metrics() -> dict:
    with _LOCK:
        return dict(_METRICS)


def _record(**values) -> None:
    with _LOCK:
        for key, value in values.items():
            if key.startswith("max_"):
                _METRICS[key] = max(int(_METRICS[key]), int(value))
            elif key == "last_http_error":
                _METRICS[key] = str(value)
            else:
                _METRICS[key] = int(_METRICS[key]) + int(value)


def _harden_request(request: Request, policy: RuntimeHardeningPolicy) -> Request:
    url = getattr(request, "full_url", "")
    if not url.endswith("/api/chat") or request.data is None:
        return request

    payload = json.loads(request.data.decode("utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeHardeningTransportError("Ollama chat payload must be object")
    _record(chat_requests=1)

    options = payload.get("options")
    options = dict(options) if isinstance(options, dict) else {}

    if policy.require_cpu_execution:
        options["num_gpu"] = policy.expected_num_gpu
        _record(cpu_enforcements=1)

    payload["options"] = options

    if (
        policy.disable_thinking_for_json_contracts
        and payload.get("format") is not None
    ):
        payload["think"] = False
        _record(json_thinking_disabled=1)

    bounded_synthesis_prompt = None
    messages = payload.get("messages")
    if isinstance(messages, list):
        rebuilt = []
        for message in messages:
            if not isinstance(message, dict):
                rebuilt.append(message)
                continue

            item = dict(message)
            if item.get("role") == "user" and isinstance(item.get("content"), str):
                compacted = compact_synthesis_user_prompt(item["content"], policy)
                if compacted is not None:
                    item["content"] = compacted.user_prompt
                    bounded_synthesis_prompt = compacted.user_prompt
                    _record(
                        synthesis_compactions=1,
                        max_original_synthesis_bytes=compacted.original_bytes,
                        max_final_synthesis_bytes=compacted.final_bytes,
                        evidence_dropped=max(
                            0,
                            compacted.original_evidence - compacted.selected_evidence,
                        ),
                        sources_dropped=max(
                            0,
                            compacted.original_sources - compacted.selected_sources,
                        ),
                    )
            rebuilt.append(item)
        payload["messages"] = rebuilt

    if bounded_synthesis_prompt is not None and payload.get("format") is not None:
        schema = synthesis_json_schema(bounded_synthesis_prompt)
        if schema is not None:
            payload["format"] = schema
            _record(synthesis_schema_enforcements=1)

        instruction = synthesis_output_instruction(bounded_synthesis_prompt)
        if instruction is not None and isinstance(payload.get("messages"), list):
            adjusted = []
            added = False
            for message in payload["messages"]:
                if (
                    not added
                    and isinstance(message, dict)
                    and message.get("role") == "system"
                    and isinstance(message.get("content"), str)
                ):
                    item = dict(message)
                    item["content"] = item["content"].rstrip() + "\n\n" + instruction
                    adjusted.append(item)
                    added = True
                else:
                    adjusted.append(message)
            payload["messages"] = adjusted
            if added:
                _record(synthesis_output_bound_enforcements=1)

    return Request(
        url,
        data=json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8"),
        headers=dict(request.header_items()),
        method=request.get_method(),
    )


def _effective_timeout(request: Request, timeout, policy: RuntimeHardeningPolicy):
    url = getattr(request, "full_url", "")
    if not url.endswith("/api/chat"):
        return timeout

    requested = float(timeout) if timeout is not None else 0.0
    effective = max(requested, float(policy.lead_request_timeout_seconds))
    if effective > requested:
        _record(timeout_extensions=1)
    _record(max_effective_timeout_seconds=int(effective))
    return effective


def install_runtime_hardening(root: Path) -> None:
    global _INSTALLED, _ORIGINAL, _POLICY, _WRAPPER
    with _LOCK:
        if _INSTALLED:
            return

        from harness.lead import runtime as lead_runtime

        policy = RuntimeHardeningPolicy.from_project_root(Path(root))
        original = lead_runtime.urlopen

        def hardened_urlopen(request, timeout=None):
            hardened = _harden_request(request, policy)
            effective_timeout = _effective_timeout(hardened, timeout, policy)
            started=time.monotonic()
            try:
                return _ORIGINAL(hardened, timeout=effective_timeout)
            except HTTPError as exc:
                from ..transport_diagnostics import capture_http_error
                diagnostic,_=capture_http_error(exc,url=hardened.full_url,
                    payload=json.loads(hardened.data) if hardened.data else None,started=started,
                    causal_exception='RuntimeHardeningTransportError',request_bytes=hardened.data)
                message = f"HTTP {exc.code}: {diagnostic.bounded_body_preview}"
                _record(http_errors=1, last_http_error=message)
                error=RuntimeHardeningTransportError(message)
                error.transport_error=diagnostic
                raise error from exc

        _ORIGINAL = original
        _POLICY = policy
        lead_runtime.urlopen = hardened_urlopen
        _WRAPPER = hardened_urlopen
        _INSTALLED = True


@contextmanager
def observe_physical_http(observer):
    """Observe after production hardening, without replacing its public wrapper.

    The observer receives (request, effective_timeout, physical_dispatch).
    Sessions are exclusive; an altered hardening chain fails before dispatch.
    """
    global _ORIGINAL, _OBSERVATION_ACTIVE
    from harness.lead import runtime as lead_runtime
    with _LOCK:
        if not _INSTALLED or lead_runtime.urlopen is not _WRAPPER:
            raise RuntimeError("PHASE20R_HARDENING_CHAIN_MISMATCH")
        if _OBSERVATION_ACTIVE:
            raise RuntimeError("PHASE20R_OBSERVATION_ALREADY_ACTIVE")
        original = _ORIGINAL
        def observed(request, timeout=None):
            return observer(request, timeout, original)
        _ORIGINAL = observed
        _OBSERVATION_ACTIVE = True
    try:
        yield
    finally:
        with _LOCK:
            _ORIGINAL = original
            _OBSERVATION_ACTIVE = False


def uninstall_runtime_hardening() -> None:
    global _INSTALLED, _ORIGINAL, _POLICY
    with _LOCK:
        if not _INSTALLED:
            return

        from harness.lead import runtime as lead_runtime

        lead_runtime.urlopen = _ORIGINAL
        _ORIGINAL = None
        _POLICY = None
        _INSTALLED = False

# Verified legacy import/pickle aliases, not canonical public exports.
Phase20RTransportError = RuntimeHardeningTransportError
