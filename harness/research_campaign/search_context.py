'research campaign.R7.3 bounded deterministic projection of research memory into search context.\n\nR7.0-R7.2 stores typed durable research insights.  R7.3 is deliberately read-only:\nit projects already materialized SEARCH_FEEDBACK records into a compact context that\nSearchPolicyPort implementations may inspect.  It does not create insights, mutate\nsearch state, or change candidate ordering by itself.  Protected TEST is not a valid\nresearch-memory surface and is rejected defensively if it is ever observed here.\n'
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping, Protocol, Sequence

from .insights import (
    ResearchInsightDispositionV1,
    ResearchInsightKindV1,
    ResearchInsightPolarityV1,
    ResearchInsightRecordV1,
    ResearchInsightSurfaceV1,
)
from .serialization import (
    finite_number,
    non_negative_int,
    optional_safe_id,
    positive_int,
    safe_id,
    sha256_hex,
    stable_sha256,
    text,
    timestamp,
)


class ResearchSearchContextStatusV1(StrEnum):
    EMPTY = "EMPTY"
    READY = "READY"
    AMBIGUOUS_EVALUATION_CONTRACT = "AMBIGUOUS_EVALUATION_CONTRACT"


@dataclass(frozen=True, slots=True)
class ResearchSearchContextBudgetV1:
    """Hard projection bounds independent of model/tokenizer implementation."""

    max_items: int = 16
    max_chars: int = 12_000
    max_statement_chars: int = 1_200
    chars_per_estimated_token: int = 4

    def __post_init__(self) -> None:
        object.__setattr__(self, "max_items", positive_int(self.max_items, "max_items"))
        object.__setattr__(self, "max_chars", positive_int(self.max_chars, "max_chars"))
        object.__setattr__(
            self,
            "max_statement_chars",
            positive_int(self.max_statement_chars, "max_statement_chars"),
        )
        object.__setattr__(
            self,
            "chars_per_estimated_token",
            positive_int(self.chars_per_estimated_token, "chars_per_estimated_token"),
        )
        if self.max_items > 1_000:
            raise ValueError("max_items must be <= 1000")
        if self.max_chars < 256 or self.max_chars > 1_000_000:
            raise ValueError("max_chars must be in [256,1000000]")
        if self.max_statement_chars > self.max_chars:
            raise ValueError("max_statement_chars must not exceed max_chars")
        if self.chars_per_estimated_token > 64:
            raise ValueError("chars_per_estimated_token must be <= 64")


