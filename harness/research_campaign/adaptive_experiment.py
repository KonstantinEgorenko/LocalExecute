'research campaign.R7.6-F-J deterministic hypothesis-to-experiment adaptation.\n\nR7.6-A-E creates bounded durable research hypotheses from consolidated beliefs.\nThis module materializes one controlled unseen ExperimentSpecV1 from such a\nhypothesis.  It does not execute work and does not bypass the existing R6\nExperimentExecutionPort.  The first version deliberately mutates only scope-\npreserving strategy dimensions so accumulated belief evidence remains comparable.\n'
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence

from .beliefs import DeterministicResearchBeliefConsolidatorV1
from .hypothesis_adaptation import HypothesisProposalV1
from .models import ExperimentSpecV1
from .search import StrategyDimensionTargetV1
from .serialization import (
    canonical_json,
    freeze_json,
    plain,
    safe_id,
    stable_sha256,
    timestamp,
)
from .store import StoredExperimentV1


class AdaptiveExperimentError(RuntimeError):
    pass


_SCOPE_PRESERVING_TARGETS = {
    StrategyDimensionTargetV1.PARAMETER,
    StrategyDimensionTargetV1.SAMPLING_CONFIG,
    StrategyDimensionTargetV1.SEED,
    StrategyDimensionTargetV1.RESOURCE_BUDGET,
    StrategyDimensionTargetV1.EXECUTION,
}

_MAPPING_TARGETS = {
    StrategyDimensionTargetV1.PARAMETER,
    StrategyDimensionTargetV1.SAMPLING_CONFIG,
    StrategyDimensionTargetV1.RESOURCE_BUDGET,
    StrategyDimensionTargetV1.EXECUTION,
}

_REPAIR_EXECUTION_KEYS = {
    "repair_evidence_fingerprint",
    "repair_source_gate_evaluation_id",
    "repair_source_experiment_id",
    "repair_source_objective_fingerprint",
}

_RESERVED_EXECUTION_KEYS = _REPAIR_EXECUTION_KEYS | {
    "adaptive_research",
}


@dataclass(frozen=True, slots=True)
class AdaptiveDimensionRuleV1:
    """One deterministic mutation axis for adaptive experiments.

    R7.6 first closes the loop without changing model/evaluator/tool scope.  Those
    changes would invalidate the CURRENT belief that motivated adaptation and need
    a later cross-scope research policy.
    """

    name: str
    target: StrategyDimensionTargetV1
    values: tuple[Any, ...]
    key: str | None = None

    def __post_init__(self) -> None:
        safe_id(self.name, "name")
        if not isinstance(self.target, StrategyDimensionTargetV1):
            raise ValueError("target must be StrategyDimensionTargetV1")
        if self.target not in _SCOPE_PRESERVING_TARGETS:
            raise ValueError(f"adaptive target {self.target.value} changes research belief scope")
        if not isinstance(self.values, tuple) or not self.values:
            raise ValueError("values must be a non-empty tuple")
        if len(self.values) > 10_000:
            raise ValueError("values exceeds maximum=10000")
        frozen_values = tuple(freeze_json(value, f"values[{index}]") for index, value in enumerate(self.values))
        identities = tuple(canonical_json(value) for value in frozen_values)
        if len(set(identities)) != len(identities):
            raise ValueError("values must not contain duplicate logical values")
        object.__setattr__(self, "values", frozen_values)
        if self.target in _MAPPING_TARGETS:
            if self.key is None:
                raise ValueError(f"{self.target.value} adaptive rule requires key")
            safe_id(self.key, "key")
            if self.target is StrategyDimensionTargetV1.EXECUTION and self.key in _RESERVED_EXECUTION_KEYS:
                raise ValueError(f"execution key is controller-owned/reserved: {self.key}")
        elif self.key is not None:
            raise ValueError(f"{self.target.value} adaptive rule must not define key")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "name": self.name,
            "target": self.target.value,
            "values": [plain(value) for value in self.values],
            "key": self.key,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "AdaptiveDimensionRuleV1":
        if raw.get("schema_version") != 1:
            raise ValueError("AdaptiveDimensionRuleV1 schema_version must be 1")
        return cls(
            name=str(raw["name"]),
            target=StrategyDimensionTargetV1(str(raw["target"])),
            values=tuple(raw.get("values", ())),
            key=None if raw.get("key") is None else str(raw["key"]),
        )


