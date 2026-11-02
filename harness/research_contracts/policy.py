'Configuration contract for research contracts.'
from __future__ import annotations
from harness.compatibility.configuration import resolve_config_path, normalize_configuration_document

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import yaml


@dataclass(frozen=True, slots=True)
class ResearchSemanticPolicy:
    max_semantic_attempts: int
    max_previous_response_chars: int
    forbid_automatic_contract_lifting: bool
    require_frozen_research_parser: bool
    require_reasoning_transport_boundary: bool
    live_plan_cases: int
    live_synthesis_cases: int
    live_min_success_rate: float

    def __post_init__(self) -> None:
        if self.max_semantic_attempts < 2:
            raise ValueError("Phase 18R2 requires max_semantic_attempts >= 2")
        if self.max_previous_response_chars <= 0:
            raise ValueError("max_previous_response_chars must be positive")
        for name in (
            "forbid_automatic_contract_lifting",
            'require_frozen_research_parser',
            'require_reasoning_transport_boundary',
        ):
            if getattr(self, name) is not True:
                raise ValueError(f"Phase 18R2 invariant requires {name}=true")
        if self.live_plan_cases <= 0 or self.live_synthesis_cases <= 0:
            raise ValueError("live plan/synthesis case counts must be positive")
        if not 0.0 <= self.live_min_success_rate <= 1.0:
            raise ValueError("live_min_success_rate must be in [0,1]")

    @classmethod
    def from_project_root(cls, root: object) -> "ResearchSemanticPolicy":
        path = resolve_config_path(Path(root) / "config" / "phase18r2.yaml")
        raw = normalize_configuration_document(yaml.safe_load(path.read_text(encoding="utf-8")))
        if not isinstance(raw, Mapping) or raw.get("schema_version") != 1:
            raise ValueError("phase18r2.yaml requires schema_version: 1")
        body = raw.get("research_semantic_contract_reliability_v1")
        if not isinstance(body, Mapping):
            raise ValueError("phase18r2 reliability config must be mapping")
        live = body.get("live_acceptance")
        if not isinstance(live, Mapping):
            raise ValueError("phase18r2 live_acceptance must be mapping")
        return cls(
            int(body["max_semantic_attempts"]),
            int(body["max_previous_response_chars"]),
            bool(body["forbid_automatic_contract_lifting"]),
            bool(body['require_frozen_research_parser']),
            bool(body['require_reasoning_transport_boundary']),
            int(live["plan_cases"]),
            int(live["synthesis_cases"]),
            float(live["min_success_rate"]),
        )

# Verified legacy import/pickle aliases, not canonical public exports.
Phase18R2Policy = ResearchSemanticPolicy

from harness.compatibility.configuration import install_legacy_fields
install_legacy_fields(ResearchSemanticPolicy, {'require_frozen_phase18_parser': 'require_frozen_research_parser', 'require_phase18r_transport_boundary': 'require_reasoning_transport_boundary'})