@dataclass(frozen=True, slots=True)
class ResearchSearchMemoryItemV1:
    insight_id: str
    kind: ResearchInsightKindV1
    polarity: ResearchInsightPolarityV1
    surface: ResearchInsightSurfaceV1
    statement: str
    subject_key: str
    evaluation_contract_hash: str
    confidence: float
    created_at: str
    evidence_source_ids: tuple[str, ...]
    hypothesis_id: str | None = None
    strategy_id: str | None = None
    relevance_tier: int = 3
    direct_influence_score: float | None = None

    def __post_init__(self) -> None:
        safe_id(self.insight_id, "insight_id")
        if not isinstance(self.kind, ResearchInsightKindV1):
            raise ValueError("kind must be ResearchInsightKindV1")
        if not isinstance(self.polarity, ResearchInsightPolarityV1):
            raise ValueError("polarity must be ResearchInsightPolarityV1")
        if not isinstance(self.surface, ResearchInsightSurfaceV1):
            raise ValueError("surface must be ResearchInsightSurfaceV1")
        # Defensive belt-and-suspenders check: TEST is intentionally absent from
        # ResearchInsightSurfaceV1, and must never enter an actionable search prompt.
        if self.surface.value == "TEST":  # pragma: no cover - impossible for valid enum today
            raise ValueError("protected TEST evidence cannot enter research search context")
        text(self.statement, "statement", max_length=32_000)
        text(self.subject_key, "subject_key", max_length=8_000)
        object.__setattr__(
            self,
            "evaluation_contract_hash",
            sha256_hex(self.evaluation_contract_hash, "evaluation_contract_hash"),
        )
        if isinstance(self.confidence, bool) or not isinstance(self.confidence, (int, float)):
            raise ValueError("confidence must be numeric")
        confidence = float(self.confidence)
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("confidence must be in [0,1]")
        object.__setattr__(self, "confidence", confidence)
        object.__setattr__(self, "created_at", timestamp(self.created_at, "created_at"))
        if not isinstance(self.evidence_source_ids, tuple) or not self.evidence_source_ids:
            raise ValueError("evidence_source_ids must be a non-empty tuple")
        normalized_sources = []
        for source_id in self.evidence_source_ids:
            normalized_sources.append(safe_id(source_id, "evidence_source_ids item"))
        if len(normalized_sources) != len(set(normalized_sources)):
            raise ValueError("evidence_source_ids must be unique")
        object.__setattr__(self, "evidence_source_ids", tuple(normalized_sources))
        object.__setattr__(self, "hypothesis_id", optional_safe_id(self.hypothesis_id, "hypothesis_id"))
        object.__setattr__(self, "strategy_id", optional_safe_id(self.strategy_id, "strategy_id"))
        object.__setattr__(self, "relevance_tier", non_negative_int(self.relevance_tier, "relevance_tier"))
        if self.relevance_tier > 9:
            raise ValueError("relevance_tier must be <= 9")
        if self.direct_influence_score is not None:
            object.__setattr__(
                self,
                "direct_influence_score",
                finite_number(self.direct_influence_score, "direct_influence_score"),
            )

    def render(self) -> str:
        focus = []
        if self.strategy_id is not None:
            focus.append(f"strategy={self.strategy_id}")
        if self.hypothesis_id is not None:
            focus.append(f"hypothesis={self.hypothesis_id}")
        focus_text = "" if not focus else " " + " ".join(focus)
        influence = (
            ""
            if self.direct_influence_score is None
            else f"|direct={self.direct_influence_score:+.3f}"
        )
        return (
            f"[{self.surface.value}|{self.kind.value}|{self.polarity.value}|confidence={self.confidence:.2f}|"
            f"tier={self.relevance_tier}{influence}] {self.statement}"
            f" (insight={self.insight_id}{focus_text})"
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "insight_id": self.insight_id,
            "kind": self.kind.value,
            "polarity": self.polarity.value,
            "surface": self.surface.value,
            "statement": self.statement,
            "subject_key": self.subject_key,
            "evaluation_contract_hash": self.evaluation_contract_hash,
            "confidence": self.confidence,
            "created_at": self.created_at,
            "evidence_source_ids": list(self.evidence_source_ids),
            "hypothesis_id": self.hypothesis_id,
            "strategy_id": self.strategy_id,
            "relevance_tier": self.relevance_tier,
            "direct_influence_score": self.direct_influence_score,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "ResearchSearchMemoryItemV1":
        if raw.get("schema_version") != 1:
            raise ValueError("ResearchSearchMemoryItemV1 schema_version must be 1")
        return cls(
            insight_id=str(raw["insight_id"]),
            kind=ResearchInsightKindV1(str(raw["kind"])),
            polarity=ResearchInsightPolarityV1(str(raw["polarity"])),
            surface=ResearchInsightSurfaceV1(str(raw["surface"])),
            statement=str(raw["statement"]),
            subject_key=str(raw["subject_key"]),
            evaluation_contract_hash=str(raw["evaluation_contract_hash"]),
            confidence=float(raw["confidence"]),
            created_at=str(raw["created_at"]),
            evidence_source_ids=tuple(raw.get("evidence_source_ids", ())),
            hypothesis_id=None if raw.get("hypothesis_id") is None else str(raw["hypothesis_id"]),
            strategy_id=None if raw.get("strategy_id") is None else str(raw["strategy_id"]),
            relevance_tier=int(raw.get("relevance_tier", 3)),
            direct_influence_score=(
                None if raw.get("direct_influence_score") is None else float(raw["direct_influence_score"])
            ),
        )


@dataclass(frozen=True, slots=True)
class ResearchSearchContextV1:
    campaign_id: str
    status: ResearchSearchContextStatusV1
    items: tuple[ResearchSearchMemoryItemV1, ...]
    max_items: int
    max_chars: int
    chars_per_estimated_token: int
    source_insight_count: int
    actionable_insight_count: int
    diagnostic_only_excluded_count: int
    cross_contract_excluded_count: int
    omitted_by_budget_count: int
    evaluation_contract_hash: str | None = None
    focus_experiment_id: str | None = None
    focus_hypothesis_id: str | None = None
    focus_strategy_id: str | None = None

    def __post_init__(self) -> None:
        safe_id(self.campaign_id, "campaign_id")
        if not isinstance(self.status, ResearchSearchContextStatusV1):
            raise ValueError("status must be ResearchSearchContextStatusV1")
        if not isinstance(self.items, tuple) or any(
            not isinstance(item, ResearchSearchMemoryItemV1) for item in self.items
        ):
            raise ValueError("items must be a tuple of ResearchSearchMemoryItemV1")
        max_items = positive_int(self.max_items, "max_items")
        max_chars = positive_int(self.max_chars, "max_chars")
        chars_per_token = positive_int(self.chars_per_estimated_token, "chars_per_estimated_token")
        object.__setattr__(self, "max_items", max_items)
        object.__setattr__(self, "max_chars", max_chars)
        object.__setattr__(self, "chars_per_estimated_token", chars_per_token)
        if len(self.items) > max_items:
            raise ValueError("items exceed max_items")
        for name in (
            "source_insight_count",
            "actionable_insight_count",
            "diagnostic_only_excluded_count",
            "cross_contract_excluded_count",
            "omitted_by_budget_count",
        ):
            object.__setattr__(self, name, non_negative_int(getattr(self, name), name))
        if self.evaluation_contract_hash is not None:
            object.__setattr__(
                self,
                "evaluation_contract_hash",
                sha256_hex(self.evaluation_contract_hash, "evaluation_contract_hash"),
            )
        object.__setattr__(
            self,
            "focus_experiment_id",
            optional_safe_id(self.focus_experiment_id, "focus_experiment_id"),
        )
        object.__setattr__(
            self,
            "focus_hypothesis_id",
            optional_safe_id(self.focus_hypothesis_id, "focus_hypothesis_id"),
        )
        object.__setattr__(
            self,
            "focus_strategy_id",
            optional_safe_id(self.focus_strategy_id, "focus_strategy_id"),
        )
        if self.actionable_insight_count + self.diagnostic_only_excluded_count != self.source_insight_count:
            raise ValueError("search context source counts are inconsistent")
        if self.cross_contract_excluded_count > self.actionable_insight_count:
            raise ValueError("cross_contract_excluded_count exceeds actionable_insight_count")
        if self.status is not ResearchSearchContextStatusV1.AMBIGUOUS_EVALUATION_CONTRACT:
            matching_count = self.actionable_insight_count - self.cross_contract_excluded_count
            if len(self.items) + self.omitted_by_budget_count != matching_count:
                raise ValueError("search context projected/omitted counts are inconsistent")
        if self.status is ResearchSearchContextStatusV1.READY and not self.items:
            raise ValueError("READY search context requires at least one item")
        if self.status is not ResearchSearchContextStatusV1.READY and self.items:
            raise ValueError("non-READY search context must not contain actionable items")
        if self.status is ResearchSearchContextStatusV1.AMBIGUOUS_EVALUATION_CONTRACT:
            if self.evaluation_contract_hash is not None:
                raise ValueError("ambiguous search context must not choose an evaluation contract")
        elif self.items and self.evaluation_contract_hash is None:
            raise ValueError("non-empty search context requires evaluation_contract_hash")
        if any(item.evaluation_contract_hash != self.evaluation_contract_hash for item in self.items):
            raise ValueError("search context items must share evaluation_contract_hash")
        if self.char_count > self.max_chars:
            raise ValueError("rendered search context exceeds max_chars")

    @property
    def char_count(self) -> int:
        return len(self.render())

    @property
    def estimated_tokens(self) -> int:
        if self.char_count == 0:
            return 0
        return (self.char_count + self.chars_per_estimated_token - 1) // self.chars_per_estimated_token

    @property
    def fingerprint(self) -> str:
        return stable_sha256(self.to_dict(include_fingerprint=False))

    def render(self) -> str:
        return "\n".join(item.render() for item in self.items)

    def to_dict(self, *, include_fingerprint: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": 1,
            "campaign_id": self.campaign_id,
            "status": self.status.value,
            "evaluation_contract_hash": self.evaluation_contract_hash,
            "focus_experiment_id": self.focus_experiment_id,
            "focus_hypothesis_id": self.focus_hypothesis_id,
            "focus_strategy_id": self.focus_strategy_id,
            "items": [item.to_dict() for item in self.items],
            "max_items": self.max_items,
            "max_chars": self.max_chars,
            "chars_per_estimated_token": self.chars_per_estimated_token,
            "source_insight_count": self.source_insight_count,
            "actionable_insight_count": self.actionable_insight_count,
            "diagnostic_only_excluded_count": self.diagnostic_only_excluded_count,
            "cross_contract_excluded_count": self.cross_contract_excluded_count,
            "omitted_by_budget_count": self.omitted_by_budget_count,
            "char_count": self.char_count,
            "estimated_tokens": self.estimated_tokens,
        }
        if include_fingerprint:
            payload["fingerprint"] = self.fingerprint
        return payload

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "ResearchSearchContextV1":
        if raw.get("schema_version") != 1:
            raise ValueError("ResearchSearchContextV1 schema_version must be 1")
        result = cls(
            campaign_id=str(raw["campaign_id"]),
            status=ResearchSearchContextStatusV1(str(raw["status"])),
            evaluation_contract_hash=(
                None if raw.get("evaluation_contract_hash") is None else str(raw["evaluation_contract_hash"])
            ),
            focus_experiment_id=(
                None if raw.get("focus_experiment_id") is None else str(raw["focus_experiment_id"])
            ),
            focus_hypothesis_id=(
                None if raw.get("focus_hypothesis_id") is None else str(raw["focus_hypothesis_id"])
            ),
            focus_strategy_id=None if raw.get("focus_strategy_id") is None else str(raw["focus_strategy_id"]),
            items=tuple(ResearchSearchMemoryItemV1.from_dict(item) for item in raw.get("items", ())),
            max_items=int(raw["max_items"]),
            max_chars=int(raw["max_chars"]),
            chars_per_estimated_token=int(raw["chars_per_estimated_token"]),
            source_insight_count=int(raw["source_insight_count"]),
            actionable_insight_count=int(raw["actionable_insight_count"]),
            diagnostic_only_excluded_count=int(raw["diagnostic_only_excluded_count"]),
            cross_contract_excluded_count=int(raw["cross_contract_excluded_count"]),
            omitted_by_budget_count=int(raw["omitted_by_budget_count"]),
        )
        expected = raw.get("fingerprint")
        if expected is not None and expected != result.fingerprint:
            raise ValueError("ResearchSearchContextV1 fingerprint mismatch")
        if raw.get("char_count") is not None and int(raw["char_count"]) != result.char_count:
            raise ValueError("ResearchSearchContextV1 char_count mismatch")
        if raw.get("estimated_tokens") is not None and int(raw["estimated_tokens"]) != result.estimated_tokens:
            raise ValueError("ResearchSearchContextV1 estimated_tokens mismatch")
        return result


class ResearchSearchContextProjectorPort(Protocol):
    def project(
        self,
        *,
        campaign_id: str,
        insights: Sequence[ResearchInsightRecordV1],
        preferred_evaluation_contract_hash: str | None = None,
        focus_experiment_id: str | None = None,
        focus_hypothesis_id: str | None = None,
        focus_strategy_id: str | None = None,
    ) -> ResearchSearchContextV1: ...


@dataclass(frozen=True, slots=True)
class DeterministicResearchSearchContextProjectorV1:
    budget: ResearchSearchContextBudgetV1 = ResearchSearchContextBudgetV1()

    def __post_init__(self) -> None:
        if not isinstance(self.budget, ResearchSearchContextBudgetV1):
            raise ValueError("budget must be ResearchSearchContextBudgetV1")

    @staticmethod
    def _truncate(value: str, maximum: int) -> str:
        stripped = " ".join(value.split())
        if len(stripped) <= maximum:
            return stripped
        if maximum <= 3:
            return stripped[:maximum]
        return stripped[: maximum - 3].rstrip() + "..."

    @staticmethod
    def _relevance_tier(
        insight: ResearchInsightRecordV1,
        *,
        focus_experiment_id: str | None,
        focus_hypothesis_id: str | None,
        focus_strategy_id: str | None,
    ) -> int:
        if focus_experiment_id is not None and any(
            evidence.experiment_id == focus_experiment_id for evidence in insight.evidence
        ):
            return 0
        if focus_hypothesis_id is not None and insight.hypothesis_id == focus_hypothesis_id:
            return 1
        if focus_strategy_id is not None and insight.strategy_id == focus_strategy_id:
            return 2
        if insight.surface in {ResearchInsightSurfaceV1.GATE, ResearchInsightSurfaceV1.REPAIR}:
            return 3
        return 4

    def project(
        self,
        *,
        campaign_id: str,
        insights: Sequence[ResearchInsightRecordV1],
        preferred_evaluation_contract_hash: str | None = None,
        focus_experiment_id: str | None = None,
        focus_hypothesis_id: str | None = None,
        focus_strategy_id: str | None = None,
    ) -> ResearchSearchContextV1:
        safe_id(campaign_id, "campaign_id")
        focus_experiment_id = optional_safe_id(focus_experiment_id, "focus_experiment_id")
        focus_hypothesis_id = optional_safe_id(focus_hypothesis_id, "focus_hypothesis_id")
        focus_strategy_id = optional_safe_id(focus_strategy_id, "focus_strategy_id")
        if preferred_evaluation_contract_hash is not None:
            preferred_evaluation_contract_hash = sha256_hex(
                preferred_evaluation_contract_hash,
                "preferred_evaluation_contract_hash",
            )

        typed: list[ResearchInsightRecordV1] = []
        for insight in insights:
            # Check leakage before the nominal isinstance failure so corruption or a
            # future accidental TEST-bearing model fails with an explicit reason.
            if getattr(getattr(insight, "surface", None), "value", None) == "TEST":
                raise ValueError("protected TEST evidence cannot enter research search context")
            if not isinstance(insight, ResearchInsightRecordV1):
                raise TypeError("insights must contain ResearchInsightRecordV1")
            if insight.campaign_id != campaign_id:
                raise ValueError("research search context insight is cross-campaign")
            typed.append(insight)

        diagnostic_count = sum(
            1 for item in typed if item.disposition is ResearchInsightDispositionV1.DIAGNOSTIC_ONLY
        )
        actionable = [
            item for item in typed if item.disposition is ResearchInsightDispositionV1.SEARCH_FEEDBACK
        ]
        actionable_count = len(actionable)
        contract_hash: str | None = preferred_evaluation_contract_hash
        cross_contract_excluded = 0

        if contract_hash is None and actionable:
            hashes = {item.evaluation_contract_hash for item in actionable}
            if len(hashes) > 1:
                return ResearchSearchContextV1(
                    campaign_id=campaign_id,
                    status=ResearchSearchContextStatusV1.AMBIGUOUS_EVALUATION_CONTRACT,
                    items=(),
                    max_items=self.budget.max_items,
                    max_chars=self.budget.max_chars,
                    chars_per_estimated_token=self.budget.chars_per_estimated_token,
                    source_insight_count=len(typed),
                    actionable_insight_count=actionable_count,
                    diagnostic_only_excluded_count=diagnostic_count,
                    cross_contract_excluded_count=0,
                    omitted_by_budget_count=0,
                    evaluation_contract_hash=None,
                    focus_experiment_id=focus_experiment_id,
                    focus_hypothesis_id=focus_hypothesis_id,
                    focus_strategy_id=focus_strategy_id,
                )
            contract_hash = next(iter(hashes))

        if contract_hash is not None:
            matching = [item for item in actionable if item.evaluation_contract_hash == contract_hash]
            cross_contract_excluded = actionable_count - len(matching)
            actionable = matching

        if not actionable:
            return ResearchSearchContextV1(
                campaign_id=campaign_id,
                status=ResearchSearchContextStatusV1.EMPTY,
                items=(),
                max_items=self.budget.max_items,
                max_chars=self.budget.max_chars,
                chars_per_estimated_token=self.budget.chars_per_estimated_token,
                source_insight_count=len(typed),
                actionable_insight_count=actionable_count,
                diagnostic_only_excluded_count=diagnostic_count,
                cross_contract_excluded_count=cross_contract_excluded,
                omitted_by_budget_count=0,
                evaluation_contract_hash=contract_hash,
                focus_experiment_id=focus_experiment_id,
                focus_hypothesis_id=focus_hypothesis_id,
                focus_strategy_id=focus_strategy_id,
            )

        ranked = sorted(
            actionable,
            key=lambda item: (
                self._relevance_tier(
                    item,
                    focus_experiment_id=focus_experiment_id,
                    focus_hypothesis_id=focus_hypothesis_id,
                    focus_strategy_id=focus_strategy_id,
                ),
                -item.confidence,
                item.insight_id,
            ),
        )

        projected: list[ResearchSearchMemoryItemV1] = []
        omitted = 0
        current_chars = 0
        for insight in ranked:
            if len(projected) >= self.budget.max_items:
                omitted += 1
                continue
            memory_item = ResearchSearchMemoryItemV1(
                insight_id=insight.insight_id,
                kind=insight.kind,
                polarity=insight.polarity,
                surface=insight.surface,
                statement=self._truncate(insight.statement, self.budget.max_statement_chars),
                subject_key=insight.subject_key,
                evaluation_contract_hash=insight.evaluation_contract_hash,
                confidence=insight.confidence,
                created_at=insight.created_at,
                evidence_source_ids=tuple(item.source_id for item in insight.evidence),
                hypothesis_id=insight.hypothesis_id,
                strategy_id=insight.strategy_id,
                relevance_tier=self._relevance_tier(
                    insight,
                    focus_experiment_id=focus_experiment_id,
                    focus_hypothesis_id=focus_hypothesis_id,
                    focus_strategy_id=focus_strategy_id,
                ),
            )
            rendered = memory_item.render()
            added_chars = len(rendered) + (1 if projected else 0)
            if current_chars + added_chars > self.budget.max_chars:
                omitted += 1
                continue
            projected.append(memory_item)
            current_chars += added_chars

        if not projected:
            return ResearchSearchContextV1(
                campaign_id=campaign_id,
                status=ResearchSearchContextStatusV1.EMPTY,
                items=(),
                max_items=self.budget.max_items,
                max_chars=self.budget.max_chars,
                chars_per_estimated_token=self.budget.chars_per_estimated_token,
                source_insight_count=len(typed),
                actionable_insight_count=actionable_count,
                diagnostic_only_excluded_count=diagnostic_count,
                cross_contract_excluded_count=cross_contract_excluded,
                omitted_by_budget_count=omitted,
                evaluation_contract_hash=contract_hash,
                focus_experiment_id=focus_experiment_id,
                focus_hypothesis_id=focus_hypothesis_id,
                focus_strategy_id=focus_strategy_id,
            )

        return ResearchSearchContextV1(
            campaign_id=campaign_id,
            status=ResearchSearchContextStatusV1.READY,
            items=tuple(projected),
            max_items=self.budget.max_items,
            max_chars=self.budget.max_chars,
            chars_per_estimated_token=self.budget.chars_per_estimated_token,
            source_insight_count=len(typed),
            actionable_insight_count=actionable_count,
            diagnostic_only_excluded_count=diagnostic_count,
            cross_contract_excluded_count=cross_contract_excluded,
            omitted_by_budget_count=omitted,
            evaluation_contract_hash=contract_hash,
            focus_experiment_id=focus_experiment_id,
            focus_hypothesis_id=focus_hypothesis_id,
            focus_strategy_id=focus_strategy_id,
        )
