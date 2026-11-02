'execution profiles immutable execution-profile and attempt-budget contracts.'
from __future__ import annotations
from harness.compatibility.configuration import resolve_config_path, normalize_configuration_document

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping
import math
import yaml

from harness.errors import ConfigurationError
from harness.executor.profile_tool_models import V2ToolName


class ExecutionRole(StrEnum):
    MICRO_EDITOR = "micro_editor"
    REPO_CODER = "repo_coder"
    HEAVY_FAST = "heavy_fast"
    HEAVY_QUALITY = "heavy_quality"


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty string")
    return value.strip()


def _positive_int(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{name} must be integer > 0")
    return value


def _non_negative_int(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be integer >= 0")
    return value


def _finite_number(value: object, name: str, *, minimum: float | None = None) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)):
        raise ValueError(f"{name} must be finite numeric")
    result = float(value)
    if minimum is not None and result < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return result


@dataclass(frozen=True, slots=True)
class ExecutionProfilePolicy:
    max_worker_calls_per_attempt: int
    finalization_grace_max_calls: int
    require_successful_terminal_tool_for_grace: bool
    require_changed_paths_for_grace: bool

    def __post_init__(self) -> None:
        _positive_int(self.max_worker_calls_per_attempt, "max_worker_calls_per_attempt")
        _non_negative_int(self.finalization_grace_max_calls, "finalization_grace_max_calls")
        if self.finalization_grace_max_calls > 1:
            raise ValueError("Phase 13C finalization grace must remain <= 1")
        if not isinstance(self.require_successful_terminal_tool_for_grace, bool):
            raise ValueError("require_successful_terminal_tool_for_grace must be bool")
        if not isinstance(self.require_changed_paths_for_grace, bool):
            raise ValueError("require_changed_paths_for_grace must be bool")


@dataclass(frozen=True, slots=True)
class ExecutionBudget:
    max_productive_calls: int
    finalization_grace_calls: int
    max_tool_calls: int
    max_protocol_failures: int
    max_tool_failures: int

    def __post_init__(self) -> None:
        _positive_int(self.max_productive_calls, "max_productive_calls")
        _non_negative_int(self.finalization_grace_calls, "finalization_grace_calls")
        if self.finalization_grace_calls > 1:
            raise ValueError("finalization_grace_calls must remain <= 1")
        _positive_int(self.max_tool_calls, "max_tool_calls")
        _non_negative_int(self.max_protocol_failures, "max_protocol_failures")
        _non_negative_int(self.max_tool_failures, "max_tool_failures")
        if self.max_tool_calls > self.max_productive_calls:
            raise ValueError("max_tool_calls cannot exceed max_productive_calls")

    @property
    def max_total_model_calls(self) -> int:
        return self.max_productive_calls + self.finalization_grace_calls


@dataclass(frozen=True, slots=True)
class ExecutionProfile:
    profile_id: str
    model_id: str
    model_tag: str
    server_id: str
    role: ExecutionRole
    context_tokens: int
    num_gpu: int | None
    think: bool | None
    num_predict: int
    temperature: float
    generation_timeout_seconds: float
    estimated_vram_gb: float
    calibration_ref: str
    budget: ExecutionBudget
    allowed_tools: tuple[V2ToolName, ...]

    def __post_init__(self) -> None:
        for name in ("profile_id", "model_id", "model_tag", "server_id", "calibration_ref"):
            _text(getattr(self, name), name)
        if not isinstance(self.role, ExecutionRole):
            raise ValueError("role must be ExecutionRole")
        _positive_int(self.context_tokens, "context_tokens")
        if self.num_gpu is not None:
            _non_negative_int(self.num_gpu, "num_gpu")
        if self.think is not None and not isinstance(self.think, bool):
            raise ValueError("think must be bool|None")
        _positive_int(self.num_predict, "num_predict")
        if not 0.0 <= _finite_number(self.temperature, "temperature") <= 2.0:
            raise ValueError("temperature must be in [0, 2]")
        _finite_number(self.generation_timeout_seconds, "generation_timeout_seconds", minimum=0.001)
        _finite_number(self.estimated_vram_gb, "estimated_vram_gb", minimum=0.0)
        if not isinstance(self.budget, ExecutionBudget):
            raise ValueError("budget must be ExecutionBudget")
        if not isinstance(self.allowed_tools, tuple) or not self.allowed_tools:
            raise ValueError("allowed_tools must be non-empty tuple")
        if any(not isinstance(tool, V2ToolName) for tool in self.allowed_tools):
            raise ValueError("allowed_tools must contain V2ToolName")
        if len(self.allowed_tools) != len(set(self.allowed_tools)):
            raise ValueError("allowed_tools must not contain duplicates")
        if V2ToolName.RUN_COMMAND in self.allowed_tools:
            raise ValueError("Phase 13C coding profiles must not expose RUN_COMMAND")

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "model_id": self.model_id,
            "model_tag": self.model_tag,
            "server_id": self.server_id,
            "role": self.role.value,
            "context_tokens": self.context_tokens,
            "num_gpu": self.num_gpu,
            "think": self.think,
            "num_predict": self.num_predict,
            "temperature": self.temperature,
            "generation_timeout_seconds": self.generation_timeout_seconds,
            "estimated_vram_gb": self.estimated_vram_gb,
            "calibration_ref": self.calibration_ref,
            "budget": {
                "max_productive_calls": self.budget.max_productive_calls,
                "finalization_grace_calls": self.budget.finalization_grace_calls,
                "max_tool_calls": self.budget.max_tool_calls,
                "max_protocol_failures": self.budget.max_protocol_failures,
                "max_tool_failures": self.budget.max_tool_failures,
                "max_total_model_calls": self.budget.max_total_model_calls,
            },
            "allowed_tools": [tool.value for tool in self.allowed_tools],
        }


