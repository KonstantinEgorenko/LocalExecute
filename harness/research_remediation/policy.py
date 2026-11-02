'Configuration contract for research remediation.'
from __future__ import annotations
from harness.compatibility.configuration import resolve_config_path, normalize_configuration_document

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import yaml


@dataclass(frozen=True, slots=True)
class ResearchRemediationPolicy:
    max_claims_per_run: int
    max_targeted_research_runs_per_claim: int
    max_revision_attempts: int
    max_evidence_items_for_revision: int
    max_evidence_chars_for_revision: int
    max_previous_response_chars: int
    operational_timeout_seconds: int
    live_revision_cases: int
    live_min_success_rate: float
    require_loopx_real_003_plan: bool

    def __post_init__(self) -> None:
        for name in (
            "max_claims_per_run",
            "max_targeted_research_runs_per_claim",
            "max_revision_attempts",
            "max_evidence_items_for_revision",
            "max_evidence_chars_for_revision",
            "max_previous_response_chars",
            "operational_timeout_seconds",
            "live_revision_cases",
        ):
            if int(getattr(self, name)) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.max_revision_attempts < 2:
            raise ValueError("Phase 19Q requires bounded revision repair with >=2 attempts")
        if not 0.0 <= self.live_min_success_rate <= 1.0:
            raise ValueError("live_min_success_rate must be in [0,1]")

    @classmethod
    def from_project_root(cls, root: object) -> "ResearchRemediationPolicy":
        path = resolve_config_path(Path(root) / "config" / "phase19q.yaml")
        raw = normalize_configuration_document(yaml.safe_load(path.read_text(encoding="utf-8")))
        if not isinstance(raw, Mapping) or raw.get("schema_version") != 1:
            raise ValueError("phase19q.yaml requires schema_version: 1")
        body = raw.get("quality_guided_research_remediation_v1")
        if not isinstance(body, Mapping):
            raise ValueError("quality_guided_research_remediation_v1 must be mapping")
        live = body.get("live_acceptance")
        if not isinstance(live, Mapping):
            raise ValueError("live_acceptance must be mapping")
        return cls(
            int(body["max_claims_per_run"]),
            int(body["max_targeted_research_runs_per_claim"]),
            int(body["max_revision_attempts"]),
            int(body["max_evidence_items_for_revision"]),
            int(body["max_evidence_chars_for_revision"]),
            int(body["max_previous_response_chars"]),
            int(body["operational_timeout_seconds"]),
            int(live["revision_cases"]),
            float(live["min_success_rate"]),
            bool(live["require_loopx_real_003_plan"]),
        )

# Verified legacy import/pickle aliases, not canonical public exports.
Phase19QPolicy = ResearchRemediationPolicy
