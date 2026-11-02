'Profile-pinned native Ollama client for execution profiles.'
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from harness.errors import WorkerGenerationError, WorkerProtocolError
from harness.executor.client import OllamaNativeClient, OllamaTimeouts
from harness.executor.models import ExecutionMessage
from harness.executor.profiles import ExecutionProfile


@dataclass(frozen=True, slots=True)
class ProfileGeneration:
    model_tag: str
    content: str
    thinking: str | None
    prompt_eval_count: int
    eval_count: int
    done_reason: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.model_tag, str) or not self.model_tag.strip():
            raise ValueError("model_tag must be non-empty string")
        if not isinstance(self.content, str):
            raise ValueError("content must be string")
        if self.thinking is not None and not isinstance(self.thinking, str):
            raise ValueError("thinking must be str|None")
        for name in ("prompt_eval_count", "eval_count"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be integer >= 0")
        if self.done_reason is not None and not isinstance(self.done_reason, str):
            raise ValueError("done_reason must be str|None")


class ProfileOllamaClient(OllamaNativeClient):
    def __init__(self, timeouts: OllamaTimeouts | None = None) -> None:
        super().__init__(timeouts)

    def chat_profile(
        self,
        *,
        base_url: str,
        profile: ExecutionProfile,
        messages: tuple[ExecutionMessage, ...],
        response_schema: dict[str, Any],
    ) -> ProfileGeneration:
        if not isinstance(profile, ExecutionProfile):
            raise ValueError("profile must be ExecutionProfile")
        options = self._options(
            profile.context_tokens,
            profile.num_gpu,
            profile.temperature,
        )
        options["num_predict"] = profile.num_predict
        payload: dict[str, Any] = {
            "model": profile.model_tag,
            "messages": [message.to_dict() for message in messages],
            "stream": False,
            "keep_alive": -1,
            "options": options,
            "format": response_schema,
        }
        if profile.think is not None:
            payload["think"] = profile.think
        response = self._request(
            base_url,
            "/api/chat",
            timeout_seconds=profile.generation_timeout_seconds,
            payload=payload,
        )
        return self._parse_profile_response(response, profile)

    @staticmethod
    def _parse_profile_response(
        response: dict[str, Any], profile: ExecutionProfile
    ) -> ProfileGeneration:
        model = response.get("model")
        done = response.get("done")
        message = response.get("message")
        if model != profile.model_tag:
            raise WorkerProtocolError(
                f"profile-pinned model mismatch: expected={profile.model_tag!r} actual={model!r}"
            )
        if done is not True:
            raise WorkerGenerationError("profile worker response did not complete")
        if not isinstance(message, dict):
            raise WorkerProtocolError("profile /api/chat message must be object")
        content = message.get("content")
        thinking = message.get("thinking")
        if not isinstance(content, str):
            raise WorkerProtocolError("profile /api/chat content must be string")
        if thinking is not None and not isinstance(thinking, str):
            raise WorkerProtocolError("profile /api/chat thinking must be string|null")
        prompt_count = response.get("prompt_eval_count", 0)
        eval_count = response.get("eval_count", 0)
        if not isinstance(prompt_count, int) or isinstance(prompt_count, bool) or prompt_count < 0:
            raise WorkerProtocolError("prompt_eval_count must be integer >= 0")
        if not isinstance(eval_count, int) or isinstance(eval_count, bool) or eval_count < 0:
            raise WorkerProtocolError("eval_count must be integer >= 0")
        done_reason = response.get("done_reason")
        if done_reason is not None and not isinstance(done_reason, str):
            raise WorkerProtocolError("done_reason must be string|null")
        return ProfileGeneration(
            profile.model_tag,
            content,
            thinking,
            prompt_count,
            eval_count,
            done_reason,
        )

    def preload_profile(self, base_url: str, profile: ExecutionProfile) -> dict[str, Any]:
        return self.preload_model(
            base_url,
            profile.model_tag,
            profile.context_tokens,
            num_gpu=profile.num_gpu,
        )

    def unload_profile(self, base_url: str, profile: ExecutionProfile) -> dict[str, Any]:
        return self.unload_model(base_url, profile.model_tag)
