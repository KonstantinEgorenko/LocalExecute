'Universal model lifecycle orchestration layered above frozen gpu coordination + gpu acceleration.\n\nLock order is authoritative: MODEL_RUNTIME -> GPU_HEAVY.  MODEL_RUNTIME spans the\nwhole model call/profile transaction, while gpu coordination remains the exclusive GPU\nallocator. Known Swarm models may be unloaded during a transition; unknown/manual\nmodels are observation-only and never mutated.\n'
from __future__ import annotations
from harness.compatibility.configuration import resolve_config_path, normalize_configuration_document

import copy
import json
import math
import os
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Iterator
from urllib.request import Request, urlopen

from harness.config import ConfigLoader
from harness.errors import LifecycleVerificationError, StaleRoutingDecisionError
from harness.models import ModelDefinition, ModelRegistry
from harness.runtime_resources.gpu_coordination.guard import current_lease
from harness.runtime_resources.gpu_coordination.lease import GpuHeavyLease, GpuHeavyLeaseTimeout
from harness.runtime_resources.gpu_coordination.policy import GpuCoordinationPolicy

from .policy import ModelLifecyclePolicy
from harness.model_vram import MEMORY_POLICY, MODEL_VRAM_BUDGET_MIB, require_model_capacity, resident_budget
from .telemetry import MemoryProbe, MemoryTelemetry, RuntimeJournal

_INSTALLED = False
_CONTROLLER: "ModelRuntimeController | None" = None
_PROFILE_STORE: "RuntimeProfileStore | None" = None
_ORIGINALS: dict[str, Any] = {}


class ModelRuntimeError(RuntimeError):
    pass


