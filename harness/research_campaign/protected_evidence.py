'research diagnostics additive no-feedback filter over the frozen research stopping evidence acquirer.'
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .evidence import (
    ResearchEvidenceAcquisitionPort,
    ResearchEvidenceBudgetV1,
    ResearchEvidenceItemV1,
    ResearchEvidencePacketV1,
    ResearchEvidenceSourceTypeV1,
)
from .serialization import stable_sha256


def _normalize(path: str) -> str:
    return path.strip().replace("\\", "/").strip("/")


def _matches(path: str, protected_paths: tuple[str, ...]) -> bool:
    candidate = _normalize(path)
    if not candidate:
        return False
    candidate_fold = candidate.casefold()
    for protected in protected_paths:
        root = _normalize(protected).casefold()
        if candidate_fold == root or candidate_fold.startswith(root + "/"):
            return True
    return False


@dataclass(frozen=True, slots=True)
class ProtectedEvidenceAcquirerV1:
    """Filters protected repository path references before model proposal generation.

    This wrapper does not alter scientific experiment history. It specifically
    prevents the frozen repository index seam from exposing protected holdout
    path names to model-backed proposal generation.
    """

    delegate: ResearchEvidenceAcquisitionPort
    protected_paths: tuple[str, ...]

    def acquire(
        self,
        *,
        campaign_id: str,
        budget: ResearchEvidenceBudgetV1,
        context: Any = None,
    ) -> ResearchEvidencePacketV1:
        packet = self.delegate.acquire(campaign_id=campaign_id, budget=budget, context=context)
        if not self.protected_paths:
            return packet

        filtered_count = 0
        items: list[ResearchEvidenceItemV1] = []
        for item in packet.items:
            if item.source_type is not ResearchEvidenceSourceTypeV1.REPOSITORY:
                items.append(item)
                continue

            if _matches(item.source_identity, self.protected_paths):
                filtered_count += 1
                continue

            # Current frozen Phase23 repository evidence uses a newline-delimited
            # repository-python-index. Filter only path-like index lines; do not
            # rewrite arbitrary repository prose/content.
            if item.source_identity == "repository-python-index":
                original_lines = item.content.splitlines()
                kept_lines = [
                    line for line in original_lines if not _matches(line, self.protected_paths)
                ]
                filtered_count += len(original_lines) - len(kept_lines)
                if not kept_lines:
                    continue
                filtered_content = "\n".join(kept_lines)
                if filtered_content != item.content:
                    identity = stable_sha256(
                        {
                            "schema_version": 1,
                            "source_evidence_id": item.evidence_id,
                            "filtered_content": filtered_content,
                        }
                    )
                    item = ResearchEvidenceItemV1(
                        evidence_id=f"p24e-{identity[:24]}",
                        campaign_id=item.campaign_id,
                        source_type=item.source_type,
                        source_identity=item.source_identity,
                        content=filtered_content,
                        freshness=item.freshness,
                        relevance=item.relevance,
                        provenance_ids=item.provenance_ids,
                        confidence=item.confidence,
                        classification=item.classification,
                    )
            items.append(item)

        if filtered_count == 0:
            return packet

        diagnostics = tuple(packet.acquisition_diagnostics) + (
            f"protected repository evidence filtered: {filtered_count} path reference(s)",
        )
        packet_identity = stable_sha256(
            {
                "schema_version": 1,
                "source_packet": packet.fingerprint,
                "items": [item.fingerprint for item in items],
                "diagnostics": diagnostics,
            }
        )
        return ResearchEvidencePacketV1(
            packet_id=f"p24packet-{packet_identity[:20]}",
            campaign_id=packet.campaign_id,
            created_at=packet.created_at,
            budget=packet.budget,
            items=tuple(items),
            acquisition_diagnostics=diagnostics,
        )
