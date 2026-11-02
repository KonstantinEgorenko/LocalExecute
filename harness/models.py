"""Configured model registry and runtime-model matching."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Iterable

from .config import ConfigLoader


class ModelStatus(StrEnum):
    CONFIGURED = "CONFIGURED"
    TAG_TODO = "TAG_TODO"
    SERVER_OFFLINE = "SERVER_OFFLINE"
    DISCOVERY_ERROR = "DISCOVERY_ERROR"
    UNLOADED = "UNLOADED"
    LOADED = "LOADED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class ServerDefinition:
    server_id: str
    ollama_base_url: str
    openai_base_url: str


@dataclass(frozen=True, slots=True)
class ModelDefinition:
    logical_id: str
    ollama_model: str
    server: str
    roles: tuple[str, ...]
    runtime: dict[str, Any]
    resource_class: str
    context: dict[str, Any]
    escalation_level: str
    resources: dict[str, Any]

    @property
    def model_tag_known(self) -> bool:
        return self.ollama_model.strip().upper() != "TODO"


@dataclass(frozen=True, slots=True)
class LoadedModel:
    name: str | None
    model: str | None
    size_bytes: int | None
    size_vram_bytes: int | None
    expires_at: str | None
    server_id: str
    context_length: int | None = None

    @property
    def size_gb(self) -> float | None:
        return None if self.size_bytes is None else self.size_bytes / 1024**3

    @property
    def vram_gb(self) -> float | None:
        return None if self.size_vram_bytes is None else self.size_vram_bytes / 1024**3

    @classmethod
    def from_api(cls, value: dict[str, Any], server_id: str) -> "LoadedModel":
        return cls(
            name=_optional_str(value.get("name")),
            model=_optional_str(value.get("model")),
            size_bytes=_optional_int(value.get("size")),
            size_vram_bytes=_optional_int(value.get("size_vram")),
            expires_at=_optional_str(value.get("expires_at")),
            server_id=server_id,
            context_length=_optional_int(value.get("context_length")),
        )


@dataclass(frozen=True, slots=True)
class ModelRuntimeStatus:
    logical_id: str
    configured: bool
    model_tag_known: bool
    server_online: bool
    discovery_ok: bool
    loaded: bool
    status: ModelStatus
    runtime_model: LoadedModel | None = None

    @property
    def size_gb(self) -> float | None:
        return self.runtime_model.size_gb if self.runtime_model else None

    @property
    def vram_gb(self) -> float | None:
        return self.runtime_model.vram_gb if self.runtime_model else None


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _normalise_tag(value: str) -> str:
    return value.strip().casefold()


class ModelRegistry:
    def __init__(self, servers: dict[str, ServerDefinition], models: dict[str, ModelDefinition]) -> None:
        self._servers = servers
        self._models = models

    @classmethod
    def from_loader(cls, loader: ConfigLoader) -> "ModelRegistry":
        document = loader.load_models()
        servers = {key: ServerDefinition(key, value["ollama_base_url"], value["openai_base_url"]) for key, value in document["servers"].items()}
        models = {key: ModelDefinition(key, value["ollama_model"], value["server"], tuple(value["roles"]), dict(value["runtime"]), value["resource_class"], dict(value["context"]), value["escalation_level"], dict(value["resources"])) for key, value in document["models"].items()}
        return cls(servers, models)

    @property
    def logical_ids(self) -> tuple[str, ...]:
        return tuple(self._models)

    @property
    def servers(self) -> tuple[ServerDefinition, ...]:
        return tuple(self._servers.values())

    def get(self, logical_id: str) -> ModelDefinition:
        return self._models[logical_id]

    def get_server(self, server_id: str) -> ServerDefinition:
        return self._servers[server_id]

    def match_loaded(self, loaded: LoadedModel) -> ModelDefinition | None:
        candidates = {_normalise_tag(tag) for tag in (loaded.name, loaded.model) if tag}
        for definition in self._models.values():
            if definition.server == loaded.server_id and definition.model_tag_known and _normalise_tag(definition.ollama_model) in candidates:
                return definition
        return None

    def status_for(self, logical_id: str, server_online: bool, discovery_ok: bool, loaded_models: Iterable[LoadedModel]) -> ModelRuntimeStatus:
        definition = self.get(logical_id)
        if not server_online: return ModelRuntimeStatus(logical_id, True, definition.model_tag_known, False, False, False, ModelStatus.SERVER_OFFLINE)
        if not discovery_ok: return ModelRuntimeStatus(logical_id, True, definition.model_tag_known, True, False, False, ModelStatus.DISCOVERY_ERROR)
        if not definition.model_tag_known: return ModelRuntimeStatus(logical_id, True, False, True, True, False, ModelStatus.TAG_TODO)
        runtime_model = next((item for item in loaded_models if self.match_loaded(item) == definition), None)
        return ModelRuntimeStatus(logical_id, True, True, True, True, runtime_model is not None, ModelStatus.LOADED if runtime_model else ModelStatus.UNLOADED, runtime_model)