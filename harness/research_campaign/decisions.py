'Typed search decisions for research campaign research campaigns.'
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping

from .serialization import freeze_json, plain, safe_id, text, timestamp, unique_ids


class SearchDecisionActionV1(StrEnum):
    KEEP = "KEEP"
    DROP = "DROP"
    BRANCH = "BRANCH"
    RETEST = "RETEST"
    PRUNE = "PRUNE"
    UPDATE_CHAMPION = "UPDATE_CHAMPION"
    STOP = "STOP"


@dataclass(frozen=True, slots=True)
class SearchDecisionV1:
    decision_id: str
    campaign_id: str
    experiment_id: str
    action: SearchDecisionActionV1
    reason: str
    created_at: str
    incumbent_experiment_id: str | None = None
    evidence_experiment_ids: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        safe_id(self.decision_id, "decision_id")
        safe_id(self.campaign_id, "campaign_id")
        safe_id(self.experiment_id, "experiment_id")
        if not isinstance(self.action, SearchDecisionActionV1):
            raise ValueError("action must be SearchDecisionActionV1")
        text(self.reason, "reason", max_length=32_000)
        object.__setattr__(self, "created_at", timestamp(self.created_at, "created_at"))
        if self.incumbent_experiment_id is not None:
            safe_id(self.incumbent_experiment_id, "incumbent_experiment_id")
        evidence = unique_ids(self.evidence_experiment_ids, "evidence_experiment_ids", maximum=256)
        object.__setattr__(self, "evidence_experiment_ids", evidence)
        frozen = freeze_json(self.metadata, "metadata")
        if not isinstance(frozen, Mapping):
            raise ValueError("metadata must be mapping")
        object.__setattr__(self, "metadata", frozen)
        if self.incumbent_experiment_id == self.experiment_id:
            raise ValueError("incumbent_experiment_id must differ from experiment_id")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "decision_id": self.decision_id,
            "campaign_id": self.campaign_id,
            "experiment_id": self.experiment_id,
            "action": self.action.value,
            "reason": self.reason,
            "created_at": self.created_at,
            "incumbent_experiment_id": self.incumbent_experiment_id,
            "evidence_experiment_ids": list(self.evidence_experiment_ids),
            "metadata": plain(self.metadata),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "SearchDecisionV1":
        if raw.get("schema_version") != 1:
            raise ValueError("SearchDecisionV1 schema_version must be 1")
        return cls(
            decision_id=str(raw["decision_id"]),
            campaign_id=str(raw["campaign_id"]),
            experiment_id=str(raw["experiment_id"]),
            action=SearchDecisionActionV1(str(raw["action"])),
            reason=str(raw["reason"]),
            created_at=str(raw["created_at"]),
            incumbent_experiment_id=(
                None if raw.get("incumbent_experiment_id") is None else str(raw["incumbent_experiment_id"])
            ),
            evidence_experiment_ids=tuple(raw.get("evidence_experiment_ids", ())),
            metadata=raw.get("metadata", {}),
        )
