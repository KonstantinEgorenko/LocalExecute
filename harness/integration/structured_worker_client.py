'Production compatibility for strict structured worker generations.\n\ngpu coordination and model runtime remain the runtime/lifecycle authorities.  This client\ndoes not override chat(); it modifies only the final strict-JSON /api/chat\npayload before delegating through the currently installed base _request chain.\n'

from __future__ import annotations

from typing import Any

from ..executor import OllamaNativeClient


class ProductionStructuredWorkerClient(OllamaNativeClient):
    """Apply strict-JSON generation controls without bypassing runtime wrappers."""

    STRICT_JSON_OVERRIDES: dict[str, dict[str, Any]] = {
        "hf.co/xhxlb/IQuest-Coder-V1-14B-Instruct-GGUF:Q4_K_M": {
            "think": False,
            "num_predict": 1024,
        },
        "qwen36-35b-a3b-heavy-fast:latest": {
            "think": False,
            "num_predict": 1024,
        },
    }

    def _request(
        self,
        base_url: str,
        path: str,
        *,
        timeout_seconds: float,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        effective_payload = payload

        if (
            path == "/api/chat"
            and isinstance(payload, dict)
            and payload.get("format") is not None
        ):
            model_tag = payload.get("model")
            override = self.STRICT_JSON_OVERRIDES.get(model_tag)

            if override is not None:
                effective_payload = dict(payload)

                raw_options = payload.get("options")
                options = (
                    dict(raw_options)
                    if isinstance(raw_options, dict)
                    else {}
                )

                effective_payload["options"] = options
                effective_payload["think"] = bool(
                    override["think"]
                )
                options["num_predict"] = int(
                    override["num_predict"]
                )

        return super()._request(
            base_url,
            path,
            timeout_seconds=timeout_seconds,
            payload=effective_payload,
        )
