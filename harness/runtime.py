"""Read-only Ollama runtime discovery CLI; it never invokes or manages models."""
from __future__ import annotations

import argparse
import json
import json as json_module
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from .config import ConfigLoader
from .errors import RuntimeDiscoveryError, ServerUnavailableError
from .models import LoadedModel, ModelRegistry
from .resource_manager.manager import ResourceManager


@dataclass(frozen=True, slots=True)
class ServerObservation:
    server_id: str
    online: bool
    discovery_ok: bool
    loaded_models: tuple[LoadedModel, ...] = ()
    error: str | None = None


class OllamaDiscoveryClient:
    def __init__(self, timeout_seconds: float = 3.0) -> None:
        self.timeout_seconds = timeout_seconds

    def get_ps(self, server_id: str, base_url: str) -> tuple[LoadedModel, ...]:
        url = base_url.rstrip("/") + "/api/ps"
        try:
            with urlopen(url, timeout=self.timeout_seconds) as response:
                raw_payload = response.read()
        except HTTPError as exc:
            raise RuntimeDiscoveryError(f"{server_id} reachable but /api/ps returned HTTP {exc.code}: {exc.reason}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise ServerUnavailableError(f"{server_id} unavailable at {url}: {exc}") from exc
        try:
            payload = json.loads(raw_payload.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise RuntimeDiscoveryError(f"{server_id} returned invalid /api/ps JSON: {exc}") from exc
        models = payload.get("models") if isinstance(payload, dict) else None
        if not isinstance(models, list):
            raise RuntimeDiscoveryError(f"{server_id} /api/ps response has no models list")
        loaded_models: list[LoadedModel] = []
        for index, entry in enumerate(models):
            if not isinstance(entry, dict):
                raise RuntimeDiscoveryError(f"{server_id} /api/ps models[{index}] must be an object")
            identifiers = (entry.get("name"), entry.get("model"))
            if not any(isinstance(value, str) and value.strip() for value in identifiers):
                raise RuntimeDiscoveryError(f"{server_id} /api/ps models[{index}] requires a non-empty name or model")
            loaded_models.append(LoadedModel.from_api(entry, server_id))
        return tuple(loaded_models)


def discover_servers(registry: ModelRegistry, client: OllamaDiscoveryClient | None = None) -> dict[str, ServerObservation]:
    client = client or OllamaDiscoveryClient()
    observations: dict[str, ServerObservation] = {}
    for server in registry.servers:
        try:
            models = client.get_ps(server.server_id, server.ollama_base_url)
            observations[server.server_id] = ServerObservation(server.server_id, True, True, models)
        except ServerUnavailableError as exc:
            observations[server.server_id] = ServerObservation(server.server_id, False, False, (), str(exc))
        except RuntimeDiscoveryError as exc:
            observations[server.server_id] = ServerObservation(server.server_id, True, False, (), str(exc))
    return observations


def build_manager(root: Path) -> ResourceManager:
    loader = ConfigLoader(root)
    return ResourceManager(ModelRegistry.from_loader(loader), loader.load_resources())


def _human_report(snapshot: object, registry: ModelRegistry) -> str:
    data = snapshot.to_dict()
    lines: list[str] = []
    for server_id in ("lead", "worker"):
        server = registry.get_server(server_id)
        online = data[f"{server_id}_online"]
        lines.extend([f"{server_id.upper()} SERVER {server.ollama_base_url}", "ONLINE" if online else "OFFLINE", ""])
    lines.append("MODELS")
    for status in data["model_statuses"]:
        line = f"{status['logical_id']}: {status['status']}"
        runtime_model = status["runtime_model"]
        if runtime_model:
            tag = runtime_model["name"] or runtime_model["model"] or "unknown-tag"
            line += f" ({tag}; size={runtime_model['size_bytes']}; vram={runtime_model['size_vram_bytes']})"
        lines.append(line)
    gpu = data["gpu"]
    lines.extend(["", "GPU", "UNAVAILABLE" if gpu is None else f"{gpu['name']} | VRAM total/used/free: {gpu['vram_total_gb']}/{gpu['vram_used_gb']}/{gpu['vram_free_gb']} GB"])
    ram = data["ram"]
    lines.extend(["", "SYSTEM RAM", "UNAVAILABLE" if ram is None else f"total/used/free: {ram['total_gb']}/{ram['used_gb']}/{ram['available_gb']} GB"])
    if data["discovery_errors"]:
        lines.extend(["", "DISCOVERY WARNINGS", *data["discovery_errors"]])
    return "\n".join(lines)


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only local swarm runtime discovery")
    parser.add_argument("--config-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    manager = build_manager(args.config_root)
    snapshot = manager.get_snapshot(discover_servers(manager.registry))
    print(json.dumps(snapshot.to_dict(), indent=2, sort_keys=True) if args.as_json else _human_report(snapshot, manager.registry))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())