from __future__ import annotations

from pathlib import Path
from typing import Mapping, Sequence

from harness.research_quality.models import ClaimClass
from harness.research_remediation.models import QualityIssueKind

from .constraints import build_constraint
from .github_primary import GitHubPrimaryResolver
from .identity import EntityBindingResolver, TargetEntityResolver
from .policy import EvidenceIntegrityPolicy


class IntegrityRemediationContext:
    def __init__(self, root: Path, policy: EvidenceIntegrityPolicy) -> None:
        self.root = Path(root)
        self.policy = policy
        self.targets = TargetEntityResolver()
        self.binding = EntityBindingResolver()
        self.github = GitHubPrimaryResolver(root) if policy.github_primary_enabled else None
        self._github_cache: dict[tuple[str, tuple[str, ...]], tuple[list[dict], list[dict], dict]] = {}

    def build_for_target(self, *, base_report: Mapping, original_claim: Mapping, target, claim_class: ClaimClass, targeted_report: Mapping) -> dict:
        entity = self.targets.resolve_for_claim(base_report, original_claim)
        constraint = build_constraint(target, entity, claim_class, self.policy)
        base_sources = {str(x.get("source_id", "")): dict(x) for x in base_report.get("sources", []) if isinstance(x, Mapping)}
        targeted_sources = {str(x.get("source_id", "")): dict(x) for x in targeted_report.get("sources", []) if isinstance(x, Mapping)}
        source_pool = {**base_sources, **targeted_sources}
        evidence_pool: list[dict] = []

        # Keep original cited evidence first.
        base_evidence = {str(x.get("evidence_id", "")): x for x in base_report.get("evidence", []) if isinstance(x, Mapping)}
        for ref in original_claim.get("evidence", []):
            if isinstance(ref, Mapping):
                item = base_evidence.get(str(ref.get("evidence_id", "")))
                if isinstance(item, Mapping):
                    evidence_pool.append(dict(item))

        # Then expose other target-bound base evidence.  This recovers distinct
        # primary docs already acquired by external evidence but not cited in the weak claim.
        seen = {str(x.get("evidence_id", "")) for x in evidence_pool}
        for item in base_report.get("evidence", []):
            if not isinstance(item, Mapping):
                continue
            eid = str(item.get("evidence_id", ""))
            if not eid or eid in seen:
                continue
            source = base_sources.get(str(item.get("source_id", "")), {})
            if self.binding.bind(source, entity).eligible:
                evidence_pool.append(dict(item)); seen.add(eid)

        for item in targeted_report.get("evidence", []):
            if not isinstance(item, Mapping):
                continue
            eid = str(item.get("evidence_id", ""))
            if eid and eid not in seen:
                evidence_pool.append(dict(item)); seen.add(eid)

        primary_metrics = {"status": "SKIPPED"}
        primary_sources: list[dict] = []
        primary_evidence: list[dict] = []
        if self.github is not None and entity.github_full_name:
            configured = tuple(self.policy.github_primary_artifacts)
            if claim_class is ClaimClass.LICENSE:
                artifacts = configured
            elif claim_class is ClaimClass.CURRENT_METRIC:
                artifacts = tuple(x for x in configured if x == "repository_metadata")
            else:
                artifacts = tuple(x for x in configured if x in {"repository_metadata", "readme"})
            cache_key = (entity.github_full_name.casefold(), artifacts)
            cached = self._github_cache.get(cache_key)
            if cached is None:
                cached = self.github.acquire(entity, artifacts=artifacts)
                self._github_cache[cache_key] = cached
            primary_sources, primary_evidence, primary_metrics = cached
            for source in primary_sources:
                source_pool[str(source["source_id"])] = source
            for item in primary_evidence:
                eid = str(item["evidence_id"])
                if eid not in seen:
                    evidence_pool.append(item); seen.add(eid)
        return {
            "target_entity": entity,
            "constraint": constraint,
            "sources": source_pool,
            "evidence": evidence_pool,
            "primary_sources": primary_sources,
            "primary_evidence": primary_evidence,
            "primary_metrics": primary_metrics,
        }

# Verified legacy import/pickle aliases, not canonical public exports.
Phase20QRemediationContext = IntegrityRemediationContext
