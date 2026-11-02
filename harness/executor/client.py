"""Native Ollama client used exclusively by executor and lifecycle code."""
from __future__ import annotations

import json
import hashlib
import math
import time
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ..errors import ExecutionHttpError, ExecutionServerUnavailableError, WorkerProtocolError
from ..models import LoadedModel
from .models import ExecutionMessage


@dataclass(frozen=True, slots=True)
class OllamaTimeouts:
    discovery_timeout_seconds: float = 5.0
    lifecycle_timeout_seconds: float = 300.0
    generation_timeout_seconds: float = 900.0

    def __post_init__(self) -> None:
        values = (self.discovery_timeout_seconds, self.lifecycle_timeout_seconds, self.generation_timeout_seconds)
        if any(not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value <= 0 for value in values):
            raise ValueError("Ollama timeouts must be finite positive values")

    @classmethod
    def from_policies(cls, policies: dict[str, Any]) -> "OllamaTimeouts":
        values = policies.get("executor", {})
        return cls(float(values.get("discovery_timeout_seconds", 5.0)), float(values.get("lifecycle_timeout_seconds", 300.0)), float(values.get("generation_timeout_seconds", 900.0)))


class OllamaNativeClient:
    def __init__(self, timeouts: OllamaTimeouts | None = None) -> None:
        self.timeouts = timeouts or OllamaTimeouts()

    def qualify_prompt_tokens(self, payload):
        """Opt-in TEST repair admission only; no transport or residency changes."""
        from ..integration.test_prompt_budget import qualify_local
        return qualify_local(payload,getattr(self,'_observed_digests',{}))

    def _request(self, base_url: str, path: str, *, timeout_seconds: float, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        from ..supervision.transport import begin
        from ..supervision.errors import SupervisionObservationHalt
        try:
            observation = begin(base_url, path, payload)
        except Exception as exc:
            raise SupervisionObservationHalt('DISPATCH_INTENT', exc) from exc
        if observation is not None:
            try:
                observation.entered()
            except Exception as exc:
                raise SupervisionObservationHalt('TRANSPORT_ENTRY', exc,
                    identity=observation.identity) from exc
        try:
            value, raw = self._request_response(base_url, path, timeout_seconds=timeout_seconds, payload=payload)
        except Exception as exc:
            if observation is not None:
                try:
                    observation.error(exc)
                except Exception as publication:
                    raise SupervisionObservationHalt('TRANSPORT_ERROR_RECEIPT', publication,
                        identity=observation.identity, transport_error_type=type(exc).__name__) from exc
            raise
        if observation is not None:
            try:
                observation.response(raw, value)
            except Exception as exc:
                import hashlib
                raise SupervisionObservationHalt('RESPONSE_RECEIPT', exc,
                    identity=observation.identity, response_received=True,
                    response_fingerprint=hashlib.sha256(raw).hexdigest()) from exc
        return value

    def _request_response(self, base_url: str, path: str, *, timeout_seconds: float, payload: dict[str, Any] | None = None):
        from ..transport_diagnostics import capture_http_error, record, safe_text
        url = base_url.rstrip("/") + path
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(url, data=data, method="POST" if payload is not None else "GET", headers={"Content-Type": "application/json"} if data else {})
        started = time.monotonic()
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                raw = response.read()
        except HTTPError as exc:
            diagnostic, legacy = capture_http_error(exc,url=url,payload=payload,started=started,request_bytes=data,
                model_digest=getattr(self,'_observed_digests',{}).get((base_url,(payload or {}).get('model'))))
            raise ExecutionHttpError(f"Ollama HTTP {exc.code} at {path}: {diagnostic.reason}",
                status_code=exc.code,transport_error=diagnostic,**legacy) from exc
        except (URLError, TimeoutError, OSError) as exc:
            error = ExecutionServerUnavailableError(f"Ollama unavailable at {path}: {safe_text(exc)}")
            reason = getattr(exc,'reason',exc)
            error.transport_error = record(category='TRANSPORT_TIMEOUT' if isinstance(reason,TimeoutError) else 'TRANSPORT_CONNECTION_ERROR',
                url=url,payload=payload,started=started,causal_exception=type(error).__name__,request_bytes=data)
            raise error from exc
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            error = WorkerProtocolError(f"Malformed JSON response from {url}: {exc}")
            error.transport_error = record(category='MODEL_RESPONSE_PARSE_ERROR',url=url,payload=payload,
                started=started,causal_exception=type(error).__name__,status_code=200,request_bytes=data,
                body_sha256=hashlib.sha256(raw).hexdigest(),body_bytes=len(raw),body_hash_scope='FULL')
            raise error from exc
        if not isinstance(value, dict):
            raise WorkerProtocolError(f"Response from {url} must be an object")
        return value, raw

    def get_loaded_models(self, server_id: str, base_url: str) -> tuple[LoadedModel, ...]:
        response = self._request(base_url, "/api/ps", timeout_seconds=self.timeouts.discovery_timeout_seconds)
        models = response.get("models")
        if not isinstance(models, list):
            raise WorkerProtocolError("/api/ps response requires models list")
        parsed: list[LoadedModel] = []
        for index, item in enumerate(models):
            if not isinstance(item, dict):
                raise WorkerProtocolError(f"/api/ps models[{index}] must be an object")
            if not any(isinstance(item.get(key), str) and item[key].strip() for key in ("name", "model")):
                raise WorkerProtocolError(f"/api/ps models[{index}] requires name or model")
            parsed.append(LoadedModel.from_api(item, server_id))
        self._observed_digests = {key:value for key,value in getattr(self,'_observed_digests',{}).items() if key[0]!=base_url}
        self._observed_digests.update({(base_url,item.get('name') or item.get('model')):item['digest']
            for item in models if isinstance(item.get('digest'),str) and len(item['digest'])==64
            and all(c in '0123456789abcdef' for c in item['digest'])})
        return tuple(parsed)

    @staticmethod
    def _options(num_ctx: int, num_gpu: int | None, temperature: float | None = None) -> dict[str, int | float]:
        options: dict[str, int | float] = {"num_ctx": num_ctx}
        if num_gpu is not None:
            options["num_gpu"] = num_gpu
        if temperature is not None:
            options["temperature"] = temperature
        return options

    def preload_model(self, base_url: str, model_tag: str, num_ctx: int, *, num_gpu: int | None = None) -> dict[str, Any]:
        return self._request(base_url, "/api/generate", timeout_seconds=self.timeouts.lifecycle_timeout_seconds, payload={"model": model_tag, "keep_alive": -1, "stream": False, "options": self._options(num_ctx, num_gpu)})

    def unload_model(self, base_url: str, model_tag: str) -> dict[str, Any]:
        return self._request(base_url, "/api/generate", timeout_seconds=self.timeouts.lifecycle_timeout_seconds, payload={"model": model_tag, "keep_alive": 0, "stream": False})

    def chat(
        self,
        base_url: str,
        model_tag: str,
        messages: tuple[ExecutionMessage, ...],
        keep_alive: int | str,
        num_ctx: int,
        *,
        num_gpu: int | None = None,
        format_schema: dict[str, Any] | str | None = None,
        temperature: float | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model_tag,
            "messages": [message.to_dict() for message in messages],
            "stream": False,
            "keep_alive": keep_alive,
            "options": self._options(num_ctx, num_gpu, temperature),
        }
        if format_schema is not None:
            payload["format"] = format_schema
        return self._request(
            base_url,
            "/api/chat",
            timeout_seconds=self.timeouts.generation_timeout_seconds,
            payload=payload,
        )