@dataclass(frozen=True, slots=True)
class AdaptiveExperimentProposalV1:
    proposal_id: str
    campaign_id: str
    hypothesis_proposal: HypothesisProposalV1
    source_experiment_id: str
    source_experiment_fingerprint: str
    dimension_name: str
    chosen_value: Any
    experiment: ExperimentSpecV1

    def __post_init__(self) -> None:
        safe_id(self.proposal_id, "proposal_id")
        safe_id(self.campaign_id, "campaign_id")
        if not isinstance(self.hypothesis_proposal, HypothesisProposalV1):
            raise ValueError("hypothesis_proposal must be HypothesisProposalV1")
        if not isinstance(self.experiment, ExperimentSpecV1):
            raise ValueError("experiment must be ExperimentSpecV1")
        if self.hypothesis_proposal.campaign_id != self.campaign_id:
            raise ValueError("hypothesis proposal campaign_id mismatch")
        safe_id(self.source_experiment_id, "source_experiment_id")
        if len(self.source_experiment_fingerprint) != 64:
            raise ValueError("source_experiment_fingerprint must be SHA-256")
        safe_id(self.dimension_name, "dimension_name")
        object.__setattr__(self, "chosen_value", freeze_json(self.chosen_value, "chosen_value"))
        spec = self.experiment
        hypothesis = self.hypothesis_proposal.hypothesis
        if spec.campaign_id != self.campaign_id:
            raise ValueError("adaptive experiment campaign_id mismatch")
        if spec.hypothesis_id != hypothesis.hypothesis_id:
            raise ValueError("adaptive experiment must reference generated hypothesis")
        if spec.strategy_id != self.hypothesis_proposal.source_strategy_id:
            raise ValueError("adaptive experiment must preserve semantic source strategy_id")
        if spec.parent_experiment_ids != (self.source_experiment_id,):
            raise ValueError("adaptive experiment must have exactly one source experiment parent")
        if spec.evaluation_contract_hash != self.hypothesis_proposal.evaluation_contract_hash:
            raise ValueError("adaptive experiment must preserve hypothesis EvaluationContractHash")
        if self.dimension_name not in self.hypothesis_proposal.target_strategy_dimensions:
            raise ValueError("adaptive experiment dimension is not authorized by hypothesis proposal")
        if spec.fingerprint == self.source_experiment_fingerprint:
            raise ValueError("adaptive experiment must have a new logical fingerprint")

    @property
    def fingerprint(self) -> str:
        return stable_sha256(
            {
                "schema_version": 1,
                "campaign_id": self.campaign_id,
                "hypothesis_novelty": self.hypothesis_proposal.novelty_fingerprint,
                "source_experiment_fingerprint": self.source_experiment_fingerprint,
                "dimension_name": self.dimension_name,
                "chosen_value": plain(self.chosen_value),
                "experiment_fingerprint": self.experiment.fingerprint,
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "proposal_id": self.proposal_id,
            "campaign_id": self.campaign_id,
            "hypothesis_proposal": self.hypothesis_proposal.to_dict(),
            "source_experiment_id": self.source_experiment_id,
            "source_experiment_fingerprint": self.source_experiment_fingerprint,
            "dimension_name": self.dimension_name,
            "chosen_value": plain(self.chosen_value),
            "experiment": self.experiment.to_dict(),
            "fingerprint": self.fingerprint,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "AdaptiveExperimentProposalV1":
        if raw.get("schema_version") != 1:
            raise ValueError("AdaptiveExperimentProposalV1 schema_version must be 1")
        result = cls(
            proposal_id=str(raw["proposal_id"]),
            campaign_id=str(raw["campaign_id"]),
            hypothesis_proposal=HypothesisProposalV1.from_dict(raw["hypothesis_proposal"]),
            source_experiment_id=str(raw["source_experiment_id"]),
            source_experiment_fingerprint=str(raw["source_experiment_fingerprint"]),
            dimension_name=str(raw["dimension_name"]),
            chosen_value=raw["chosen_value"],
            experiment=ExperimentSpecV1.from_dict(raw["experiment"]),
        )
        if raw.get("fingerprint") is not None and raw["fingerprint"] != result.fingerprint:
            raise ValueError("AdaptiveExperimentProposalV1 fingerprint mismatch")
        return result


class AdaptiveExperimentMaterializationPort(Protocol):
    @property
    def dimension_names(self) -> tuple[str, ...]: ...

    def propose(
        self,
        hypothesis_proposal: HypothesisProposalV1,
        source: StoredExperimentV1,
        *,
        known_experiment_fingerprints: frozenset[str],
    ) -> AdaptiveExperimentProposalV1 | None: ...


@dataclass(frozen=True, slots=True)
class DeterministicAdaptiveExperimentMaterializerV1:
    rules: tuple[AdaptiveDimensionRuleV1, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.rules, tuple) or not self.rules:
            raise ValueError("rules must be a non-empty tuple")
        if any(not isinstance(item, AdaptiveDimensionRuleV1) for item in self.rules):
            raise ValueError("rules must contain AdaptiveDimensionRuleV1")
        names = tuple(item.name for item in self.rules)
        if len(set(names)) != len(names):
            raise ValueError("adaptive rule names must be unique")

    @property
    def dimension_names(self) -> tuple[str, ...]:
        return tuple(sorted(item.name for item in self.rules))

    def propose(
        self,
        hypothesis_proposal: HypothesisProposalV1,
        source: StoredExperimentV1,
        *,
        known_experiment_fingerprints: frozenset[str],
    ) -> AdaptiveExperimentProposalV1 | None:
        if not isinstance(hypothesis_proposal, HypothesisProposalV1):
            raise TypeError("hypothesis_proposal must be HypothesisProposalV1")
        if not isinstance(source, StoredExperimentV1):
            raise TypeError("source must be StoredExperimentV1")
        if not isinstance(known_experiment_fingerprints, frozenset):
            raise TypeError("known_experiment_fingerprints must be frozenset")
        if source.spec.campaign_id != hypothesis_proposal.campaign_id:
            raise AdaptiveExperimentError("source experiment is cross-campaign")
        source_scope = DeterministicResearchBeliefConsolidatorV1.scope_for_experiment(source)
        if source_scope.fingerprint != hypothesis_proposal.scope_fingerprint:
            raise AdaptiveExperimentError("source experiment scope does not match adaptive hypothesis")
        dimension = hypothesis_proposal.target_strategy_dimensions[0]
        rule = next((item for item in self.rules if item.name == dimension), None)
        if rule is None:
            raise AdaptiveExperimentError(f"no adaptive materialization rule for dimension {dimension}")

        for value in rule.values:
            if self._is_noop(source.spec, rule, value):
                continue
            spec = self._materialize(hypothesis_proposal, source, rule, value)
            if spec.fingerprint == source.spec.fingerprint:
                continue
            if spec.fingerprint in known_experiment_fingerprints:
                continue
            digest = stable_sha256(
                {
                    "hypothesis_novelty": hypothesis_proposal.novelty_fingerprint,
                    "source_fingerprint": source.spec.fingerprint,
                    "dimension": rule.name,
                    "value": plain(value),
                    "experiment_fingerprint": spec.fingerprint,
                }
            )
            return AdaptiveExperimentProposalV1(
                proposal_id=f"r76x-{digest[:24]}",
                campaign_id=spec.campaign_id,
                hypothesis_proposal=hypothesis_proposal,
                source_experiment_id=source.spec.experiment_id,
                source_experiment_fingerprint=source.spec.fingerprint,
                dimension_name=rule.name,
                chosen_value=value,
                experiment=spec,
            )
        return None

    @staticmethod
    def _is_noop(spec: ExperimentSpecV1, rule: AdaptiveDimensionRuleV1, value: Any) -> bool:
        missing = object()
        current: Any = missing
        if rule.target is StrategyDimensionTargetV1.PARAMETER:
            current = spec.parameters.get(rule.key or "", missing)
        elif rule.target is StrategyDimensionTargetV1.SAMPLING_CONFIG:
            current = spec.sampling_config.get(rule.key or "", missing)
        elif rule.target is StrategyDimensionTargetV1.RESOURCE_BUDGET:
            current = spec.resource_budget.get(rule.key or "", missing)
        elif rule.target is StrategyDimensionTargetV1.EXECUTION:
            current = spec.execution.get(rule.key or "", missing)
        elif rule.target is StrategyDimensionTargetV1.SEED:
            current = spec.seed
        if current is missing:
            return False
        return canonical_json(current) == canonical_json(value)

    def _materialize(
        self,
        hypothesis_proposal: HypothesisProposalV1,
        source: StoredExperimentV1,
        rule: AdaptiveDimensionRuleV1,
        value: Any,
    ) -> ExperimentSpecV1:
        source_spec = source.spec
        parameters = dict(plain(source_spec.parameters))
        sampling = dict(plain(source_spec.sampling_config))
        resources = dict(plain(source_spec.resource_budget))
        execution = dict(plain(source_spec.execution))
        # Once a repair descendant becomes the source of a new research adaptation,
        # it is no longer itself an R6.6 repair experiment.  Preserve ancestry via
        # parent_experiment_ids but remove repair-only execution markers so belief
        # consolidation does not misclassify the adaptive descendant as another repair.
        for key in _REPAIR_EXECUTION_KEYS:
            execution.pop(key, None)
        seed = source_spec.seed

        if rule.target is StrategyDimensionTargetV1.PARAMETER:
            parameters[rule.key or ""] = plain(value)
        elif rule.target is StrategyDimensionTargetV1.SAMPLING_CONFIG:
            sampling[rule.key or ""] = plain(value)
        elif rule.target is StrategyDimensionTargetV1.RESOURCE_BUDGET:
            resources[rule.key or ""] = plain(value)
        elif rule.target is StrategyDimensionTargetV1.EXECUTION:
            execution[rule.key or ""] = plain(value)
        elif rule.target is StrategyDimensionTargetV1.SEED:
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise AdaptiveExperimentError("adaptive SEED value must be non-negative integer")
            seed = value
        else:  # pragma: no cover - guarded by AdaptiveDimensionRuleV1
            raise AdaptiveExperimentError(f"unsupported adaptive target {rule.target.value}")

        global_constraints = execution.get("global_constraints", {})
        if not isinstance(global_constraints, Mapping):
            raise AdaptiveExperimentError("source execution.global_constraints must be mapping")
        constraints = dict(plain(global_constraints))
        constraints["adaptive_research"] = {
            "schema_version": 1,
            "hypothesis_id": hypothesis_proposal.hypothesis.hypothesis_id,
            "hypothesis_statement": hypothesis_proposal.hypothesis.statement,
            "hypothesis_mechanism": hypothesis_proposal.hypothesis.mechanism,
            "predicted_effect": hypothesis_proposal.hypothesis.predicted_effect,
            "falsifier": hypothesis_proposal.hypothesis.falsifier,
            "source_experiment_id": source_spec.experiment_id,
            "source_strategy_id": hypothesis_proposal.source_strategy_id,
            "target_dimension": rule.name,
            "target_kind": rule.target.value,
            "target_key": rule.key,
            "target_value": plain(value),
            "hypothesis_novelty_fingerprint": hypothesis_proposal.novelty_fingerprint,
        }
        execution["global_constraints"] = constraints

        # A successful candidate commit is the strongest descendant starting point.
        # Otherwise retain the source's original code base.  Belief scope deliberately
        # excludes Git commit, so this does not invalidate comparable evaluator/model/tool scope.
        code_base_commit = source_spec.code_base_commit
        if source.result is not None and source.result.candidate_commit is not None:
            code_base_commit = source.result.candidate_commit

        digest = stable_sha256(
            {
                "source_experiment_fingerprint": source_spec.fingerprint,
                "hypothesis_novelty": hypothesis_proposal.novelty_fingerprint,
                "dimension": rule.name,
                "value": plain(value),
            }
        )
        created_at = timestamp(hypothesis_proposal.created_at, "hypothesis_proposal.created_at")
        return ExperimentSpecV1(
            experiment_id=f"r76e-{digest[:24]}",
            campaign_id=source_spec.campaign_id,
            hypothesis_id=hypothesis_proposal.hypothesis.hypothesis_id,
            parent_experiment_ids=(source_spec.experiment_id,),
            strategy_id=hypothesis_proposal.source_strategy_id,
            parameters=parameters,
            model_id=source_spec.model_id,
            provider=source_spec.provider,
            model_version=source_spec.model_version,
            prompt_hash=source_spec.prompt_hash,
            sampling_config=sampling,
            tool_policy_hash=source_spec.tool_policy_hash,
            code_base_commit=code_base_commit,
            candidate_patch_ref=None,
            dataset_fingerprint=source_spec.dataset_fingerprint,
            evaluation_contract_hash=source_spec.evaluation_contract_hash,
            seed=seed,
            resource_budget=resources,
            execution=execution,
            created_at=created_at,
        )
