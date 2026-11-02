"""Validated, cached loading of the swarm configuration files."""
from __future__ import annotations
from pathlib import Path
from typing import Any
import math
import yaml
from .errors import ConfigurationError

class ConfigLoader:
    """Loads each requested YAML document once per loader instance."""
    def __init__(self, root: Path) -> None: self.root, self._cache = Path(root), {}
    def load_models(self) -> dict[str, Any]: return self._load("models.yaml", self._validate_models)
    def load_resources(self) -> dict[str, Any]: return self._load("resources.yaml", self._validate_resources)
    def load_routing(self) -> dict[str, Any]: return self._load("routing.yaml", self._validate_routing)
    def load_policies(self) -> dict[str, Any]: return self._load("policies.yaml", self._validate_policies)
    def load_lead(self) -> dict[str, Any]: return self._load("lead.yaml", self._validate_lead)
    def _load(self, filename: str, validator: Any) -> dict[str, Any]:
        if filename not in self._cache:
            path = self.root / "config" / filename
            try:
                with path.open(encoding="utf-8") as source: document = yaml.safe_load(source)
            except OSError as exc: raise ConfigurationError(f"Cannot read configuration {path}: {exc}") from exc
            except yaml.YAMLError as exc: raise ConfigurationError(f"Malformed YAML in {path}: {exc}") from exc
            if not isinstance(document, dict): raise ConfigurationError(f"Configuration {path} must be a mapping")
            validator(document, filename); self._cache[filename] = document
        return self._cache[filename]
    @staticmethod
    def _require(mapping: dict[str, Any], key: str, location: str) -> Any:
        if key not in mapping: raise ConfigurationError(f"Missing required field {location}.{key}")
        return mapping[key]
    def _validate_models(self, document: dict[str, Any], filename: str) -> None:
        servers, models = self._require(document,"servers",filename), self._require(document,"models",filename)
        if not isinstance(servers,dict) or not isinstance(models,dict): raise ConfigurationError(f"{filename}.servers and {filename}.models must be mappings")
        for server_id, server in servers.items():
            if not isinstance(server,dict): raise ConfigurationError(f"{filename}.servers.{server_id} must be a mapping")
            for key in ("ollama_base_url","openai_base_url"):
                value=self._require(server,key,f"{filename}.servers.{server_id}")
                if not isinstance(value,str) or not value.startswith("http"): raise ConfigurationError(f"{filename}.servers.{server_id}.{key} must be an HTTP URL")
        for logical_id, model in models.items():
            if not isinstance(model,dict): raise ConfigurationError(f"{filename}.models.{logical_id} must be a mapping")
            for key in ("ollama_model","roles","server","context","resource_class","escalation_level","runtime","resources"): self._require(model,key,f"{filename}.models.{logical_id}")
            if model["server"] not in servers: raise ConfigurationError(f"{filename}.models.{logical_id}.server references an unknown server")
            if not isinstance(model["context"],dict) or not isinstance(model["runtime"],dict) or not isinstance(model["resources"],dict): raise ConfigurationError(f"{filename}.models.{logical_id} context/runtime/resources must be mappings")
            for key in ("supported_max","default","operational_max"): self._require(model["context"],key,f"{filename}.models.{logical_id}.context")
            default, operational = model["context"]["default"], model["context"]["operational_max"]
            if not isinstance(default, int) or isinstance(default, bool) or default <= 0: raise ConfigurationError(f"{filename}.models.{logical_id}.context.default must be a positive integer")
            if not isinstance(operational, int) or isinstance(operational, bool) or operational <= 0: raise ConfigurationError(f"{filename}.models.{logical_id}.context.operational_max must be a positive integer")
            if default > operational: raise ConfigurationError(f"{filename}.models.{logical_id}.context.default must not exceed operational_max")
            num_gpu = model["runtime"].get("num_gpu")
            if num_gpu is not None and (not isinstance(num_gpu, int) or isinstance(num_gpu, bool) or num_gpu < 0): raise ConfigurationError(f"{filename}.models.{logical_id}.runtime.num_gpu must be a non-negative integer or null")
    def _validate_lead(self, document: dict[str, Any], filename: str) -> None:
        lead = self._require(document, "lead", filename)
        if not isinstance(lead, dict): raise ConfigurationError("lead.yaml.lead must be a mapping")
        for key in ("model_id", "schema_version", "max_todos", "max_dependencies_per_todo", "max_lead_calls", "timeout_seconds", "output_tokens"):
            self._require(lead, key, "lead.yaml.lead")
        if lead["model_id"] != "ornith_lead": raise ConfigurationError("lead.yaml.lead.model_id must be ornith_lead")
        for key in ("schema_version", "max_todos", "max_dependencies_per_todo", "max_lead_calls", "output_tokens"):
            value=lead[key]
            if not isinstance(value,int) or isinstance(value,bool) or value<=0: raise ConfigurationError(f"lead.yaml.lead.{key} must be a positive integer")
        if lead["max_lead_calls"] != 2: raise ConfigurationError("lead.yaml.lead.max_lead_calls must be 2")
        timeout=lead["timeout_seconds"]
        if not isinstance(timeout,(int,float)) or isinstance(timeout,bool) or not math.isfinite(timeout) or timeout<=0: raise ConfigurationError("lead.yaml.lead.timeout_seconds must be finite positive numeric")
    def _validate_resources(self, document: dict[str, Any], filename: str) -> None:
        worker_vram, locks = self._require(document,"worker_vram",filename), self._require(document,"resource_locks",filename)
        if not isinstance(worker_vram,dict) or not isinstance(locks,dict): raise ConfigurationError(f"{filename}.worker_vram and {filename}.resource_locks must be mappings")
        for key in ("soft_target_gb","hard_limit_gb","min_free_after_load_gb"):
            if not isinstance(self._require(worker_vram,key,f"{filename}.worker_vram"),(int,float)): raise ConfigurationError(f"{filename}.worker_vram.{key} must be numeric")
        gpu_lock=self._require(locks,"gpu_worker_mutex",f"{filename}.resource_locks")
        if not isinstance(gpu_lock,dict) or self._require(gpu_lock,"max_active","gpu_worker_mutex") != 1: raise ConfigurationError("resources.yaml requires gpu_worker_mutex.max_active: 1")
    def _validate_routing(self, document: dict[str, Any], filename: str) -> None:
        router=self._require(document,"router_v1",filename); branches=self._require(document,"branches",filename); graph=self._require(document,"escalation_graph",filename)
        if not isinstance(router,dict) or not isinstance(branches,dict) or not isinstance(graph,dict): raise ConfigurationError("routing router_v1/branches/escalation_graph must be mappings")
        threshold=self._require(router,"confidence_threshold","routing.yaml.router_v1")
        if not isinstance(threshold,(int,float)) or not 0 <= threshold <= 1: raise ConfigurationError("routing.yaml confidence_threshold must be numeric in [0, 1]")
        order=self._require(router,"candidate_order","routing.yaml.router_v1")
        if not isinstance(order,list) or len(order) != len(set(order)): raise ConfigurationError("routing.yaml candidate_order must be a duplicate-free list")
        weights=self._require(router,"weights","routing.yaml.router_v1")
        if not isinstance(weights,dict) or any(not isinstance(value,(int,float)) for value in weights.values()): raise ConfigurationError("routing.yaml weights must be numeric")
        self._require(router,"model_signals","routing.yaml.router_v1")
        for name, branch in branches.items():
            if not isinstance(branch,dict) or not isinstance(branch.get("task_types"),list) or not isinstance(branch.get("workers"),list): raise ConfigurationError(f"routing.yaml branch {name} must define task_types/workers lists")
    def _validate_policies(self, document: dict[str, Any], filename: str) -> None:
        retry=self._require(document,"retry_and_anti_loop",filename)
        for key in ("max_attempts_same_fast_worker","max_attempts_same_heavy_worker"): self._require(retry,key,"policies.yaml.retry_and_anti_loop")
        executor = document.get("executor")
        if executor is not None:
            if not isinstance(executor, dict): raise ConfigurationError("policies.yaml.executor must be a mapping")
            for key in ("discovery_timeout_seconds", "lifecycle_timeout_seconds", "generation_timeout_seconds", "lifecycle_verification_timeout_seconds", "lifecycle_verification_poll_interval_seconds"):
                value = self._require(executor, key, "policies.yaml.executor")
                if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0 or not math.isfinite(value): raise ConfigurationError(f"policies.yaml.executor.{key} must be finite positive numeric")
            if executor["lifecycle_verification_poll_interval_seconds"] > executor["lifecycle_verification_timeout_seconds"]: raise ConfigurationError("policies.yaml.executor.lifecycle_verification_poll_interval_seconds must not exceed verification timeout")
        calibration = document.get("calibration")
        if calibration is not None:
            if not isinstance(calibration, dict): raise ConfigurationError("policies.yaml.calibration must be a mapping")
            ram_floor = self._require(calibration, "minimum_available_ram_gb", "policies.yaml.calibration")
            if not isinstance(ram_floor, (int, float)) or isinstance(ram_floor, bool) or not math.isfinite(ram_floor) or ram_floor <= 0: raise ConfigurationError("policies.yaml.calibration.minimum_available_ram_gb must be finite positive numeric")
            for key in ("require_clean_worker", "unload_after_run"):
                if not isinstance(self._require(calibration, key, "policies.yaml.calibration"), bool): raise ConfigurationError(f"policies.yaml.calibration.{key} must be boolean")
            if not isinstance(self._require(calibration, "generation_prompt", "policies.yaml.calibration"), str): raise ConfigurationError("policies.yaml.calibration.generation_prompt must be a string")
        worktree = document.get("worktree")
        if worktree is not None:
            if not isinstance(worktree, dict): raise ConfigurationError("policies.yaml.worktree must be a mapping")
            prefix = self._require(worktree, "branch_prefix", "policies.yaml.worktree")
            if not isinstance(prefix, str) or not prefix.strip(): raise ConfigurationError("policies.yaml.worktree.branch_prefix must be non-empty string")
            for key in ("require_clean_source", "protect_same_git_common_dir", "allow_model_commands"):
                if not isinstance(self._require(worktree, key, "policies.yaml.worktree"), bool): raise ConfigurationError(f"policies.yaml.worktree.{key} must be boolean")
            max_active = self._require(worktree, "max_active_leases", "policies.yaml.worktree")
            if not isinstance(max_active, int) or isinstance(max_active, bool) or max_active <= 0: raise ConfigurationError("policies.yaml.worktree.max_active_leases must be positive integer")
        critic = document.get("critic")
        if critic is not None:
            if not isinstance(critic, dict): raise ConfigurationError("policies.yaml.critic must be a mapping")
            model = self._require(critic, "model", "policies.yaml.critic")
            if model != "ministral_reasoner": raise ConfigurationError("policies.yaml.critic.model must be ministral_reasoner")
            for key in ("max_model_attempts", "max_evidence_items", "max_diff_bytes", "max_artifact_bytes", "max_prompt_bytes", "max_response_bytes"):
                value = self._require(critic, key, "policies.yaml.critic")
                if not isinstance(value, int) or isinstance(value, bool) or value <= 0: raise ConfigurationError(f"policies.yaml.critic.{key} must be positive integer")
            temperature = self._require(critic, "temperature", "policies.yaml.critic")
            if not isinstance(temperature, (int, float)) or isinstance(temperature, bool) or not math.isfinite(temperature) or not 0 <= temperature <= 2: raise ConfigurationError("policies.yaml.critic.temperature must be finite numeric in [0, 2]")
            keep_alive = self._require(critic, "keep_alive", "policies.yaml.critic")
            if not isinstance(keep_alive, (int, str)) or isinstance(keep_alive, bool): raise ConfigurationError("policies.yaml.critic.keep_alive must be int or str")
            mutation_guard = self._require(critic, "enforce_no_workspace_mutation", "policies.yaml.critic")
            if mutation_guard is not True: raise ConfigurationError("policies.yaml.critic.enforce_no_workspace_mutation must be true")
        judge = document.get("judge")
        if judge is not None:
            if not isinstance(judge, dict): raise ConfigurationError("policies.yaml.judge must be a mapping")
            model = self._require(judge, "model", "policies.yaml.judge")
            if model != "ornith_lead": raise ConfigurationError("policies.yaml.judge.model must be ornith_lead")
            for key in ("max_model_attempts", "max_diff_bytes", "max_prompt_bytes", "max_response_bytes", "output_tokens"):
                value = self._require(judge, key, "policies.yaml.judge")
                if not isinstance(value, int) or isinstance(value, bool) or value <= 0: raise ConfigurationError(f"policies.yaml.judge.{key} must be positive integer")
            timeout = self._require(judge, "timeout_seconds", "policies.yaml.judge")
            if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0: raise ConfigurationError("policies.yaml.judge.timeout_seconds must be finite positive numeric")
            if self._require(judge, "enforce_no_workspace_mutation", "policies.yaml.judge") is not True: raise ConfigurationError("policies.yaml.judge.enforce_no_workspace_mutation must be true")
            cannot_override = self._require(judge, "cannot_override", "policies.yaml.judge")
            evaluates = self._require(judge, "evaluates", "policies.yaml.judge")
            if not isinstance(cannot_override, list) or not cannot_override or any(not isinstance(x, str) or not x.strip() for x in cannot_override): raise ConfigurationError("policies.yaml.judge.cannot_override must be non-empty list[str]")
            if not isinstance(evaluates, list) or not evaluates or any(not isinstance(x, str) or not x.strip() for x in evaluates): raise ConfigurationError("policies.yaml.judge.evaluates must be non-empty list[str]")
        repository_intelligence = document.get("repository_intelligence")
        if repository_intelligence is not None:
            if not isinstance(repository_intelligence, dict): raise ConfigurationError("policies.yaml.repository_intelligence must be a mapping")
            if self._require(repository_intelligence, "trusted_worktrees_only", "policies.yaml.repository_intelligence") is not True: raise ConfigurationError("repository_intelligence.trusted_worktrees_only must be true")
            if self._require(repository_intelligence, "enforce_no_workspace_mutation", "policies.yaml.repository_intelligence") is not True: raise ConfigurationError("repository_intelligence.enforce_no_workspace_mutation must be true")
            for key in ("max_files", "max_file_bytes", "max_total_bytes", "max_symbols_per_file", "max_results", "max_context_files", "max_context_bytes"):
                value = self._require(repository_intelligence, key, "policies.yaml.repository_intelligence")
                if not isinstance(value, int) or isinstance(value, bool) or value <= 0: raise ConfigurationError(f"policies.yaml.repository_intelligence.{key} must be positive integer")
            if repository_intelligence["max_context_files"] > repository_intelligence["max_results"]: raise ConfigurationError("repository_intelligence.max_context_files must not exceed max_results")
            if repository_intelligence["max_context_bytes"] > repository_intelligence["max_total_bytes"]: raise ConfigurationError("repository_intelligence.max_context_bytes must not exceed max_total_bytes")
            excluded = self._require(repository_intelligence, "excluded_directories", "policies.yaml.repository_intelligence")
            if not isinstance(excluded, list) or not excluded or any(not isinstance(x, str) or not x.strip() or "/" in x or "\\" in x or x in (".", "..") for x in excluded) or len(excluded) != len(set(excluded)):
                raise ConfigurationError("repository_intelligence.excluded_directories must be unique directory names")
        evaluation = document.get("evaluation")
        if evaluation is not None:
            if not isinstance(evaluation, dict): raise ConfigurationError("policies.yaml.evaluation must be a mapping")
            store_dir = self._require(evaluation, "store_dir", "policies.yaml.evaluation")
            if not isinstance(store_dir, str) or not store_dir.strip(): raise ConfigurationError("policies.yaml.evaluation.store_dir must be non-empty string")
            store_path = Path(store_dir.strip())
            if store_path.is_absolute() or ".." in store_path.parts or str(store_path) in ("", "."): raise ConfigurationError("evaluation.store_dir must be project-relative and must not escape project root")
            for key in ("max_cases_per_suite", "max_records_per_run", "max_tags_per_case", "max_repetitions_per_case", "max_models_per_case"):
                value = self._require(evaluation, key, "policies.yaml.evaluation")
                if not isinstance(value, int) or isinstance(value, bool) or value <= 0: raise ConfigurationError(f"policies.yaml.evaluation.{key} must be positive integer")
            percentiles = self._require(evaluation, "latency_percentiles", "policies.yaml.evaluation")
            if not isinstance(percentiles, list) or not percentiles or any(not isinstance(x, int) or isinstance(x, bool) or not 1 <= x <= 100 for x in percentiles) or percentiles != sorted(set(percentiles)):
                raise ConfigurationError("evaluation.latency_percentiles must be unique ascending integers in [1, 100]")
            for key in ("require_verified_truth", "require_router_choice_in_candidates", "allow_partial_runs"):
                if not isinstance(self._require(evaluation, key, "policies.yaml.evaluation"), bool): raise ConfigurationError(f"policies.yaml.evaluation.{key} must be boolean")
            if evaluation["require_verified_truth"] is not True: raise ConfigurationError("evaluation.require_verified_truth must be true")
            if evaluation["require_router_choice_in_candidates"] is not True: raise ConfigurationError("evaluation.require_router_choice_in_candidates must be true")
            if evaluation["allow_partial_runs"] is not False: raise ConfigurationError("evaluation.allow_partial_runs must be false in Phase 12 V1")
        scheduler = document.get("scheduler")
        if scheduler is not None:
            if not isinstance(scheduler, dict): raise ConfigurationError("policies.yaml.scheduler must be a mapping")
            for key in ("max_parallel_todos", "max_active_claims", "max_attempts_per_todo", "claim_ttl_seconds", "resource_lease_ttl_seconds", "lock_timeout_seconds"):
                value = self._require(scheduler, key, "policies.yaml.scheduler")
                if not isinstance(value, int) or isinstance(value, bool) or value <= 0: raise ConfigurationError(f"policies.yaml.scheduler.{key} must be positive integer")
            if scheduler["max_parallel_todos"] > scheduler["max_active_claims"]: raise ConfigurationError("scheduler.max_parallel_todos must not exceed max_active_claims")
            for key in ("serialize_unknown_working_sets", "require_judge_report", "allow_parallel_conflict_free"):
                if not isinstance(self._require(scheduler, key, "policies.yaml.scheduler"), bool): raise ConfigurationError(f"policies.yaml.scheduler.{key} must be boolean")
            if scheduler["serialize_unknown_working_sets"] is not True: raise ConfigurationError("scheduler.serialize_unknown_working_sets must be true in Phase 11 V1")
            if scheduler["require_judge_report"] is not True: raise ConfigurationError("scheduler.require_judge_report must be true")
            if worktree is not None and scheduler["max_parallel_todos"] > worktree["max_active_leases"]: raise ConfigurationError("scheduler.max_parallel_todos must not exceed worktree.max_active_leases")
        verification = document.get("verification")
        if verification is not None:
            if not isinstance(verification, dict): raise ConfigurationError("policies.yaml.verification must be a mapping")
            runtime = verification.get("runtime")
            if runtime is not None:
                if not isinstance(runtime, dict): raise ConfigurationError("policies.yaml.verification.runtime must be a mapping")
                trusted = self._require(runtime, "trusted_worktrees_only", "policies.yaml.verification.runtime")
                if trusted is not True: raise ConfigurationError("policies.yaml.verification.runtime.trusted_worktrees_only must be true")
                trusted_python_venv = self._require(runtime, "trusted_python_venv", "policies.yaml.verification.runtime")
                if not isinstance(trusted_python_venv, str) or not trusted_python_venv.strip(): raise ConfigurationError("policies.yaml.verification.runtime.trusted_python_venv must be non-empty relative path")
                trusted_venv_path = Path(trusted_python_venv.strip())
                if trusted_venv_path.is_absolute() or ".." in trusted_venv_path.parts or str(trusted_venv_path) in ("", "."): raise ConfigurationError("policies.yaml.verification.runtime.trusted_python_venv must be project-relative and must not escape project root")
                for key in ("require_candidate_dirty", "fail_fast"):
                    if not isinstance(self._require(runtime, key, "policies.yaml.verification.runtime"), bool): raise ConfigurationError(f"policies.yaml.verification.runtime.{key} must be boolean")
                for key in ("max_gates", "max_gate_timeout_seconds", "max_total_timeout_seconds", "max_output_bytes", "max_source_bytes"):
                    value = self._require(runtime, key, "policies.yaml.verification.runtime")
                    if not isinstance(value, int) or isinstance(value, bool) or value <= 0: raise ConfigurationError(f"policies.yaml.verification.runtime.{key} must be positive integer")
                if runtime["max_gate_timeout_seconds"] > runtime["max_total_timeout_seconds"]: raise ConfigurationError("verification gate timeout must not exceed total timeout")
            catalog = verification.get("command_catalog")
            if catalog is not None:
                if not isinstance(catalog, dict): raise ConfigurationError("policies.yaml.verification.command_catalog must be a mapping")
                for gate_id, entry in catalog.items():
                    if not isinstance(gate_id, str) or not gate_id.strip() or not isinstance(entry, dict): raise ConfigurationError("verification command catalog entries must be mappings")
                    gate_type = self._require(entry, "gate_type", f"policies.yaml.verification.command_catalog.{gate_id}")
                    if gate_type not in ("TESTS", "LINT", "TYPE_CHECK", "BUILD", "CUSTOM"): raise ConfigurationError(f"verification command catalog {gate_id} has invalid gate_type")
                    argv = self._require(entry, "argv", f"policies.yaml.verification.command_catalog.{gate_id}")
                    if not isinstance(argv, list) or not argv or any(not isinstance(item, str) or not item.strip() for item in argv): raise ConfigurationError(f"verification command catalog {gate_id}.argv must be non-empty list[str]")
                    first = argv[0]
                    if first not in ("{python}", "{git}") and not Path(first).is_absolute(): raise ConfigurationError(f"verification command catalog {gate_id} executable must be {{python}}, {{git}}, or absolute path")
                    timeout = self._require(entry, "timeout_seconds", f"policies.yaml.verification.command_catalog.{gate_id}")
                    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0: raise ConfigurationError(f"verification command catalog {gate_id}.timeout_seconds must be positive integer")
                    if runtime is not None and timeout > runtime["max_gate_timeout_seconds"]: raise ConfigurationError(f"verification command catalog {gate_id}.timeout_seconds exceeds runtime max")
                    codes = self._require(entry, "expected_exit_codes", f"policies.yaml.verification.command_catalog.{gate_id}")
                    if not isinstance(codes, list) or not codes or any(not isinstance(code, int) or isinstance(code, bool) for code in codes) or len(codes) != len(set(codes)): raise ConfigurationError(f"verification command catalog {gate_id}.expected_exit_codes must be unique integers")