class ExecutionProfileRegistry:
    def __init__(
        self,
        policy: ExecutionProfilePolicy,
        profiles: Mapping[str, ExecutionProfile],
    ) -> None:
        if not isinstance(policy, ExecutionProfilePolicy):
            raise ValueError("policy must be Phase13ExecutionPolicy")
        if not isinstance(profiles, Mapping) or not profiles:
            raise ValueError("profiles must be non-empty mapping")
        normalized: dict[str, ExecutionProfile] = {}
        for profile_id, profile in profiles.items():
            if not isinstance(profile, ExecutionProfile):
                raise ValueError("profiles must contain ExecutionProfile")
            if profile_id != profile.profile_id:
                raise ValueError("profile mapping key must match profile_id")
            if profile.budget.max_total_model_calls > policy.max_worker_calls_per_attempt:
                raise ValueError(
                    f"profile {profile_id} exceeds Phase13 attempt worker-call contract"
                )
            if profile.budget.finalization_grace_calls > policy.finalization_grace_max_calls:
                raise ValueError(
                    f"profile {profile_id} exceeds finalization grace policy"
                )
            normalized[profile_id] = profile
        self.policy = policy
        self._profiles = MappingProxyType(normalized)

    @property
    def profile_ids(self) -> tuple[str, ...]:
        return tuple(self._profiles)

    def get(self, profile_id: str) -> ExecutionProfile:
        try:
            return self._profiles[profile_id]
        except KeyError as exc:
            raise KeyError(f"unknown execution profile: {profile_id}") from exc

    @classmethod
    def from_project_root(cls, root: Path) -> "ExecutionProfileRegistry":
        root = Path(root)
        execution_policy_path = resolve_config_path(root / "config" / "phase13.yaml")
        profiles_path = root / "config" / "execution_profiles_v2.yaml"
        try:
            execution_policy_document = normalize_configuration_document(yaml.safe_load(execution_policy_path.read_text(encoding="utf-8")))
            document = normalize_configuration_document(yaml.safe_load(profiles_path.read_text(encoding="utf-8")))
        except OSError as exc:
            raise ConfigurationError(f"cannot read Phase 13C config: {exc}") from exc
        except yaml.YAMLError as exc:
            raise ConfigurationError(f"malformed Phase 13C YAML: {exc}") from exc
        if not isinstance(execution_policy_document, dict) or execution_policy_document.get("schema_version") != 1:
            raise ConfigurationError("phase13.yaml requires schema_version: 1")
        execution = execution_policy_document.get("execution_v2")
        if not isinstance(execution, dict):
            raise ConfigurationError("phase13.yaml.execution_v2 must be mapping")
        try:
            policy = ExecutionProfilePolicy(
                int(execution["max_worker_calls_per_attempt"]),
                int(execution["finalization_grace_max_calls"]),
                execution["require_successful_terminal_tool_for_grace"],
                execution["require_changed_paths_for_grace"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigurationError(f"invalid phase13 execution policy: {exc}") from exc

        if not isinstance(document, dict) or document.get("schema_version") != 1:
            raise ConfigurationError("execution_profiles_v2.yaml requires schema_version: 1")
        raw_profiles = document.get("profiles")
        if not isinstance(raw_profiles, dict) or not raw_profiles:
            raise ConfigurationError("execution_profiles_v2.yaml.profiles must be mapping")
        profiles: dict[str, ExecutionProfile] = {}
        for profile_id, raw in raw_profiles.items():
            if not isinstance(profile_id, str) or not isinstance(raw, dict):
                raise ConfigurationError("profile entries must be mappings")
            try:
                budget = ExecutionBudget(
                    int(raw["max_productive_calls"]),
                    int(raw["finalization_grace_calls"]),
                    int(raw["max_tool_calls"]),
                    int(raw["max_protocol_failures"]),
                    int(raw["max_tool_failures"]),
                )
                tools_raw = raw["allowed_tools"]
                if not isinstance(tools_raw, list):
                    raise ValueError("allowed_tools must be list")
                tools = tuple(V2ToolName(str(item)) for item in tools_raw)
                profiles[profile_id] = ExecutionProfile(
                    profile_id=profile_id,
                    model_id=str(raw["model_id"]),
                    model_tag=str(raw["model_tag"]),
                    server_id=str(raw["server_id"]),
                    role=ExecutionRole(str(raw["role"])),
                    context_tokens=int(raw["context_tokens"]),
                    num_gpu=None if raw.get("num_gpu") is None else int(raw["num_gpu"]),
                    think=raw.get("think"),
                    num_predict=int(raw["num_predict"]),
                    temperature=float(raw["temperature"]),
                    generation_timeout_seconds=float(raw["generation_timeout_seconds"]),
                    estimated_vram_gb=float(raw["estimated_vram_gb"]),
                    calibration_ref=str(raw["calibration_ref"]),
                    budget=budget,
                    allowed_tools=tools,
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ConfigurationError(f"invalid execution profile {profile_id}: {exc}") from exc
        try:
            return cls(policy, profiles)
        except ValueError as exc:
            raise ConfigurationError(str(exc)) from exc

# Verified compatibility aliases for historical Python interfaces.
Phase13ExecutionPolicy = ExecutionProfilePolicy