class ModelRuntimeBlocker(ModelRuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class ResidentModel:
    base_url: str
    server_id: str
    name: str
    size_bytes: int
    size_vram_bytes: int
    expires_at: str | None
    logical_id: str | None
    known: bool

    @property
    def gpu_resident(self) -> bool:
        return self.size_vram_bytes > 0



class RuntimeProfileStore:
    """Persistent, measured runtime overlays without editing frozen model config."""

    SCHEMA_VERSION = 1

    def __init__(self, root: str | Path, policy: ModelLifecyclePolicy) -> None:
        self.root = Path(root).resolve()
        self.policy = policy
        self.path = policy.cache_path(self.root)
        self._lock = threading.RLock()
        self._document: dict[str, Any] = self._load()
        self._execution_profiles: tuple[Any, ...] | None = None

    def _empty(self) -> dict[str, Any]:
        return {"schema_version": self.SCHEMA_VERSION, "models": {}}

    def _load(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return self._empty()
        if not isinstance(raw, dict) or raw.get("schema_version") != self.SCHEMA_VERSION or not isinstance(raw.get("models"), dict):
            return self._empty()
        return raw

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)

    def get(self, logical_id: str) -> dict[str, Any] | None:
        with self._lock:
            entry = self._document.get("models", {}).get(logical_id)
            return copy.deepcopy(entry) if isinstance(entry, dict) else None

    def remove(self, logical_id: str) -> None:
        with self._lock:
            models = self._document.setdefault("models", {})
            if logical_id in models:
                del models[logical_id]
                self._write()

    def set_resolution(self, logical_id: str, model_tag: str) -> None:
        with self._lock:
            models = self._document.setdefault("models", {})
            entry = dict(models.get(logical_id) or {})
            if entry.get("model_tag") != model_tag:
                # A tag change invalidates any measured placement.
                entry = {"model_tag": model_tag}
            else:
                entry["model_tag"] = model_tag
            models[logical_id] = entry
            self._write()

    def set_calibrated(
        self,
        logical_id: str,
        *,
        model_tag: str,
        num_gpu: int,
        context_default: int,
        observed_vram_mib: float,
        admission_vram_mib: float,
        free_after_load_mib: float,
        gpu_total_mib: float | None,
        attempts: list[dict[str, Any]],
    ) -> None:
        with self._lock:
            self._document.setdefault("models", {})[logical_id] = {
                "model_tag": model_tag,
                "status": "CALIBRATED",
                "num_gpu": int(num_gpu),
                "context_default": int(context_default),
                "observed_vram_mib": round(float(observed_vram_mib), 3),
                "admission_vram_mib": round(float(admission_vram_mib), 3),
                "free_after_load_mib": round(float(free_after_load_mib), 3),
                "gpu_total_mib": None if gpu_total_mib is None else round(float(gpu_total_mib), 3),
                "min_free_vram_mib": self.policy.min_free_vram_mib,
                "max_worker_vram_mib": self.policy.max_worker_vram_mib,
                "attempts": copy.deepcopy(attempts),
                "updated_at_unix": time.time(),
            }
            self._write()

    def _cache_memory_valid(self, entry: dict[str, Any]) -> bool:
        if self.policy.memory_policy == MEMORY_POLICY:
            value = entry.get("observed_vram_mib")
            return type(value) in (int, float) and 0 < value <= MODEL_VRAM_BUDGET_MIB
        return int(entry.get("min_free_vram_mib") or -1) == self.policy.min_free_vram_mib

    def _placement_valid_for_document(self, entry: dict[str, Any], model: dict[str, Any]) -> bool:
        return (
            entry.get("status") == "CALIBRATED"
            and isinstance(entry.get("num_gpu"), int)
            and not isinstance(entry.get("num_gpu"), bool)
            and int(entry["num_gpu"]) >= self.policy.calibration_min_num_gpu
            and isinstance(entry.get("admission_vram_mib"), (int, float))
            and not isinstance(entry.get("admission_vram_mib"), bool)
            and int(entry.get("context_default") or 0) == int((model.get("context") or {}).get("default") or -1)
            and self._cache_memory_valid(entry)
            and int(entry.get("max_worker_vram_mib") or -1) == self.policy.max_worker_vram_mib
        )

    def execution_profiles(self) -> tuple[Any, ...]:
        if self._execution_profiles is None:
            execution_policy_path = resolve_config_path(self.root / "config" / "phase13.yaml")
            profiles_path = self.root / "config" / "execution_profiles_v2.yaml"
            if not execution_policy_path.is_file() or not profiles_path.is_file():
                self._execution_profiles = ()
            else:
                from harness.executor.profiles import ExecutionProfileRegistry
                registry = ExecutionProfileRegistry.from_project_root(self.root)
                self._execution_profiles = tuple(registry.get(pid) for pid in registry.profile_ids)
        return self._execution_profiles

    def _merge_execution_profile_targets(self, result: dict[str, Any]) -> None:
        models = result.get("models")
        if not isinstance(models, dict):
            return
        for profile in self.execution_profiles():
            logical_id = str(profile.model_id)
            if logical_id in models:
                model = models[logical_id]
                if isinstance(model, dict) and str(model.get("ollama_model", "")).strip().upper() == "TODO":
                    model["ollama_model"] = profile.model_tag
                continue
            # Router V2 may intentionally own a profile that has no Router V1
            # logical model (currently qwen38_heavy_fast). Add an in-memory-only
            # model target so model runtime can protect/calibrate its lifecycle too.
            models[logical_id] = {
                "ollama_model": profile.model_tag,
                "roles": ["phase13_execution_profile"],
                "server": profile.server_id,
                "context": {
                    "supported_max": profile.context_tokens,
                    "default": profile.context_tokens,
                    "operational_max": profile.context_tokens,
                },
                "resource_class": "gpu_worker_cpu_offload",
                "escalation_level": "phase13_profile",
                "runtime": {
                    "backend": "ollama",
                    "execution": "gpu_worker",
                    "persistent": False,
                    "cpu_ram_offload_allowed": True,
                    **({} if profile.num_gpu is None else {"num_gpu": profile.num_gpu}),
                },
                "resources": {
                    "requires_gpu": True,
                    "estimated_vram_gb": profile.estimated_vram_gb,
                    "heavy_cpu_offload": True,
                },
            }

    def apply_document(self, document: dict[str, Any]) -> dict[str, Any]:
        """Return an in-memory overlay; the source YAML remains byte-for-byte frozen."""
        with self._lock:
            result = copy.deepcopy(document)
            self._merge_execution_profile_targets(result)
            models = result.get("models")
            if not isinstance(models, dict):
                return result
            cached = self._document.get("models", {})
            for logical_id, entry in cached.items():
                if not isinstance(entry, dict) or logical_id not in models:
                    continue
                model = models[logical_id]
                if not isinstance(model, dict):
                    continue
                tag = entry.get("model_tag")
                if isinstance(tag, str) and tag.strip():
                    model["ollama_model"] = tag.strip()
                if self._placement_valid_for_document(entry, model):
                    runtime = dict(model.get("runtime") or {})
                    resources = dict(model.get("resources") or {})
                    runtime["num_gpu"] = int(entry["num_gpu"])
                    resources["estimated_vram_gb"] = float(entry["admission_vram_mib"]) / 1024.0
                    resources["phase20v_observed_vram_gb"] = float(entry.get("observed_vram_mib") or 0.0) / 1024.0
                    model["runtime"] = runtime
                    model["resources"] = resources
            return result


def _normalised_tag_text(value: str) -> str:
    return " ".join(part for part in re.sub(r"[^a-z0-9]+", " ", value.casefold()).split() if part)


def _install_config_overlay(store: RuntimeProfileStore) -> None:
    'Make future registries see measured model runtime profiles without editing YAML.'
    if "ConfigLoader.load_models.model_runtime" in _ORIGINALS:
        return
    original = ConfigLoader.load_models
    _ORIGINALS["ConfigLoader.load_models.model_runtime"] = original

    def load_models(self):
        return store.apply_document(original(self))

    ConfigLoader.load_models = load_models

def _install_execution_profile_overlay(store: RuntimeProfileStore) -> None:
    """Apply the same measured placement to Router V2 execution profiles."""
    key = "ExecutionProfileRegistry.from_project_root.model_runtime"
    if key in _ORIGINALS:
        return
    from harness.executor.profiles import ExecutionProfileRegistry
    original = ExecutionProfileRegistry.from_project_root.__func__
    _ORIGINALS[key] = original

    @classmethod
    def from_project_root(cls, root):
        current = original(cls, root)
        profiles = {}
        for profile_id in current.profile_ids:
            profile = current.get(profile_id)
            entry = store.get(profile.model_id)
            if isinstance(entry, dict):
                changes: dict[str, Any] = {}
                tag = entry.get("model_tag")
                if isinstance(tag, str) and tag.strip():
                    changes["model_tag"] = tag.strip()
                if (
                    entry.get("status") == "CALIBRATED"
                    and isinstance(entry.get("num_gpu"), int)
                    and isinstance(entry.get("admission_vram_mib"), (int, float))
                ):
                    changes["num_gpu"] = int(entry["num_gpu"])
                    changes["estimated_vram_gb"] = float(entry["admission_vram_mib"]) / 1024.0
                if changes:
                    profile = replace(profile, **changes)
            profiles[profile_id] = profile
        return cls(current.policy, profiles)

    ExecutionProfileRegistry.from_project_root = from_project_root

class OllamaRuntimeBackend:
    def __init__(self, timeout_seconds: float = 120.0, poll_interval_seconds: float = 0.2) -> None:
        self.timeout_seconds = timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds

    @staticmethod
    def _json(base_url: str, path: str, payload: dict[str, Any] | None = None, *, timeout: float) -> dict[str, Any]:
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = Request(
            base_url.rstrip("/") + path,
            data=data,
            method="GET" if data is None else "POST",
            headers={} if data is None else {"Content-Type": "application/json"},
        )
        with urlopen(req, timeout=timeout) as response:
            raw = json.loads(response.read().decode("utf-8"))
        if not isinstance(raw, dict):
            raise ModelRuntimeError(f"non-object Ollama response: {base_url}{path}")
        return raw

    def ps(self, base_url: str, *, optional: bool = False) -> tuple[dict[str, Any], ...]:
        try:
            raw = self._json(base_url, "/api/ps", timeout=5.0)
        except Exception:
            if optional:
                return ()
            raise
        return tuple(x for x in raw.get("models", ()) if isinstance(x, dict))

    def tags(self, base_url: str, *, optional: bool = False) -> tuple[str, ...]:
        try:
            raw = self._json(base_url, "/api/tags", timeout=10.0)
        except Exception:
            if optional:
                return ()
            raise
        tags = []
        for item in raw.get("models", ()):
            if isinstance(item, dict):
                name = item.get("name") or item.get("model")
                if isinstance(name, str) and name.strip():
                    tags.append(name.strip())
        return tuple(tags)

    def show(self, base_url: str, model_tag: str) -> dict[str, Any]:
        return self._json(
            base_url, "/api/show", {"model": model_tag}, timeout=max(10.0, self.timeout_seconds)
        )

    def resident(self, base_url: str, model_tag: str) -> dict[str, Any] | None:
        for item in self.ps(base_url, optional=True):
            if str(item.get("name") or item.get("model") or "") == model_tag:
                return item
        return None

    def block_count(self, base_url: str, model_tag: str) -> int | None:
        try:
            raw = self.show(base_url, model_tag)
        except Exception:
            return None
        info = raw.get("model_info")
        if not isinstance(info, dict):
            return None
        values: list[int] = []
        for key, value in info.items():
            if not isinstance(key, str) or not key.endswith(".block_count"):
                continue
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                continue
            if parsed > 0:
                values.append(parsed)
        return max(values) if values else None

    def unload(self, base_url: str, model_tag: str) -> None:
        if not any((x.get("name") or x.get("model")) == model_tag for x in self.ps(base_url, optional=True)):
            return
        self._json(
            base_url, "/api/generate",
            {"model": model_tag, "stream": False, "keep_alive": 0},
            timeout=self.timeout_seconds,
        )
        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            if not any((x.get("name") or x.get("model")) == model_tag for x in self.ps(base_url, optional=True)):
                return
            time.sleep(self.poll_interval_seconds)
        raise ModelRuntimeError(f"unload verification timeout: {model_tag}")

    def preload(self, base_url: str, model_tag: str, num_ctx: int, num_gpu: int | None) -> dict[str, Any]:
        options: dict[str, Any] = {"num_ctx": int(num_ctx)}
        if num_gpu is not None:
            options["num_gpu"] = int(num_gpu)
        return self._json(
            base_url, "/api/generate",
            {"model": model_tag, "prompt": "", "stream": False, "keep_alive": -1, "options": options},
            timeout=self.timeout_seconds,
        )

    def chat_probe(self, base_url: str, model_tag: str, num_ctx: int, num_gpu: int | None, keep_alive: str) -> dict[str, Any]:
        options: dict[str, Any] = {"num_ctx": int(num_ctx), "num_predict": 24, "temperature": 0}
        if num_gpu is not None:
            options["num_gpu"] = int(num_gpu)
        return self._json(
            base_url, "/api/chat",
            {
                "model": model_tag,
                "messages": [{"role": "user", "content": "Reply with exactly: PHASE20V_OK"}],
                "stream": False,
                "think": False,
                "keep_alive": keep_alive,
                "options": options,
            },
            timeout=max(self.timeout_seconds, 600.0),
        )


class ModelRuntimeController:
    def __init__(
        self,
        root: str | Path,
        *,
        policy: ModelLifecyclePolicy | None = None,
        registry: ModelRegistry | None = None,
        runtime_lease: GpuHeavyLease | None = None,
        gpu_lease: GpuHeavyLease | None = None,
        backend: OllamaRuntimeBackend | None = None,
        memory_probe: MemoryProbe | None = None,
        journal: RuntimeJournal | None = None,
        profile_store: RuntimeProfileStore | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.policy = policy or ModelLifecyclePolicy.from_project_root(self.root)
        self._registry_source = registry
        self.registry = registry or ModelRegistry.from_loader(ConfigLoader(self.root))
        self.runtime_lease = runtime_lease or GpuHeavyLease(
            self.policy.lock_path(), poll_interval_seconds=self.policy.poll_interval_seconds
        )
        if gpu_lease is not None:
            self.gpu_lease = gpu_lease
        else:
            g = GpuCoordinationPolicy.from_project_root(self.root)
            self.gpu_lease = current_lease() or GpuHeavyLease(
                g.lock_path(), poll_interval_seconds=g.poll_interval_seconds
            )
        self.backend = backend or OllamaRuntimeBackend(
            self.policy.lifecycle_timeout_seconds, self.policy.status_poll_interval_seconds
        )
        self.memory_probe = memory_probe or MemoryProbe()
        self.journal = journal or RuntimeJournal(self.root)
        self.profile_store = profile_store or RuntimeProfileStore(self.root, self.policy)
        self._local = threading.local()
        self._metrics_lock = threading.Lock()
        self._metrics = {
            "runtime_transactions": 0,
            "transitions": 0,
            "known_unloads": 0,
            "blocked_unknown_gpu": 0,
            "blocked_todo": 0,
            "blocked_ram": 0,
            "blocked_vram": 0,
            "post_load_failures": 0,
            "cleanup_failures": 0,
            "warm_reuse": 0,
            "tag_resolutions": 0,
            "placement_calibrations": 0,
            "calibration_retries": 0,
            "placement_cache_hits": 0,
            "calibration_failures": 0,
        }

    def metrics(self) -> dict[str, int]:
        with self._metrics_lock:
            return dict(self._metrics)

    def _record(self, key: str, amount: int = 1) -> None:
        with self._metrics_lock:
            self._metrics[key] += amount

    def refresh_registry(self) -> ModelRegistry:
        if self._registry_source is None:
            self.registry = ModelRegistry.from_loader(ConfigLoader(self.root))
            return self.registry
        servers = {s.server_id: s for s in self._registry_source.servers}
        models: dict[str, ModelDefinition] = {}
        for logical_id in self._registry_source.logical_ids:
            d = self._registry_source.get(logical_id)
            entry = self.profile_store.get(logical_id)
            if isinstance(entry, dict):
                tag = entry.get("model_tag")
                if isinstance(tag, str) and tag.strip():
                    d = replace(d, ollama_model=tag.strip())
                if (
                    entry.get("status") == "CALIBRATED"
                    and isinstance(entry.get("num_gpu"), int)
                    and isinstance(entry.get("admission_vram_mib"), (int, float))
                ):
                    runtime = dict(d.runtime); resources = dict(d.resources)
                    runtime["num_gpu"] = int(entry["num_gpu"])
                    resources["estimated_vram_gb"] = float(entry["admission_vram_mib"]) / 1024.0
                    resources["phase20v_observed_vram_gb"] = float(entry.get("observed_vram_mib") or 0.0) / 1024.0
                    d = replace(d, runtime=runtime, resources=resources)
            models[logical_id] = d
        self.registry = ModelRegistry(servers, models)
        return self.registry

    def definition_for_tag(self, base_url: str, tag: str) -> ModelDefinition | None:
        for sid in self.registry.servers:
            if sid.ollama_base_url.rstrip("/") != base_url.rstrip("/"):
                continue
            for logical_id in self.registry.logical_ids:
                d = self.registry.get(logical_id)
                if d.server == sid.server_id and d.model_tag_known and d.ollama_model == tag:
                    return d
        return None

    def _server_urls(self) -> tuple[tuple[str, str], ...]:
        # A single physical Ollama endpoint may serve multiple logical roles.
        # Read/account each physical resident once, regardless of role aliases.
        urls: dict[str, str] = {}
        for server in self.registry.servers:
            urls.setdefault(server.ollama_base_url.rstrip("/"), server.server_id)
        return tuple((server_id, url) for url, server_id in urls.items())

    def _tag_rule_matches(self, logical_id: str, tag: str) -> bool:
        rule = self.policy.todo_tag_resolution.get(logical_id)
        if rule is None:
            return False
        text = _normalised_tag_text(tag)
        all_tokens = tuple(rule.get("all_tokens", ()))
        any_tokens = tuple(rule.get("any_tokens", ()))
        if not all(token in text for token in all_tokens):
            return False
        return not any_tokens or any(token in text for token in any_tokens)

    def resolve_definition(
        self,
        logical_id: str,
        *,
        available_tags: tuple[str, ...] | None = None,
    ) -> ModelDefinition:
        try:
            current = self.registry.get(logical_id)
        except KeyError as exc:
            raise ModelRuntimeBlocker("UNKNOWN_MODEL", logical_id) from exc
        if current.model_tag_known:
            return current
        rule = self.policy.todo_tag_resolution.get(logical_id)
        if rule is None:
            self._record("blocked_todo")
            raise ModelRuntimeBlocker("MODEL_TAG_TODO", logical_id)
        server = self.registry.get_server(current.server)
        tags = available_tags if available_tags is not None else self.backend.tags(server.ollama_base_url, optional=False)
        matches = tuple(sorted(tag for tag in tags if self._tag_rule_matches(logical_id, tag)))
        if not matches:
            self._record("blocked_todo")
            raise ModelRuntimeBlocker(
                "MODEL_NOT_INSTALLED",
                f"{logical_id}: no installed Ollama tag matches the Phase20V deterministic rule",
            )
        if len(matches) != 1:
            self._record("blocked_todo")
            raise ModelRuntimeBlocker(
                "MODEL_TAG_AMBIGUOUS",
                f"{logical_id}: deterministic rule matched multiple installed tags: {matches}",
            )
        self.profile_store.set_resolution(logical_id, matches[0])
        self._record("tag_resolutions")
        self.journal.event("model_tag_resolved", logical_id=logical_id, model=matches[0])
        self.refresh_registry()
        resolved = self.registry.get(logical_id)
        if not resolved.model_tag_known:
            raise ModelRuntimeError(f"runtime profile overlay failed to resolve {logical_id}")
        return resolved

    def residents(self, *, include_external: bool = True) -> tuple[ResidentModel, ...]:
        rows: list[ResidentModel] = []
        for server_id, base_url in self._server_urls():
            for item in self.backend.ps(base_url, optional=True):
                tag = str(item.get("name") or item.get("model") or "")
                if not tag:
                    continue
                d = self.definition_for_tag(base_url, tag)
                if d is not None and self.policy.memory_policy == MEMORY_POLICY:
                    resident_budget([item])
                rows.append(ResidentModel(
                    base_url, server_id if d is None else d.server, tag,
                    int(item.get("size") or 0), int(item.get("size_vram") or 0),
                    str(item.get("expires_at")) if item.get("expires_at") is not None else None,
                    None if d is None else d.logical_id, d is not None,
                ))
        if include_external:
            for base_url in self.policy.external_observe_urls:
                for item in self.backend.ps(base_url, optional=True):
                    tag = str(item.get("name") or item.get("model") or "")
                    if tag:
                        rows.append(ResidentModel(
                            base_url, "external", tag,
                            int(item.get("size") or 0), int(item.get("size_vram") or 0),
                            str(item.get("expires_at")) if item.get("expires_at") is not None else None,
                            None, False,
                        ))
        return tuple(rows)

    def all_known_worker_tags(self) -> tuple[str, ...]:
        tags = []
        for logical_id in self.registry.logical_ids:
            d = self.registry.get(logical_id)
            if d.server == "worker" and d.runtime.get("execution") == "gpu_worker" and d.model_tag_known:
                tags.append(d.ollama_model)
        return tuple(sorted(set(tags)))

    def blocker_summary(self) -> tuple[str, ...]:
        out = []
        for logical_id in self.registry.logical_ids:
            d = self.registry.get(logical_id)
            if not d.model_tag_known:
                out.append(f"TAG_UNRESOLVED:{logical_id}")
        for r in self.residents():
            if r.gpu_resident and not r.known:
                out.append(f"FOREIGN_GPU_RESIDENT:{r.server_id}:{r.name}")
        return tuple(sorted(set(out)))

    def snapshot(self, *, write: bool = True) -> dict[str, Any]:
        memory = self.memory_probe.sample()
        rows = [asdict(x) | {"gpu_resident": x.gpu_resident} for x in self.residents()]
        payload = {
            "memory": asdict(memory),
            "residents": rows,
            "memory_policy": self.policy.memory_policy,
            "model_vram_budget_mib": MODEL_VRAM_BUDGET_MIB,
            "swarm_resident_vram_bytes": sum(r["size_vram_bytes"] for r in rows if r["known"]),
            "peak_allocation_cap": "NOT_PROVEN",
            "blockers": list(self.blocker_summary()),
            "model_runtime_lock": asdict(self.runtime_lease.status()),
            "gpu_heavy_lock": asdict(self.gpu_lease.status()),
            "metrics": self.metrics(),
        }
        if write:
            self.journal.snapshot(payload)
        return payload

    def _assert_known_target(self, logical_id: str) -> ModelDefinition:
        return self.resolve_definition(logical_id)

    def _assert_memory_floor(self, memory: MemoryTelemetry, *, phase: str) -> None:
        if memory.ram_available_mib < self.policy.min_available_ram_mib:
            self._record("blocked_ram")
            raise ModelRuntimeBlocker(
                "RAM_FLOOR", f"{phase}: available={memory.ram_available_mib:.0f} MiB < {self.policy.min_available_ram_mib} MiB"
            )
        if self.policy.memory_policy == MEMORY_POLICY:
            try:
                resident_budget([dict(size_vram=r.size_vram_bytes) for r in self.residents(include_external=False) if r.known])
            except ValueError as exc:
                raise ModelRuntimeBlocker(str(exc), phase) from exc
            return
        if memory.vram_free_mib is not None and memory.vram_free_mib < self.policy.min_free_vram_mib:
            self._record("blocked_vram")
            raise ModelRuntimeBlocker(
                "VRAM_FLOOR", f"{phase}: free={memory.vram_free_mib:.0f} MiB < {self.policy.min_free_vram_mib} MiB"
            )

    def _assert_no_unknown_gpu(self) -> None:
        bad = [r for r in self.residents() if r.gpu_resident and not r.known]
        if bad:
            self._record("blocked_unknown_gpu")
            detail = ", ".join(f"{r.server_id}:{r.name}" for r in bad)
            raise ModelRuntimeBlocker("FOREIGN_GPU_RESIDENT", detail)

    def unload_known(self, resident: ResidentModel) -> None:
        if not resident.known:
            raise ModelRuntimeError("refusing to unload unknown/manual model")
        self.journal.event("unload_start", logical_id=resident.logical_id, model=resident.name, server=resident.server_id)
        self.backend.unload(resident.base_url, resident.name)
        self._record("known_unloads")
        self.journal.event("unload_complete", logical_id=resident.logical_id, model=resident.name, server=resident.server_id)

    def unload_registered_except(self, logical_id: str | None, *, worker_only: bool = False) -> tuple[str, ...]:
        unloaded = []
        for r in self.residents(include_external=False):
            if not r.known or r.logical_id == logical_id:
                continue
            if worker_only and r.server_id != "worker":
                continue
            self.unload_known(r)
            unloaded.append(r.logical_id or r.name)
        if unloaded:
            self._record("transitions")
        return tuple(unloaded)

    @staticmethod
    def _resident_vram_mib(item: dict[str, Any] | None) -> float:
        return float((item or {}).get("size_vram") or 0) / (1024.0 * 1024.0)

    def _calibration_start_num_gpu(self, definition: ModelDefinition, base_url: str) -> int:
        configured = definition.runtime.get("num_gpu")
        if isinstance(configured, int) and not isinstance(configured, bool) and configured >= self.policy.calibration_min_num_gpu:
            return configured
        blocks = self.backend.block_count(base_url, definition.ollama_model)
        if blocks is None:
            raise ModelRuntimeBlocker(
                "PLACEMENT_CALIBRATION_UNAVAILABLE",
                f"{definition.logical_id}: model block_count unavailable and no configured num_gpu exists",
            )
        # Ollama treats block_count+1 as the full-offload boundary.
        return blocks + 1

    def _calibrate_worker_locked(self, definition: ModelDefinition) -> dict[str, Any]:
        if definition.runtime.get("execution") != "gpu_worker":
            raise ModelRuntimeBlocker("NOT_GPU_WORKER", definition.logical_id)
        server = self.registry.get_server(definition.server)
        context_default = int(definition.context.get("default") or 0)
        if context_default <= 0:
            raise ModelRuntimeBlocker("INVALID_CONTEXT", definition.logical_id)
        candidate = self._calibration_start_num_gpu(definition, server.ollama_base_url)
        attempts: list[dict[str, Any]] = []
        seen: set[int] = set()
        target_free = self.policy.min_free_vram_mib + self.policy.calibration_safety_margin_mib

        for attempt_index in range(1, self.policy.calibration_max_attempts + 1):
            if candidate < self.policy.calibration_min_num_gpu or candidate in seen:
                break
            seen.add(candidate)
            self.unload_registered_except(definition.logical_id)
            self._assert_no_unknown_gpu()
            before = self.memory_probe.sample()
            if before.ram_available_mib < self.policy.min_available_ram_mib:
                self._assert_memory_floor(before, phase="calibration_preload")
            if before.vram_free_mib is None or before.vram_total_mib is None:
                raise ModelRuntimeBlocker("VRAM_TELEMETRY_UNAVAILABLE", definition.logical_id)

            if self.policy.memory_policy == MEMORY_POLICY:
                try:
                    require_model_capacity(before.vram_free_mib)
                except ValueError as exc:
                    raise ModelRuntimeBlocker(str(exc), definition.logical_id) from exc
            started = time.monotonic()
            resident: dict[str, Any] | None = None
            after: MemoryTelemetry | None = None
            load_error: Exception | None = None
            try:
                self.backend.preload(
                    server.ollama_base_url,
                    definition.ollama_model,
                    context_default,
                    candidate,
                )
                resident = self.backend.resident(server.ollama_base_url, definition.ollama_model)
                if resident is None:
                    raise ModelRuntimeError(f"calibration preload did not make {definition.ollama_model} resident")
                after = self.memory_probe.sample()
            except Exception as exc:
                load_error = exc

            load_s = time.monotonic() - started
            if load_error is not None or after is None:
                try:
                    self.backend.unload(server.ollama_base_url, definition.ollama_model)
                except Exception as cleanup_exc:
                    self._record("cleanup_failures")
                    raise ModelRuntimeError(
                        f"calibration load failed and cleanup failed for {definition.logical_id}: {cleanup_exc}"
                    ) from cleanup_exc
                attempts.append({
                    "attempt": attempt_index,
                    "num_gpu": candidate,
                    "load_s": load_s,
                    "status": "LOAD_FAILED",
                    "error": str(load_error),
                })
                self._record("calibration_retries")
                next_candidate = candidate - max(1, math.ceil(candidate * 0.10))
                candidate = max(self.policy.calibration_min_num_gpu, next_candidate)
                continue

            assert after.vram_free_mib is not None and after.vram_total_mib is not None
            observed_vram_mib = self._resident_vram_mib(resident)
            admission_vram_mib = max(0.0, before.vram_free_mib - after.vram_free_mib)
            item = {
                "attempt": attempt_index,
                "num_gpu": candidate,
                "load_s": load_s,
                "status": "OBSERVED",
                "observed_vram_mib": observed_vram_mib,
                "admission_vram_mib": admission_vram_mib,
                "free_after_load_mib": after.vram_free_mib,
                "ram_available_mib": after.ram_available_mib,
            }
            attempts.append(item)

            ram_ok = after.ram_available_mib >= self.policy.min_available_ram_mib
            vram_floor_ok = self.policy.memory_policy == MEMORY_POLICY or after.vram_free_mib >= target_free
            hard_limit_ok = (observed_vram_mib if self.policy.memory_policy == MEMORY_POLICY else admission_vram_mib) <= self.policy.max_worker_vram_mib
            gpu_present = observed_vram_mib > 0

            if ram_ok and vram_floor_ok and hard_limit_ok and gpu_present:
                self.backend.unload(server.ollama_base_url, definition.ollama_model)
                self.profile_store.set_calibrated(
                    definition.logical_id,
                    model_tag=definition.ollama_model,
                    num_gpu=candidate,
                    context_default=context_default,
                    observed_vram_mib=observed_vram_mib,
                    admission_vram_mib=admission_vram_mib,
                    free_after_load_mib=after.vram_free_mib,
                    gpu_total_mib=after.vram_total_mib,
                    attempts=attempts,
                )
                self._record("placement_calibrations")
                self.journal.event(
                    "worker_placement_calibrated",
                    logical_id=definition.logical_id,
                    model=definition.ollama_model,
                    num_gpu=candidate,
                    admission_vram_mib=admission_vram_mib,
                    free_after_load_mib=after.vram_free_mib,
                    attempts=attempt_index,
                )
                self.refresh_registry()
                return {
                    "logical_id": definition.logical_id,
                    "model_tag": definition.ollama_model,
                    "num_gpu": candidate,
                    "admission_vram_mib": admission_vram_mib,
                    "observed_vram_mib": observed_vram_mib,
                    "free_after_load_mib": after.vram_free_mib,
                    "ram_available_mib": after.ram_available_mib,
                    "attempts": attempts,
                    "status": "CALIBRATED",
                }

            try:
                self.backend.unload(server.ollama_base_url, definition.ollama_model)
            except Exception as cleanup_exc:
                self._record("cleanup_failures")
                raise ModelRuntimeError(
                    f"unsafe calibration placement could not be unloaded for {definition.logical_id}: {cleanup_exc}"
                ) from cleanup_exc

            if not ram_ok:
                self._record("calibration_failures")
                raise ModelRuntimeBlocker(
                    "RAM_FLOOR",
                    f"{definition.logical_id}: calibration available RAM {after.ram_available_mib:.0f} MiB < {self.policy.min_available_ram_mib} MiB",
                )
            if not gpu_present:
                self._record("calibration_failures")
                raise ModelRuntimeBlocker(
                    "GPU_PLACEMENT_MISSING",
                    f"{definition.logical_id}: num_gpu={candidate} produced size_vram=0",
                )

            floor_deficit = 0.0 if self.policy.memory_policy == MEMORY_POLICY else max(0.0, target_free - after.vram_free_mib)
            limit_deficit = max(0.0, (observed_vram_mib if self.policy.memory_policy == MEMORY_POLICY else admission_vram_mib) - self.policy.max_worker_vram_mib)
            deficit = max(floor_deficit, limit_deficit)
            per_layer = max(64.0, observed_vram_mib / max(1, candidate))
            drop = max(1, math.ceil((deficit + 32.0) / per_layer))
            next_candidate = candidate - drop
            self._record("calibration_retries")
            if next_candidate < self.policy.calibration_min_num_gpu:
                break
            candidate = next_candidate

        self._record("calibration_failures")
        raise ModelRuntimeBlocker(
            "NO_SAFE_GPU_PLACEMENT",
            f"{definition.logical_id}: no placement satisfied free>={target_free} MiB and worker_vram<={self.policy.max_worker_vram_mib} MiB after {len(attempts)} attempt(s)",
        )

    def ensure_worker_profiles(self, *, force_validate: bool = False) -> dict[str, Any]:
        'Resolve TODO tags and calibrate maximum safe GPU placement profiles.\n\n        The result is persisted under state/evaluation only. Frozen model YAML is\n        never edited. Future ConfigLoader instances transparently see this measured\n        overlay while model runtime is enabled.\n        '
        report: dict[str, Any] = {"admissible": [], "profiles": [], "blockers": []}
        with self.model_transaction("worker-profile-ensure"):
            try:
                lease_cm = self.gpu_lease.held(
                    "MODEL_RUNTIME:worker-profile-ensure",
                    timeout_seconds=self.policy.acquire_timeout_seconds,
                )
                lease_cm.__enter__()
            except GpuHeavyLeaseTimeout as exc:
                raise ModelRuntimeBlocker("GPU_LOCK_BUSY", "cannot calibrate worker profiles") from exc
            try:
                worker_server = self.registry.get_server("worker")
                available = self.backend.tags(worker_server.ollama_base_url, optional=False)
                # Resolve deterministic TODO aliases before classifying current
                # residents as foreign. This lets a stale but uniquely identifiable
                # Swarm model become known and therefore safely reclaimable.
                resolution_blockers: dict[str, ModelRuntimeBlocker] = {}
                logical_ids = tuple(self.registry.logical_ids)
                for logical_id in logical_ids:
                    d0 = self.registry.get(logical_id)
                    if d0.runtime.get("execution") != "gpu_worker" or d0.model_tag_known:
                        continue
                    try:
                        self.resolve_definition(logical_id, available_tags=available)
                    except ModelRuntimeBlocker as exc:
                        resolution_blockers[logical_id] = exc
                self.refresh_registry()
                self.unload_registered_except(None)
                self._assert_no_unknown_gpu()
                # Work from a stable logical-id list; refresh_registry() may replace
                # the registry after a calibration.
                logical_ids = tuple(self.registry.logical_ids)
                for logical_id in logical_ids:
                    base_definition = self.registry.get(logical_id)
                    if base_definition.runtime.get("execution") != "gpu_worker":
                        continue
                    if logical_id in resolution_blockers:
                        exc = resolution_blockers[logical_id]
                        report["blockers"].append({"logical_id": logical_id, "code": exc.code, "detail": exc.detail})
                        continue
                    try:
                        definition = self.resolve_definition(logical_id, available_tags=available)
                        if definition.ollama_model not in available:
                            raise ModelRuntimeBlocker(
                                "MODEL_NOT_INSTALLED",
                                f"{logical_id}: configured tag {definition.ollama_model!r} is not installed on worker Ollama",
                            )
                        entry = self.profile_store.get(logical_id)
                        cache_valid = (
                            isinstance(entry, dict)
                            and entry.get("status") == "CALIBRATED"
                            and entry.get("model_tag") == definition.ollama_model
                            and int(entry.get("context_default") or -1) == int(definition.context.get("default") or -2)
                            and self._cache_memory_valid(entry)
                            and int(entry.get("max_worker_vram_mib") or -1) == self.policy.max_worker_vram_mib
                        )
                        if cache_valid and not force_validate:
                            self._record("placement_cache_hits")
                            profile = {
                                "logical_id": logical_id,
                                "model_tag": definition.ollama_model,
                                "num_gpu": int(entry["num_gpu"]),
                                "admission_vram_mib": float(entry["admission_vram_mib"]),
                                "observed_vram_mib": float(entry.get("observed_vram_mib") or 0.0),
                                "free_after_load_mib": float(entry.get("free_after_load_mib") or 0.0),
                                "status": "CACHE_HIT",
                            }
                        else:
                            if entry and not cache_valid:
                                self.profile_store.remove(logical_id)
                                self.refresh_registry()
                                definition = self.resolve_definition(logical_id, available_tags=available)
                            profile = self._calibrate_worker_locked(definition)
                        report["profiles"].append(profile)
                        report["admissible"].append(logical_id)
                    except ModelRuntimeBlocker as exc:
                        # Exact target cleanup is mandatory before continuing the inventory.
                        try:
                            d = self.registry.get(logical_id)
                            if d.model_tag_known:
                                self.backend.unload(worker_server.ollama_base_url, d.ollama_model)
                        except Exception as cleanup_exc:
                            self._record("cleanup_failures")
                            if self.policy.fail_closed:
                                raise ModelRuntimeError(
                                    f"profile blocker cleanup failed for {logical_id}: {cleanup_exc}"
                                ) from cleanup_exc
                        report["blockers"].append({
                            "logical_id": logical_id,
                            "code": exc.code,
                            "detail": exc.detail,
                        })
                self.refresh_registry()
            finally:
                lease_cm.__exit__(None, None, None)
        self.journal.event("worker_profiles_ensured", **report)
        return report

    def _cache_memory_valid(self, entry: dict[str, Any]) -> bool:
        if self.policy.memory_policy == MEMORY_POLICY:
            value = entry.get("observed_vram_mib")
            return type(value) in (int, float) and 0 < value <= MODEL_VRAM_BUDGET_MIB
        return int(entry.get("min_free_vram_mib") or -1) == self.policy.min_free_vram_mib

    def pre_load(self, base_url: str, model_tag: str) -> None:
        d = self.definition_for_tag(base_url, model_tag)
        if d is None:
            raise ModelRuntimeBlocker("UNREGISTERED_LOAD", f"{base_url}:{model_tag}")
        self._assert_known_target(d.logical_id)
        # At this point WorkerLifecycle/gpu coordination already owns GPU_HEAVY. Enforce
        # the invariant that one registered target is the only Swarm model allowed
        # to remain resident across a transition. Frozen SWITCH_REQUIRED may already
        # have unloaded its victim; this is idempotent and also catches stale models
        # that appeared after routing.
        self.unload_registered_except(d.logical_id)
        self._assert_no_unknown_gpu()
        before = self.memory_probe.sample()
        if before.ram_available_mib < self.policy.min_available_ram_mib:
            self._assert_memory_floor(before, phase="pre_load")
        if self.policy.memory_policy == MEMORY_POLICY:
            try:
                rows = [dict(size_vram=r.size_vram_bytes) for r in self.residents(include_external=False) if r.known]
                occupied = resident_budget(rows)
                require_model_capacity(before.vram_free_mib, resident_bytes=occupied)
            except ValueError as exc:
                raise ModelRuntimeBlocker(str(exc), d.logical_id) from exc
            self.journal.event("load_admitted", logical_id=d.logical_id, model=model_tag, memory=asdict(before), memory_policy=MEMORY_POLICY, model_budget_mib=MODEL_VRAM_BUDGET_MIB)
            return
        estimate = d.resources.get("estimated_vram_gb")
        if isinstance(estimate, (int, float)) and not isinstance(estimate, bool) and before.vram_free_mib is not None:
            resident = self.backend.resident(base_url, model_tag)
            resident_vram_mib = self._resident_vram_mib(resident)

            # A target that is already GPU-resident has already consumed most
            # or all of its calibrated footprint. Admission must therefore
            # reserve only the incremental allocation still required rather
            # than charging the complete model footprint a second time.
            estimated_vram_mib = float(estimate) * 1024.0
            incremental_vram_mib = max(
                0.0,
                estimated_vram_mib - resident_vram_mib,
            )
            required = incremental_vram_mib + self.policy.min_free_vram_mib

            if before.vram_free_mib < required:
                self._record("blocked_vram")
                raise ModelRuntimeBlocker(
                    "VRAM_ADMISSION",
                    (
                        f"{d.logical_id}: free={before.vram_free_mib:.0f} MiB "
                        f"< incremental+reserve={required:.0f} MiB "
                        f"(estimate={estimated_vram_mib:.0f} MiB, "
                        f"resident={resident_vram_mib:.0f} MiB)"
                    ),
                )

        self.journal.event(
            "load_admitted",
            logical_id=d.logical_id,
            model=model_tag,
            memory=asdict(before),
        )

    def post_load(self, base_url: str, model_tag: str) -> None:
        d = self.definition_for_tag(base_url, model_tag)
        if d is None:
            return
        memory = self.memory_probe.sample()
        try:
            self._assert_memory_floor(memory, phase="post_load")
        except ModelRuntimeBlocker:
            self._record("post_load_failures")
            try:
                self.backend.unload(base_url, model_tag)
            except Exception as cleanup_exc:
                self._record("cleanup_failures")
                if self.policy.fail_closed:
                    raise ModelRuntimeError(f"post-load floor failed and cleanup failed: {cleanup_exc}") from cleanup_exc
            raise
        self.journal.event("load_verified", logical_id=d.logical_id, model=model_tag, memory=asdict(memory))

    @contextmanager
    def model_transaction(self, owner: str, logical_id: str | None = None) -> Iterator[None]:
        with self.runtime_lease.held(f"MODEL_RUNTIME:{owner}", timeout_seconds=self.policy.acquire_timeout_seconds):
            self._record("runtime_transactions")
            self.journal.event("transaction_start", owner=owner, logical_id=logical_id)
            try:
                if logical_id is not None:
                    self._assert_known_target(logical_id)
                yield
            finally:
                self.journal.event("transaction_end", owner=owner, logical_id=logical_id)
                try:
                    self.snapshot(write=True)
                except Exception as exc:
                    self.journal.event("snapshot_error", error=str(exc))

    def reconcile_startup(self) -> tuple[str, ...]:
        with self.model_transaction("startup-reconcile"):
            # GPU lock is required while mutating worker/possibly GPU residency.
            try:
                cm = self.gpu_lease.held("MODEL_RUNTIME:startup-cleanup", timeout_seconds=self.policy.acquire_timeout_seconds)
                cm.__enter__()
            except GpuHeavyLeaseTimeout as exc:
                raise ModelRuntimeBlocker("GPU_LOCK_BUSY", "cannot reconcile stale registered models") from exc
            try:
                unloaded = self.unload_registered_except(None)
            finally:
                cm.__exit__(None, None, None)
            self.journal.event("startup_reconcile", unloaded=list(unloaded))
            return unloaded

    def final_cleanup(self) -> tuple[str, ...]:
        with self.model_transaction("final-cleanup"):
            try:
                cm = self.gpu_lease.held("MODEL_RUNTIME:final-cleanup", timeout_seconds=self.policy.acquire_timeout_seconds)
                cm.__enter__()
            except GpuHeavyLeaseTimeout as exc:
                raise ModelRuntimeBlocker("GPU_LOCK_BUSY", "cannot perform final model cleanup") from exc
            try:
                unloaded = self.unload_registered_except(None)
            finally:
                cm.__exit__(None, None, None)
            remaining = [r for r in self.residents(include_external=False) if r.known]
            if remaining:
                self._record("cleanup_failures")
                raise ModelRuntimeError("final cleanup left registered Swarm models resident")
            self.journal.event("final_cleanup", unloaded=list(unloaded))
            return unloaded

    def live_worker_probe(self, logical_id: str, *, leave_resident: bool = True) -> dict[str, Any]:
        d = self._assert_known_target(logical_id)
        if d.runtime.get("execution") != "gpu_worker":
            raise ModelRuntimeBlocker("NOT_GPU_WORKER", logical_id)
        entry = self.profile_store.get(logical_id)
        if not isinstance(entry, dict) or entry.get("status") != "CALIBRATED":
            # The live validator normally ensures all profiles up front. Keep this
            # method safe when called independently.
            ensured = self.ensure_worker_profiles(force_validate=False)
            if logical_id not in ensured.get("admissible", []):
                match = next((x for x in ensured.get("blockers", []) if x.get("logical_id") == logical_id), None)
                if match:
                    raise ModelRuntimeBlocker(str(match["code"]), str(match["detail"]))
                raise ModelRuntimeBlocker("PROFILE_NOT_CALIBRATED", logical_id)
            d = self.refresh_registry().get(logical_id)
            entry = self.profile_store.get(logical_id)
        server = self.registry.get_server(d.server)
        num_gpu = d.runtime.get("num_gpu")
        if not isinstance(num_gpu, int) or isinstance(num_gpu, bool) or num_gpu < self.policy.calibration_min_num_gpu:
            raise ModelRuntimeBlocker("PROFILE_NOT_CALIBRATED", f"{logical_id}: invalid effective num_gpu={num_gpu!r}")
        context_default = int(d.context.get("default") or 8192)

        with self.model_transaction(f"live-worker:{logical_id}", logical_id):
            with self.gpu_lease.held(f"MODEL_RUNTIME:live-worker:{logical_id}", timeout_seconds=self.policy.acquire_timeout_seconds):
                self.unload_registered_except(logical_id)
                self._assert_no_unknown_gpu()
                current = self.backend.resident(server.ollama_base_url, d.ollama_model)
                warm_reuse = current is not None and int(current.get("size_vram") or 0) > 0
                if warm_reuse:
                    memory = self.memory_probe.sample()
                    self._assert_memory_floor(memory, phase="warm_reuse")
                    load_s = 0.0
                    self._record("warm_reuse")
                else:
                    self.pre_load(server.ollama_base_url, d.ollama_model)
                    started = time.monotonic()
                    self.backend.preload(server.ollama_base_url, d.ollama_model, context_default, num_gpu)
                    load_s = time.monotonic() - started
                    self.post_load(server.ollama_base_url, d.ollama_model)
                started = time.monotonic()
                response = self.backend.chat_probe(
                    server.ollama_base_url, d.ollama_model,
                    context_default, num_gpu,
                    self.policy.worker_keep_alive,
                )
                call_s = time.monotonic() - started
                memory = self.memory_probe.sample()
                self._assert_memory_floor(memory, phase="live_call")
                if not leave_resident:
                    self.backend.unload(server.ollama_base_url, d.ollama_model)
                self.journal.event(
                    "live_worker_probe", logical_id=logical_id, load_s=load_s, call_s=call_s,
                    memory=asdict(memory), done=bool(response.get("done")), warm_reuse=warm_reuse,
                    num_gpu=num_gpu,
                )
                return {
                    "logical_id": logical_id,
                    "load_s": load_s,
                    "call_s": call_s,
                    "memory": asdict(memory),
                    "done": response.get("done"),
                    "warm_reuse": warm_reuse,
                    "num_gpu": num_gpu,
                    "model_tag": d.ollama_model,
                }



def _install_worker_lock_and_lifecycle(controller: ModelRuntimeController) -> None:
    from harness.executor.lifecycle import WorkerLifecycleManager

    original_prepared = WorkerLifecycleManager.prepared
    _ORIGINALS["WorkerLifecycleManager.prepared"] = original_prepared

    @contextmanager
    def runtime_prepared(self, decision):
        logical_id = getattr(decision, "selected_model", None)
        owner = str(logical_id or "unknown")
        with controller.model_transaction(f"worker:{owner}", logical_id if isinstance(logical_id, str) else None):
            with original_prepared(self, decision) as prepared:
                yield prepared

    WorkerLifecycleManager.prepared = runtime_prepared


def _install_client_lifecycle(controller: ModelRuntimeController) -> None:
    from harness.executor.client import OllamaNativeClient

    original_preload = OllamaNativeClient.preload_model
    original_chat = OllamaNativeClient.chat
    _ORIGINALS["OllamaNativeClient.preload_model"] = original_preload
    _ORIGINALS["OllamaNativeClient.chat"] = original_chat

    def guarded_preload(self, base_url, model_tag, num_ctx, *, num_gpu=None):
        controller.pre_load(base_url, model_tag)
        try:
            result = original_preload(self, base_url, model_tag, num_ctx, num_gpu=num_gpu)
            controller.post_load(base_url, model_tag)
            return result
        except Exception:
            # Partial load cleanup is exact and verified by the backend.
            try:
                if controller.definition_for_tag(base_url, model_tag) is not None:
                    controller.backend.unload(base_url, model_tag)
            except Exception as cleanup_exc:
                controller._record("cleanup_failures")
                if controller.policy.fail_closed:
                    raise ModelRuntimeError(f"worker preload failed and cleanup failed: {cleanup_exc}") from cleanup_exc
            raise

    def bounded_chat(self, base_url, model_tag, messages, keep_alive, num_ctx, *, num_gpu=None, **kwargs):
        definition = controller.definition_for_tag(base_url, model_tag)
        effective = keep_alive
        if definition is not None and definition.server == "worker" and keep_alive != 0:
            effective = controller.policy.worker_keep_alive
        if definition is not None and controller.policy.memory_policy == MEMORY_POLICY:
            controller._assert_memory_floor(controller.memory_probe.sample(), phase="generation")
        return original_chat(self, base_url, model_tag, messages, effective, num_ctx, num_gpu=num_gpu, **kwargs)

    OllamaNativeClient.preload_model = guarded_preload
    OllamaNativeClient.chat = bounded_chat


def _install_calibration_transaction(controller: ModelRuntimeController) -> None:
    from harness.calibration.runner import CalibrationRunner
    original = CalibrationRunner.run
    _ORIGINALS["CalibrationRunner.run"] = original

    def guarded(self, request):
        logical_id = getattr(request, "logical_model_id", "unknown")
        with controller.model_transaction(f"calibration:{logical_id}", logical_id):
            return original(self, request)

    CalibrationRunner.run = guarded


def _install_profile_transaction(controller: ModelRuntimeController) -> None:
    from harness.executor.profile_client import ProfileOllamaClient
    original_preload = ProfileOllamaClient.preload_profile
    original_unload = ProfileOllamaClient.unload_profile
    _ORIGINALS["ProfileOllamaClient.preload_profile"] = original_preload
    _ORIGINALS["ProfileOllamaClient.unload_profile"] = original_unload

    def guarded_preload(self, base_url, profile):
        existing = getattr(self, "_model_runtime_reservation", None)
        tag = getattr(profile, "model_tag", "unknown")
        d = controller.definition_for_tag(base_url, tag)
        logical_id = None if d is None else d.logical_id
        if existing is not None:
            if existing[0] != tag:
                raise ModelRuntimeBlocker("PROFILE_RUNTIME_BUSY", f"active={existing[0]} requested={tag}")
            return original_preload(self, base_url, profile)
        cm = controller.model_transaction(f"profile:{tag}", logical_id)
        cm.__enter__()
        try:
            result = original_preload(self, base_url, profile)
        except BaseException:
            cm.__exit__(*__import__("sys").exc_info())
            raise
        self._model_runtime_reservation = (tag, cm)
        return result

    def guarded_unload(self, base_url, profile):
        reservation = getattr(self, "_model_runtime_reservation", None)
        try:
            return original_unload(self, base_url, profile)
        finally:
            if reservation is not None and reservation[0] == getattr(profile, "model_tag", None):
                self._model_runtime_reservation = None
                reservation[1].__exit__(None, None, None)

    ProfileOllamaClient.preload_profile = guarded_preload
    ProfileOllamaClient.unload_profile = guarded_unload


def _install_ornith_transaction(controller: ModelRuntimeController) -> None:
    from harness.gpu_acceleration.runtime import OrnithGpuBurstManager
    original = OrnithGpuBurstManager.burst
    _ORIGINALS["OrnithGpuBurstManager.burst"] = original

    @contextmanager
    def guarded_burst(self, owner="ornith"):
        with controller.model_transaction(f"ornith:{owner}", "ornith_lead"):
            # Phase20U was frozen before Phase20V could resolve TODO tags or add
            # execution evidence-only profile workers. Refresh its reclaim allowlist at the
            # transaction boundary so Worker -> Ornith can reclaim every known
            # Swarm worker after acquiring GPU_HEAVY.
            self.known_worker_tags = controller.all_known_worker_tags()
            with original(self, owner) as state:
                yield state

    OrnithGpuBurstManager.burst = guarded_burst


def install_runtime_orchestration(
    root: str | Path,
    *,
    policy: ModelLifecyclePolicy | None = None,
    controller: ModelRuntimeController | None = None,
) -> ModelRuntimeController:
    global _INSTALLED, _CONTROLLER, _PROFILE_STORE
    if _INSTALLED:
        assert _CONTROLLER is not None
        return _CONTROLLER
    root = Path(root).resolve()
    policy = policy or ModelLifecyclePolicy.from_project_root(root)
    store = RuntimeProfileStore(root, policy)
    _PROFILE_STORE = store
    _install_config_overlay(store)
    _install_execution_profile_overlay(store)
    controller = controller or ModelRuntimeController(root, policy=policy, profile_store=store)
    _install_worker_lock_and_lifecycle(controller)
    _install_client_lifecycle(controller)
    _install_calibration_transaction(controller)
    _install_profile_transaction(controller)
    _install_ornith_transaction(controller)
    _CONTROLLER = controller
    _INSTALLED = True
    controller.journal.event("phase20v_installed")
    return controller


def current_controller() -> ModelRuntimeController | None:
    return _CONTROLLER


def install_from_environment() -> ModelRuntimeController | None:
    root_text = os.getenv("SWARM_MODEL_RUNTIME_ROOT", "").strip()
    if not root_text:
        return None
    policy = ModelLifecyclePolicy.from_project_root(root_text)
    if not policy.enabled():
        return None
    return install_runtime_orchestration(root_text, policy=policy)

# Verified compatibility aliases for historical Python interfaces.
RuntimeProfileStore.phase13_profiles = RuntimeProfileStore.execution_profiles
RuntimeProfileStore._merge_phase13_targets = RuntimeProfileStore._merge_execution_profile_targets
