"""Process-local single-Ollama-endpoint overlay for the production Swarm.

The historical frozen phases describe separate logical ``lead`` and ``worker``
servers.  Production keeps those logical identities for routing and telemetry,
but maps both to the already running lead endpoint.  No Ollama process, global
configuration, or source YAML is changed.
"""

from __future__ import annotations

import copy
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from ..config import ConfigLoader
from ..lead.runtime import LeadModelError, OrnithRuntime
from ..models import ModelRegistry
from ..gpu_acceleration.policy import GpuAccelerationPolicy


_INSTALLED = False
_ORIGINAL_LOAD_MODELS: Callable[[ConfigLoader], dict[str, Any]] | None = None


def single_endpoint_models_document(loader: ConfigLoader) -> dict[str, Any]:
    """Return a copied model document with one physical endpoint."""

    document = copy.deepcopy(loader.load_models())
    servers = document["servers"]
    lead = dict(servers["lead"])
    servers["worker"] = {
        "ollama_base_url": lead["ollama_base_url"],
        "openai_base_url": lead["openai_base_url"],
    }
    return document


def single_endpoint_registry(loader: ConfigLoader) -> ModelRegistry:
    """Build an isolated registry without mutating ConfigLoader globally."""

    document = single_endpoint_models_document(loader)

    class DocumentLoader:
        @staticmethod
        def load_models() -> dict[str, Any]:
            return document

    return ModelRegistry.from_loader(DocumentLoader())


def install_single_endpoint_overlay() -> None:
    """Map logical worker traffic to the lead URL for this process only."""

    global _INSTALLED, _ORIGINAL_LOAD_MODELS
    if _INSTALLED:
        return
    original = ConfigLoader.load_models
    _ORIGINAL_LOAD_MODELS = original

    def load_models(loader: ConfigLoader) -> dict[str, Any]:
        document = copy.deepcopy(original(loader))
        servers = document["servers"]
        lead = dict(servers["lead"])
        servers["worker"] = {
            "ollama_base_url": lead["ollama_base_url"],
            "openai_base_url": lead["openai_base_url"],
        }
        return document

    ConfigLoader.load_models = load_models
    _INSTALLED = True


class SingleEndpointOrnithRuntime(OrnithRuntime):
    """Ornith contract variant protected by the shared production runtime lock.

    The original runtime rejects equal URLs because older launches had no common
    lifecycle owner. The production launcher installs MODEL_RUNTIME and
    GPU_HEAVY guards before this adapter is constructed, so logical role checks
    remain mandatory while physical URL separation is no longer a safety rule.
    """

    def _definition(self):
        try:
            definition = self.registry.get(self.model_id)
            server = self.registry.get_server(definition.server)
        except KeyError as exc:
            raise LeadModelError("configured Lead model/server is unavailable") from exc
        if (
            definition.logical_id != self.model_id
            or server.server_id != "lead"
            or definition.server == "worker"
            or definition.runtime.get("execution") != "cpu"
        ):
            raise LeadModelError("configured Lead must be CPU model on lead server")
        if not definition.model_tag_known:
            raise LeadModelError("Ornith model tag is TODO")
        return definition, server

def single_endpoint_gpu_acceleration_policy(root: str | Path) -> GpuAccelerationPolicy:
    """Retain frozen placement policy while observing workers on the lead URL."""

    policy = GpuAccelerationPolicy.from_project_root(root)
    return replace(policy, worker_base_url=policy.lead_base_url)


# Verified compatibility aliases for historical Python interfaces.
single_endpoint_phase20u_policy = single_endpoint_gpu_acceleration_policy
