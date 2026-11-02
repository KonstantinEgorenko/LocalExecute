'Configuration contract for research reasoning.'
from __future__ import annotations
from harness.compatibility.configuration import resolve_config_path, normalize_configuration_document

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import yaml


@dataclass(frozen=True, slots=True)
class ResearchReasoningPolicy:
    max_model_attempts: int
    max_previous_response_chars: int
    allow_canonical_json_markdown_fence: bool
    require_strict_json_object: bool
    require_frozen_research_parser: bool
    live_reasoning_cases: int
    live_min_success_rate: float

    def __post_init__(self) -> None:
        if self.max_model_attempts < 2:
            raise ValueError("Phase 18R requires max_model_attempts >= 2")
        if self.max_previous_response_chars <= 0:
            raise ValueError("max_previous_response_chars must be positive")
        for name in (
            "allow_canonical_json_markdown_fence",
            "require_strict_json_object",
            'require_frozen_research_parser',
        ):
            if getattr(self, name) is not True:
                raise ValueError(f"Phase 18R invariant requires {name}=true")
        if self.live_reasoning_cases <= 0:
            raise ValueError("live_reasoning_cases must be positive")
        if not 0.0 <= self.live_min_success_rate <= 1.0:
            raise ValueError("live_min_success_rate must be in [0,1]")

    @classmethod
    def from_project_root(cls, root: object) -> "ResearchReasoningPolicy":
        path = resolve_config_path(Path(root) / "config" / "phase18r.yaml")
        raw = normalize_configuration_document(yaml.safe_load(path.read_text(encoding="utf-8")))
        if not isinstance(raw, Mapping) or raw.get("schema_version") != 1:
            raise ValueError("phase18r.yaml requires schema_version: 1")
        body = raw.get("research_reasoning_reliability_v1")
        if not isinstance(body, Mapping):
            raise ValueError("phase18r research_reasoning_reliability_v1 must be mapping")
        live = body.get("live_acceptance")
        if not isinstance(live, Mapping):
            raise ValueError("phase18r live_acceptance must be mapping")
        return cls(
            int(body["max_model_attempts"]),
            int(body["max_previous_response_chars"]),
            bool(body["allow_canonical_json_markdown_fence"]),
            bool(body["require_strict_json_object"]),
            bool(body['require_frozen_research_parser']),
            int(live["reasoning_cases"]),
            float(live["min_success_rate"]),
        )

# Verified legacy import/pickle aliases, not canonical public exports.
Phase18RPolicy = ResearchReasoningPolicy

from harness.compatibility.configuration import install_legacy_fields
install_legacy_fields(ResearchReasoningPolicy, {'require_frozen_phase18_parser': 'require_frozen_research_parser'})
