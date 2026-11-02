"""Read-only diagnostic selection of existing production placement authorities."""
from __future__ import annotations

import copy
from pathlib import Path

from .config import ConfigLoader
from .integration.resource_policy import production_model_lifecycle_policy
from .integration.single_endpoint import single_endpoint_models_document, single_endpoint_gpu_acceleration_policy
from .model_runtime.runtime import RuntimeProfileStore


class RuntimeProfileResolutionError(ValueError):
    pass


def diagnostic_models_document(root: Path, mode="production", model_id=None, execution_profile=None):
    if mode not in ("production", "cpu-fallback"):
        raise RuntimeProfileResolutionError(f"unknown runtime profile: {mode}")
    configured = single_endpoint_models_document(ConfigLoader(root))
    policy = production_model_lifecycle_policy(root)
    store = RuntimeProfileStore(root, policy)
    effective = store.apply_document(configured)
    burst = single_endpoint_gpu_acceleration_policy(root)
    selected_execution = None
    if execution_profile is not None:
        selected_execution = next((p for p in store.execution_profiles() if p.profile_id == execution_profile), None)
        if selected_execution is None:
            raise RuntimeProfileResolutionError(f"unknown exact execution profile: {execution_profile}")
        if model_id is not None and model_id != selected_execution.model_id:
            raise RuntimeProfileResolutionError("execution profile does not match selected logical model")
        model_id = selected_execution.model_id
        if mode != "production":
            raise RuntimeProfileResolutionError("execution profiles have no declared CPU fallback")
    if model_id is not None:
        if model_id not in effective["models"]:
            raise RuntimeProfileResolutionError(f"unknown exact profile ID: {model_id}")
        effective["models"] = {model_id: effective["models"][model_id]}
    for key, model in effective["models"].items():
        baseline = configured["models"].get(key, model)
        endpoint = effective["servers"][model["server"]]["ollama_base_url"]
        tag = model["ollama_model"]
        context = model["context"]["default"]
        if tag == burst.model_tag and endpoint.rstrip("/") == burst.lead_base_url.rstrip("/"):
            layers = burst.cpu_num_gpu if mode == "cpu-fallback" else burst.gpu_num_gpu
            context = burst.num_ctx
            source = "GpuAccelerationPolicy.cpu_num_gpu" if mode == "cpu-fallback" else "GpuAccelerationPolicy.gpu_num_gpu"
        else:
            if mode == "cpu-fallback":
                raise RuntimeProfileResolutionError(f"{key}: no production CPU fallback is declared")
            entry = store.get(key)
            if (not entry or entry.get("model_tag") != tag or
                    not store._placement_valid_for_document(entry, model)):
                raise RuntimeProfileResolutionError(f"{key}: effective placement unresolved; valid Phase20V calibration required")
            layers = entry["num_gpu"]
            source = "RuntimeProfileStore (validated production policy)"
        if selected_execution is not None:
            context = selected_execution.context_tokens
            source += "; ExecutionProfileRegistry context"
        if type(layers) is not int or layers < 0 or type(context) is not int or context <= 0:
            raise RuntimeProfileResolutionError(f"{key}: invalid effective placement")
        if not isinstance(tag, str) or not tag.strip() or tag.upper() == "TODO":
            raise RuntimeProfileResolutionError(f"{key}: exact model tag unresolved")
        model["configured_runtime"] = copy.deepcopy(baseline["runtime"])
        model["configured_context"] = copy.deepcopy(baseline["context"])
        model["effective_placement"] = {
            "runtime_profile": mode, "source": source, "model_id": key,
            "execution_profile": execution_profile, "model": tag, "endpoint": endpoint,
            "num_gpu": layers, "num_ctx": context, "resolution": "SELECTED_NOT_LIVE_ADMITTED",
        }
        model["runtime"]["num_gpu"] = layers
        model["context"]["default"] = context
    return effective
