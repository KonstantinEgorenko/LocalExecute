"""Manual calibration command. It is never imported or run by normal harness paths."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Callable

from ..config import ConfigLoader
from ..executor.client import OllamaNativeClient, OllamaTimeouts
from ..models import ModelRegistry
from ..resource_manager.manager import ResourceManager
from ..resource_manager.snapshot import ResourceSnapshot
from ..runtime import OllamaDiscoveryClient, discover_servers
from .models import CalibrationObservation, CalibrationRequest
from .runner import CalibrationRunner


def _fresh_snapshot_provider(resources: ResourceManager, registry: ModelRegistry, discovery: OllamaDiscoveryClient) -> Callable[[], ResourceSnapshot]:
    """Discover runtime state anew at every calibration checkpoint."""
    def snapshot() -> ResourceSnapshot:
        return resources.get_snapshot(discover_servers(registry, discovery))
    return snapshot


def _write_observation(observation: CalibrationObservation, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = observation.timestamp.replace(":", "-").replace("+", "_")
    output = output_dir / f"{observation.logical_model_id}_ctx{observation.requested_context_length}_{stamp}.json"
    output.write_text(json.dumps(observation.to_dict(), indent=2) + "\n", encoding="utf-8")
    return output


def _human_output(observation: CalibrationObservation, output: Path) -> str:
    generation = "EXECUTED" if observation.generation_executed else "SKIPPED"
    return f"MODEL: {observation.logical_model_id}\nTAG: {observation.model_tag}\nREQUESTED CONTEXT: {observation.requested_context_length}\nREQUESTED NUM GPU: {observation.requested_num_gpu}\nOBSERVED CONTEXT: {observation.observed_context_length}\nOUTCOME: {observation.outcome}\nSTOP REASON: {observation.stop_reason}\nMODEL VRAM: {observation.loaded_model_vram_gb}\nSYSTEM GPU DELTA: {observation.system_gpu_delta_gb}\nSYSTEM RAM DELTA: {observation.system_ram_delta_gb}\nGENERATION: {generation}\nTOK/S: {observation.tokens_per_second}\nCLEANUP: {'PASS' if observation.cleanup_verified else 'FAIL'}\nRESULT JSON: {output}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one explicit worker calibration point.")
    parser.add_argument("--config-root", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--context", type=int, required=True)
    parser.add_argument("--num-gpu", type=int)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    loader = ConfigLoader(args.config_root)
    policies, registry = loader.load_policies(), ModelRegistry.from_loader(loader)
    resources = ResourceManager(registry, loader.load_resources())
    snapshot = _fresh_snapshot_provider(resources, registry, OllamaDiscoveryClient())
    runner = CalibrationRunner(registry, resources, OllamaNativeClient(OllamaTimeouts.from_policies(policies)), snapshot, policies)
    observation = runner.run(CalibrationRequest(args.model, args.context, args.num_gpu))
    output = _write_observation(observation, args.output_dir)
    print(_human_output(observation, output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())