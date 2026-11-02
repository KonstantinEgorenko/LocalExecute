'Configuration contract for research quality.'
from __future__ import annotations
from harness.compatibility.configuration import resolve_config_path, normalize_configuration_document

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import yaml

from .models import ClaimClass


@dataclass(frozen=True, slots=True)
class ResearchQualityPolicy:
    important_min_unique_documents: int
    important_min_unique_source_families: int
    critical_min_primary_documents: int
    max_entailment_attempts: int
    max_previous_response_chars: int
    current_metric_freshness_hours: int
    topic_claim_classes: Mapping[str, ClaimClass]
    live_entailment_cases: int
    live_min_success_rate: float
    require_loopx_real_003: bool

    def __post_init__(self) -> None:
        for name in (
            "important_min_unique_documents",
            "important_min_unique_source_families",
            "critical_min_primary_documents",
            "max_entailment_attempts",
            "max_previous_response_chars",
            "current_metric_freshness_hours",
            "live_entailment_cases",
        ):
            if int(getattr(self, name)) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.max_entailment_attempts < 2:
            raise ValueError("Phase 19 requires bounded entailment repair with >=2 attempts")
        if not 0.0 <= self.live_min_success_rate <= 1.0:
            raise ValueError("live_min_success_rate must be in [0,1]")

    def claim_class_for(self, topic: str) -> ClaimClass:
        return self.topic_claim_classes.get(topic.casefold(), ClaimClass.GENERAL)

    @classmethod
    def from_project_root(cls, root: object) -> "ResearchQualityPolicy":
        path = resolve_config_path(Path(root) / "config" / "phase19.yaml")
        raw = normalize_configuration_document(yaml.safe_load(path.read_text(encoding="utf-8")))
        if not isinstance(raw, Mapping) or raw.get("schema_version") != 1:
            raise ValueError("phase19.yaml requires schema_version: 1")
        body = raw.get("research_quality_evaluation_v1")
        if not isinstance(body, Mapping):
            raise ValueError("phase19 research_quality_evaluation_v1 must be mapping")
        classes = body.get("topic_claim_classes", {})
        if not isinstance(classes, Mapping):
            raise ValueError("topic_claim_classes must be mapping")
        parsed_classes = {
            str(key).casefold(): ClaimClass(str(value))
            for key, value in classes.items()
        }
        live = body.get("live_acceptance")
        if not isinstance(live, Mapping):
            raise ValueError("phase19 live_acceptance must be mapping")
        return cls(
            int(body["important_min_unique_documents"]),
            int(body["important_min_unique_source_families"]),
            int(body["critical_min_primary_documents"]),
            int(body["max_entailment_attempts"]),
            int(body["max_previous_response_chars"]),
            int(body["current_metric_freshness_hours"]),
            parsed_classes,
            int(live["entailment_cases"]),
            float(live["min_success_rate"]),
            bool(live["require_loopx_real_003"]),
        )

# Verified legacy import/pickle aliases, not canonical public exports.
Phase19Policy = ResearchQualityPolicy
