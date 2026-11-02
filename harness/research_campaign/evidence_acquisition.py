'research stopping campaign evidence adapter over persisted research state and external evidence.\n\nRepository inspection is read-only. Optional external acquisition is supplied by\nthe existing external evidence service boundary; failures are retained as diagnostics.\n'
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .evidence import (
    ResearchEvidenceBudgetV1, ResearchEvidenceClassificationV1, ResearchEvidenceItemV1,
    ResearchEvidencePacketV1, ResearchEvidenceSourceTypeV1,
)
from .serialization import stable_sha256


@dataclass(frozen=True, slots=True)
class CampaignResearchEvidenceAcquirerV1:
    repository_root: Path
    clock: Callable[[], str]
    external_research: Callable[..., Any] | None = None
    external_required: bool = False

    def acquire(self, *, campaign_id: str, budget: ResearchEvidenceBudgetV1, context: Any = None) -> ResearchEvidencePacketV1:
        created_at = self.clock()
        items: list[ResearchEvidenceItemV1] = []
        diagnostics: list[str] = []

        def add(source_type: ResearchEvidenceSourceTypeV1, identity: str, content: str,
                classification: ResearchEvidenceClassificationV1, provenance: tuple[str, ...],
                relevance: float = 1.0, confidence: float = 1.0) -> None:
            if len(items) >= budget.max_items:
                return
            remaining = budget.max_context_characters - sum(len(item.content) for item in items)
            if remaining <= 0:
                return
            bounded = content[: min(32_000, remaining)]
            if not bounded:
                return
            identity_hash = stable_sha256({"campaign": campaign_id, "type": source_type.value,
                                           "identity": identity, "content": bounded})
            items.append(ResearchEvidenceItemV1(f"p23e-{identity_hash[:24]}", campaign_id, source_type,
                identity, bounded, created_at, relevance, provenance, confidence, classification))

        if context is not None:
            campaign_context = context.campaign_context
            champion = campaign_context.champion
            add(ResearchEvidenceSourceTypeV1.CONFIGURATION, "campaign-objective",
                campaign_context.contract.objective, ResearchEvidenceClassificationV1.CONTEXT,
                (f"contract:{campaign_context.contract.contract_hash}",))
            if champion is not None:
                add(ResearchEvidenceSourceTypeV1.CHAMPION, champion.spec.experiment_id,
                    str(champion.spec.to_dict()), ResearchEvidenceClassificationV1.SCIENTIFIC,
                    (f"experiment:{champion.spec.experiment_id}",))
            for experiment in context.experiments[-min(len(context.experiments), 12):]:
                classification = (ResearchEvidenceClassificationV1.DIAGNOSTIC
                    if experiment.result is not None and experiment.result.failure_kind is not None
                    else ResearchEvidenceClassificationV1.SCIENTIFIC)
                add(ResearchEvidenceSourceTypeV1.EXPERIMENT_HISTORY, experiment.spec.experiment_id,
                    str({"spec": experiment.spec.to_dict(), "result": None if experiment.result is None else experiment.result.to_dict()}),
                    classification, (f"experiment:{experiment.spec.experiment_id}",), 0.9, 1.0)
            for belief in context.belief_set.beliefs:
                add(ResearchEvidenceSourceTypeV1.RESEARCH_MEMORY, belief.belief_id, str(belief.to_dict()),
                    ResearchEvidenceClassificationV1.SCIENTIFIC, (f"belief:{belief.belief_id}",), 0.9, belief.confidence)

        root = Path(self.repository_root)
        source_files = sorted(path for path in root.rglob("*.py") if ".git" not in path.parts)[:8]
        add(ResearchEvidenceSourceTypeV1.REPOSITORY, "repository-python-index",
            "\n".join(str(path.relative_to(root)).replace("\\", "/") for path in source_files),
            ResearchEvidenceClassificationV1.CONTEXT, (f"repo:{root.resolve()}",), 0.7, 1.0)

        if self.external_research is not None and budget.max_external_rounds > 0 and budget.max_tool_calls > 0:
            try:
                report = self.external_research(campaign_id=campaign_id, max_rounds=budget.max_external_rounds,
                                               max_tool_calls=budget.max_tool_calls)
                for evidence in tuple(getattr(report, "evidence", ()))[:budget.max_items]:
                    add(ResearchEvidenceSourceTypeV1.EXTERNAL, str(evidence.evidence_id), str(evidence.excerpt),
                        ResearchEvidenceClassificationV1.CONTEXT,
                        (f"phase18:{getattr(report, 'research_id', campaign_id)}", f"source:{evidence.source_id}"), 0.7, 0.8)
            except Exception as exc:
                diagnostic = f"external acquisition diagnostic: {type(exc).__name__}: {exc}"
                diagnostics.append(diagnostic)
                if self.external_required:
                    raise RuntimeError(diagnostic) from exc

        packet_identity = stable_sha256({"campaign": campaign_id, "items": [item.fingerprint for item in items],
                                         "diagnostics": diagnostics, "budget": budget.to_dict()})
        return ResearchEvidencePacketV1(f"p23packet-{packet_identity[:20]}", campaign_id, created_at,
                                        budget, tuple(items), tuple(diagnostics))

# Version-one constructor/attribute compatibility; wire keys stay byte-stable.
from harness.compatibility.configuration import install_legacy_fields
install_legacy_fields(CampaignResearchEvidenceAcquirerV1, {'phase18_research': 'external_research'})
