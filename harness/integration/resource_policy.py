'Project-local resource policy used by the production integration.2 production composition.'

from __future__ import annotations
from harness.compatibility.configuration import resolve_config_path, normalize_configuration_document

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

from ..errors import ConfigurationError
from ..model_vram import MEMORY_POLICY
from ..models import ModelRegistry
from ..model_runtime import ModelLifecyclePolicy
from ..resource_manager import ResourceManager


def load_worker_reserve(root: Path) -> float:
    current_policy = resolve_config_path(Path(root) / "config" / "phase20v.yaml")
    if current_policy.is_file() and ModelLifecyclePolicy.from_project_root(root).memory_policy == MEMORY_POLICY:
        return 0.0
    path = resolve_config_path(Path(root) / "config" / "phase21_2.yaml")
    try:
        document = normalize_configuration_document(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigurationError(f"Cannot load {path}: {exc}") from exc
    try:
        value = document["protocol_reliability_v1"]["worker_min_free_after_load_gb"]
    except (KeyError, TypeError) as exc:
        raise ConfigurationError(
            "phase21_2.yaml.protocol_reliability_v1.worker_min_free_after_load_gb is required"
        ) from exc
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ConfigurationError("Phase 21.2 worker reserve must be numeric")

    reserve = float(value)

    # Historical reserve contract for explicit legacy policy fixtures: never allow below
    # 1.5 GiB. Higher values remain valid.
    if reserve < 1.5:
        raise ConfigurationError(
            "Production worker reserve must remain >= 1.5 GiB"
        )

    return reserve


def production_model_lifecycle_policy(root: Path) -> ModelLifecyclePolicy:
    'Resolve current model-only policy or an explicit historical reserve.\n\n    Current config/model runtime.yaml selects the model-only contract. Historical\n    reserve fixtures continue to resolve config/production resources.yaml explicitly.\n    '
    base = ModelLifecyclePolicy.from_project_root(root)
    reserve_gb = load_worker_reserve(root)
    reserve_mib = int(round(reserve_gb * 1024.0))

    return replace(
        base,
        min_free_vram_mib=reserve_mib,
    )


class ProductionResourceManager(ResourceManager):
    """Expose the effective model-only budget to routing and executor checks.

    Explicit legacy callers retain their historical free-reserve contract.
    OS execution locks remain independent of model-memory accounting.
    """

    def __init__(
        self,
        registry: ModelRegistry,
        resource_config: dict[str, Any],
        *,
        min_free_after_load_gb: float,
        memory_policy: str | None = None,
    ) -> None:
        effective = deepcopy(resource_config)
        effective["worker_vram"]["min_free_after_load_gb"] = float(min_free_after_load_gb)
        if memory_policy == MEMORY_POLICY:
            effective["worker_vram"]["memory_policy"] = MEMORY_POLICY
            effective["worker_vram"]["hard_limit_gb"] = 11.0
        super().__init__(registry, effective)
        self._production_reserve_gb = float(min_free_after_load_gb)

    @property
    def worker_vram_policy(self) -> MappingProxyType:
        current = dict(super().worker_vram_policy)
        current["min_free_after_load_gb"] = self._production_reserve_gb
        return MappingProxyType(current)

# Verified compatibility aliases for historical Python interfaces.
production_phase20v_policy = production_model_lifecycle_policy
