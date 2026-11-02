from __future__ import annotations
from harness.compatibility.configuration import resolve_config_path, normalize_configuration_document

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import yaml


@dataclass(frozen=True, slots=True)
class EvidenceIntegrityPolicy:
    expected_parent_commit: str
    current_metric_freshness_hours: int
    max_revision_evidence_items: int
    max_relevance_candidates: int
    max_relevance_selected: int
    bm25_k1: float
    bm25_b: float
    browser_escalation_min_text_chars: int
    github_primary_enabled: bool
    github_primary_artifacts: tuple[str, ...]
    reject_other_github_repositories: bool
    exact_repository_required_for: tuple[str, ...]
    require_new_document_for_document_independence: bool
    require_fresh_primary_for_current_metric: bool
    fail_closed_on_unclosed_defect: bool
    fallback_action: str

    def __post_init__(self) -> None:
        for name in (
            "current_metric_freshness_hours",
            "max_revision_evidence_items",
            "max_relevance_candidates",
            "max_relevance_selected",
            "browser_escalation_min_text_chars",
        ):
            if int(getattr(self, name)) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.max_relevance_selected > self.max_relevance_candidates:
            raise ValueError("max_relevance_selected cannot exceed max_relevance_candidates")
        if self.bm25_k1 <= 0 or not 0 <= self.bm25_b <= 1:
            raise ValueError("invalid BM25 parameters")
        if self.fallback_action not in {"DROP"}:
            raise ValueError("Phase20Q V1 supports fail-closed fallback_action=DROP only")

    @classmethod
    def from_project_root(cls, root: object) -> "EvidenceIntegrityPolicy":
        path = resolve_config_path(Path(root) / "config" / "phase20q.yaml")
        raw = normalize_configuration_document(yaml.safe_load(path.read_text(encoding="utf-8")))
        if not isinstance(raw, Mapping) or raw.get("schema_version") != 1:
            raise ValueError("phase20q.yaml requires schema_version: 1")
        body = raw.get("research_evidence_integrity_v1")
        if not isinstance(body, Mapping):
            raise ValueError("phase20q research_evidence_integrity_v1 must be mapping")
        binding = body.get("entity_binding")
        remediation = body.get("remediation")
        if not isinstance(binding, Mapping) or not isinstance(remediation, Mapping):
            raise ValueError("phase20q entity_binding/remediation must be mappings")
        return cls(
            expected_parent_commit=str(body["expected_parent_commit"]),
            current_metric_freshness_hours=int(body["current_metric_freshness_hours"]),
            max_revision_evidence_items=int(body["max_revision_evidence_items"]),
            max_relevance_candidates=int(body["max_relevance_candidates"]),
            max_relevance_selected=int(body["max_relevance_selected"]),
            bm25_k1=float(body["bm25_k1"]),
            bm25_b=float(body["bm25_b"]),
            browser_escalation_min_text_chars=int(body["browser_escalation_min_text_chars"]),
            github_primary_enabled=bool(body["github_primary_enabled"]),
            github_primary_artifacts=tuple(str(x) for x in body.get("github_primary_artifacts", ())),
            reject_other_github_repositories=bool(binding["reject_other_github_repositories"]),
            exact_repository_required_for=tuple(str(x).upper() for x in binding.get("exact_repository_required_for", ())),
            require_new_document_for_document_independence=bool(remediation["require_new_document_for_document_independence"]),
            require_fresh_primary_for_current_metric=bool(remediation["require_fresh_primary_for_current_metric"]),
            fail_closed_on_unclosed_defect=bool(remediation["fail_closed_on_unclosed_defect"]),
            fallback_action=str(remediation["fallback_action"]).upper(),
        )

# Verified legacy import/pickle aliases, not canonical public exports.
Phase20QPolicy = EvidenceIntegrityPolicy
