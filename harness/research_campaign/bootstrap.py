'research diagnostics.1 Reusable Generic Research Campaign Bootstrap and Composition Root.'
from __future__ import annotations
from harness.compatibility.configuration import legacy_keywords

import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .autonomous_cycle import DurableAutonomousStrategyPlannerV1
from .command_metric_evaluation import CommandMetricEvaluationPortV1
from .contracts import (
    EvaluationContractV1,
    MetricDirectionV1,
    ResearchBudgetV1,
    ResearchContractV1,
)
from .controller import ResearchCampaignControllerV1
from .decisions import SearchDecisionActionV1, SearchDecisionV1
from .errors import ResearchStoreNotFoundError
from .evidence import ResearchEvidencePacketV1
from .evidence_acquisition import CampaignResearchEvidenceAcquirerV1
from .protected_evidence import ProtectedEvidenceAcquirerV1
from .execution import ProductionSwarmExperimentExecutionPortV1
from .exact_parameter_execution import (
    DeterministicExactParameterExperimentExecutionPortV1,
    exact_parameter_policy_from_profile,
)
from .model_proposals import ModelBackedResearchProposalGeneratorV1
from .models import (
    ExperimentFailureKindV1,
    ExperimentSpecV1,
    HypothesisModeV1,
    HypothesisStatusV1,
    HypothesisV1,
)
from .ports import (
    ExperimentEvaluationStatusV1,
    ExperimentExecutionOutcomeV1,
    ExperimentExecutionPort,
    ExperimentExecutionStatusV1,
)
from .profile import (
    ResearchBudgetPolicyV1,
    ResearchCampaignProfileV1,
    compute_dataset_fingerprint,
    compute_scorer_fingerprint,
    compute_split_fingerprint,
)
from .runner import ResearchCampaignRunPolicyV1, ResearchCampaignRunnerV1
from .serialization import freeze_json, plain, safe_id, stable_sha256, text
from .stop_policy import ResearchStopPolicyV1
from .store import ResearchExperimentStore
from .strategy_cycle import DeterministicResearchStrategyPlannerV1
from .strategy_proposals import (
    DeterministicResearchProposalGeneratorV1,
    ResearchProposalGeneratorPort,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _git_cmd(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed in {cwd} (rc={proc.returncode}): {proc.stderr.strip()}"
        )
    return proc.stdout


class _BaselineThenProductionExecutionPort:
    """Execution wrapper that intercepts baseline experiment and delegates candidates to production Swarm."""

    def __init__(
        self,
        *,
        original_baseline_commit: str,
        delegate: ExperimentExecutionPort,
        baseline_experiment_id: str,
    ) -> None:
        self.original_baseline_commit = original_baseline_commit
        self.delegate = delegate
        self.baseline_experiment_id = baseline_experiment_id
        self.attempted_calls: list[str] = []
        self.physical_calls: list[str] = []

    def execute(
        self,
        spec: ExperimentSpecV1,
        *,
        resume: bool = False,
    ) -> ExperimentExecutionOutcomeV1:
        if spec.experiment_id == self.baseline_experiment_id:
            now = _utc_now()
            return ExperimentExecutionOutcomeV1(
                status=ExperimentExecutionStatusV1.COMPLETED,
                started_at=now,
                ended_at=now,
                verification_outcome="PASSED",
                candidate_commit=self.original_baseline_commit,
                telemetry={"execution_backend": "BASELINE_NOOP"},
            )
        self.attempted_calls.append(spec.experiment_id)
        outcome = self.delegate.execute(spec, resume=resume)
        if outcome.telemetry.get("execution_backend") in {
            "SWARM_PHASE17_PHASE16",
            "DETERMINISTIC_EXACT_PARAMETER",
        }:
            self.physical_calls.append(spec.experiment_id)
        return outcome


class _GenericObservationPolicy:
    """Standard search observation policy that initializes with baseline and adopts champion on improvement."""

    def __init__(
        self,
        baseline: ExperimentSpecV1,
        *,
        metric_direction: MetricDirectionV1 = MetricDirectionV1.MAXIMIZE,
        baseline_experiment_id: str,
    ) -> None:
        self.baseline = baseline
        self.metric_direction = metric_direction
        self.baseline_experiment_id = baseline_experiment_id
        self.propose_calls = 0
        self.observe_calls = 0

    def propose(self, context):
        self.propose_calls += 1
        if context.champion is not None:
            raise RuntimeError(
                "legacy SearchPolicy.propose must be bypassed after champion exists"
            )
        return self.baseline

    def observe(self, context, observation):
        self.observe_calls += 1
        evaluation = observation.evaluation
        incumbent = context.champion
        incumbent_metric = (
            None
            if incumbent is None or incumbent.result is None
            else incumbent.result.primary_metric
        )
        scored = (
            evaluation is not None
            and evaluation.status is ExperimentEvaluationStatusV1.SCORED
            and evaluation.primary_metric is not None
        )
        if scored:
            if incumbent_metric is None:
                improved = True
            elif self.metric_direction == MetricDirectionV1.MAXIMIZE:
                improved = evaluation.primary_metric > incumbent_metric
            else:
                improved = evaluation.primary_metric < incumbent_metric
        else:
            improved = False

        action = (
            SearchDecisionActionV1.UPDATE_CHAMPION
            if improved
            else SearchDecisionActionV1.DROP
        )
        incumbent_id = None if incumbent is None else incumbent.spec.experiment_id

        now = _utc_now()
        return SearchDecisionV1(
            decision_id=f"decision-{observation.spec.experiment_id}-{self.observe_calls}",
            campaign_id=observation.spec.campaign_id,
            experiment_id=observation.spec.experiment_id,
            action=action,
            reason=(
                "valid evidence improved the incumbent"
                if improved
                else "candidate did not improve valid evidence"
            ),
            created_at=now,
            incumbent_experiment_id=incumbent_id if improved else None,
            evidence_experiment_ids=()
            if incumbent_id is None
            else (incumbent_id,),
            metadata={
                "metric_direction": self.metric_direction.value,
            },
        )


@dataclass(slots=True)
class ResearchCampaignBundleV1:
    """Fully composed generic research campaign bundle."""

    campaign_id: str
    profile: ResearchCampaignProfileV1
    contract: ResearchContractV1
    evaluation_contract: EvaluationContractV1
    baseline_spec: ExperimentSpecV1
    store: ResearchExperimentStore
    controller: ResearchCampaignControllerV1
    runner: ResearchCampaignRunnerV1
    evaluator: CommandMetricEvaluationPortV1
    execution: ExperimentExecutionPort
    original_baseline_commit: str
    generator_mode: str

    def summarize(
        self,
        run_result: Any,
    ) -> dict[str, Any]:
        ordered_records: dict[str, dict[str, Any]] = {}
        for stored in self.store.list_experiments(self.campaign_id, terminal_only=True):
            if stored.result is not None and stored.result.primary_metric is not None:
                commit = stored.result.candidate_commit or self.original_baseline_commit
                changed: list[str] = []
                if commit != self.original_baseline_commit:
                    raw_diff = _git_cmd(
                        self.evaluator.target,
                        "diff",
                        "--name-only",
                        f"{self.original_baseline_commit}..{commit}",
                    )
                    changed = [
                        line.strip().replace("\\", "/")
                        for line in raw_diff.splitlines()
                        if line.strip()
                    ]
                ordered_records[stored.spec.experiment_id] = {
                    "experiment_id": stored.spec.experiment_id,
                    "candidate_commit": commit,
                    "metric": float(stored.result.primary_metric),
                    "parameters": dict(plain(stored.spec.parameters)),
                    "changed_paths_from_original_baseline": changed,
                    "split": self.profile.evaluation_config.research_split,
                }

        for record in self.evaluator.records:
            exp_id = record["experiment_id"]
            if exp_id not in ordered_records:
                ordered_records[exp_id] = dict(record)

        records = list(ordered_records.values())
        metrics = [float(item["metric"]) for item in records]
        checkpoints = self.store.list_strategy_checkpoints(self.campaign_id, limit=100_000)
        evidence_packets = self.store.list_evidence_packets(self.campaign_id)
        cohorts = self.store.list_proposal_cohorts(self.campaign_id)
        stop_decisions = self.store.list_stop_decisions(self.campaign_id)

        self.store.verify_integrity()

        baseline_record = next(
            (item for item in records if item["experiment_id"] == self.baseline_spec.experiment_id),
            None,
        )
        baseline_metric = float(baseline_record["metric"]) if baseline_record is not None else None
        best_metric = None
        if metrics:
            best_metric = (
                max(metrics)
                if self.profile.metric_direction == MetricDirectionV1.MAXIMIZE
                else min(metrics)
            )

        real_improvement = False
        if baseline_metric is not None and best_metric is not None:
            if self.profile.metric_direction == MetricDirectionV1.MAXIMIZE:
                real_improvement = best_metric > baseline_metric
            else:
                real_improvement = best_metric < baseline_metric

        model_success_count = sum(
            1 for item in cohorts if item.model_called and not item.fallback_used
        )
        fallback_count = sum(1 for item in cohorts if item.fallback_used)

        durable_physical_calls: list[str] = []
        durable_swarm_calls: list[str] = []
        durable_exact_parameter_calls: list[str] = []
        for stored in self.store.list_experiments(self.campaign_id):
            if stored.spec.experiment_id == self.baseline_spec.experiment_id:
                continue
            if stored.result is None:
                continue
            backend = stored.result.telemetry.get("execution_backend")
            if backend == "SWARM_PHASE17_PHASE16":
                durable_swarm_calls.append(stored.spec.experiment_id)
                durable_physical_calls.append(stored.spec.experiment_id)
            elif backend == "DETERMINISTIC_EXACT_PARAMETER":
                durable_exact_parameter_calls.append(stored.spec.experiment_id)
                durable_physical_calls.append(stored.spec.experiment_id)

        merged_physical_calls = list(durable_physical_calls)
        durable_physical_set = set(durable_physical_calls)
        phys_calls_this_process: list[str] = []
        if hasattr(self.execution, "physical_calls"):
            phys_calls_this_process = list(self.execution.physical_calls)
            for call in self.execution.physical_calls:
                if call not in durable_physical_set:
                    durable_physical_set.add(call)
                    merged_physical_calls.append(call)

        execution_delegate = getattr(self.execution, "delegate", None)
        exact_parameter_calls_this_process = list(
            getattr(execution_delegate, "deterministic_calls", ())
        )
        delegated_swarm_calls_this_process = list(
            getattr(execution_delegate, "delegated_calls", ())
        )

        splits_called_this_process = list(self.evaluator.splits_called)
        splits_called = [self.profile.evaluation_config.research_split] if records else []

        return {
            "schema_version": 1,
            "campaign_id": self.campaign_id,
            "profile_id": self.profile.profile_id,
            "profile_fingerprint": self.profile.profile_fingerprint,
            "generator_mode": self.generator_mode,
            "original_baseline_commit": self.original_baseline_commit,
            "final_state": run_result.final_state.status.value,
            "tick_count": len(run_result.ticks),
            "exhausted_tick_budget": run_result.exhausted_tick_budget,
            "strategy_checkpoint_count": len(checkpoints),
            "strategy_decision_fingerprints": [
                item.decision_fingerprint for item in checkpoints
            ],
            "physical_experiment_count": len(merged_physical_calls),
            "physical_experiment_calls": merged_physical_calls,
            "physical_experiment_count_this_process": len(phys_calls_this_process),
            "physical_experiment_calls_this_process": phys_calls_this_process,
            "swarm_execution_count": len(durable_swarm_calls),
            "swarm_execution_calls": durable_swarm_calls,
            "deterministic_exact_parameter_count": len(durable_exact_parameter_calls),
            "deterministic_exact_parameter_calls": durable_exact_parameter_calls,
            "deterministic_exact_parameter_count_this_process": len(
                exact_parameter_calls_this_process
            ),
            "deterministic_exact_parameter_calls_this_process": (
                exact_parameter_calls_this_process
            ),
            "delegated_swarm_count_this_process": len(
                delegated_swarm_calls_this_process
            ),
            "delegated_swarm_calls_this_process": delegated_swarm_calls_this_process,
            "evidence_packet_count": len(evidence_packets),
            "proposal_cohort_count": len(cohorts),
            "model_success_cohort_count": model_success_count,
            "fallback_cohort_count": fallback_count,
            "stop_reasons": [item.reason.value for item in stop_decisions],
            "evaluation_splits_called": splits_called,
            "evaluation_splits_called_this_process": splits_called_this_process,
            "research_evaluation_records_this_process": list(self.evaluator.records),
            "research_evaluation_count_this_process": len(self.evaluator.records),
            "research_records": records,
            "baseline_metric": baseline_metric,
            "best_metric": best_metric,
            "real_metric_improvement": real_improvement,
        }


class ResearchCampaignBootstrapV1:
    """Reusable generic composition root for autonomous research campaigns."""

    @staticmethod
    @legacy_keywords({'require_phase20v_environment': 'require_model_runtime_environment'})
    def build_campaign(
        *,
        campaign_id: str,
        profile: ResearchCampaignProfileV1,
        objective: str,
        target: Path,
        state_root: Path,
        swarm_root: Path,
        python: Path,
        proposal_generator_factory: (
            Callable[[ResearchEvidencePacketV1], ModelBackedResearchProposalGeneratorV1] | None
        ) = None,
        generator_identity: str = "phase24-generic-model-proposal",
        require_model_runtime_environment: bool = True,
        execution_port_override: ExperimentExecutionPort | None = None,
        custom_budget_policy: ResearchBudgetPolicyV1 | None = None,
    ) -> ResearchCampaignBundleV1:
        safe_id(campaign_id, "campaign_id")
        text(objective, "objective", max_length=2_000_000)
        target = Path(target).resolve(strict=True)
        state_root = Path(state_root).resolve(strict=False)
        state_root.mkdir(parents=True, exist_ok=True)
        swarm_root = Path(swarm_root).resolve(strict=False)
        python = Path(python).resolve(strict=True)

        if proposal_generator_factory is None:
            resolved_generator_identity = "deterministic"
        else:
            safe_id(generator_identity, "generator_identity")
            resolved_generator_identity = generator_identity

        # 1. Resolve clean target HEAD
        status_out = _git_cmd(target, "status", "--porcelain=v1")
        if status_out.strip():
            raise RuntimeError(f"Target repository {target} must be clean before campaign build")
        original_baseline_commit = _git_cmd(target, "rev-parse", "HEAD").strip().lower()

        # 2. Compute fingerprints
        dataset_fp = compute_dataset_fingerprint(target, profile.dataset_refs)
        split_fp = compute_split_fingerprint(target, profile.research_split_ref)
        scorer_fp = compute_scorer_fingerprint(target, profile.scorer_refs)

        eval_integrity_rules = {
            "research_split": profile.evaluation_config.research_split,
            "allowed_mutation_scope": list(profile.allowed_mutation_scope),
            "forbidden_mutation_scope": list(profile.forbidden_mutation_scope),
            "protected_no_feedback_paths": list(profile.protected_no_feedback_paths),
            "protected_feedback": False,
        }
        eval_contract = EvaluationContractV1(
            metric_name=profile.metric_name,
            metric_version=profile.metric_version,
            direction=profile.metric_direction,
            dataset_fingerprint=dataset_fp,
            split_fingerprint=split_fp,
            scorer_fingerprint=scorer_fp,
            aggregation="mean",
            seed_policy={"mode": "fixed", "seed": 0},
            repetitions=1,
            integrity_rules=eval_integrity_rules,
        )
        eval_contract_hash = eval_contract.contract_hash

        store = ResearchExperimentStore(state_root / "research.sqlite")
        budget_policy = custom_budget_policy or profile.budget_policy

        # Campaign-scoped bounded IDs
        camp_hash = stable_sha256(campaign_id)[:16]
        baseline_exp_id = f"baseline-{camp_hash}"
        baseline_hyp_id = f"hypothesis-baseline-{camp_hash}"

        # 3. Research Contract Materialization & Strict Immutable Resume Validation
        try:
            persisted_contract = store.current_research_contract(campaign_id)
        except ResearchStoreNotFoundError:
            now_iso = _utc_now()
            budget = budget_policy.materialize_budget(now_iso)
            constraints = {
                "original_baseline_commit": original_baseline_commit,
                "dataset_fingerprint": dataset_fp,
                "split_fingerprint": split_fp,
                "scorer_fingerprint": scorer_fp,
                "evaluation_contract_hash": eval_contract_hash,
                "research_profile_id": profile.profile_id,
                "research_profile_fingerprint": profile.profile_fingerprint,
                "proposal_generator_identity": resolved_generator_identity,
                "research_required": False,
                "protected_feedback": False,
                "allowed_mutation_scope": list(profile.allowed_mutation_scope),
                "forbidden_mutation_scope": list(profile.forbidden_mutation_scope),
                "protected_no_feedback_paths": list(profile.protected_no_feedback_paths),
                "execution_goal_capsule": profile.execution_goal_capsule,
                "execution_global_constraints": plain(profile.execution_global_constraints),
            }
            contract = ResearchContractV1(
                campaign_id=campaign_id,
                objective=objective,
                primary_metric=profile.metric_name,
                metric_direction=profile.metric_direction,
                budget=budget,
                created_at=now_iso,
                constraints=constraints,
                dataset_refs=profile.dataset_refs,
                data_split_policy=profile.data_split_policy,
                allowed_mutation_scope=profile.allowed_mutation_scope,
                forbidden_mutation_scope=profile.forbidden_mutation_scope,
            )
            store.create_campaign(contract)
        else:
            # Strict validation of all immutable envelope keys BEFORE any subordinate write
            if persisted_contract.campaign_id != campaign_id:
                raise RuntimeError(
                    f"persisted campaign_id {persisted_contract.campaign_id} != {campaign_id}"
                )
            if persisted_contract.objective != objective:
                raise RuntimeError(
                    f"persisted objective {persisted_contract.objective!r} != {objective!r}"
                )
            if persisted_contract.primary_metric != profile.metric_name:
                raise RuntimeError(
                    f"persisted primary_metric {persisted_contract.primary_metric} != {profile.metric_name}"
                )
            if persisted_contract.metric_direction != profile.metric_direction:
                raise RuntimeError(
                    f"persisted metric_direction {persisted_contract.metric_direction} != {profile.metric_direction}"
                )
            if persisted_contract.dataset_refs != profile.dataset_refs:
                raise RuntimeError(
                    f"persisted dataset_refs {persisted_contract.dataset_refs} != {profile.dataset_refs}"
                )
            if persisted_contract.allowed_mutation_scope != profile.allowed_mutation_scope:
                raise RuntimeError(
                    f"persisted allowed_mutation_scope {persisted_contract.allowed_mutation_scope} != {profile.allowed_mutation_scope}"
                )
            if persisted_contract.forbidden_mutation_scope != profile.forbidden_mutation_scope:
                raise RuntimeError(
                    f"persisted forbidden_mutation_scope {persisted_contract.forbidden_mutation_scope} != {profile.forbidden_mutation_scope}"
                )

            persisted_baseline_commit = persisted_contract.constraints.get("original_baseline_commit")
            if persisted_baseline_commit != original_baseline_commit:
                raise RuntimeError(
                    f"current target HEAD {original_baseline_commit} does not match persisted campaign baseline commit {persisted_baseline_commit}"
                )

            persisted_dataset_fp = persisted_contract.constraints.get("dataset_fingerprint")
            if persisted_dataset_fp != dataset_fp:
                raise RuntimeError(
                    f"current dataset fingerprint {dataset_fp} does not match persisted campaign dataset fingerprint {persisted_dataset_fp}"
                )

            persisted_split_fp = persisted_contract.constraints.get("split_fingerprint")
            if persisted_split_fp != split_fp:
                raise RuntimeError(
                    f"current split fingerprint {split_fp} does not match persisted campaign split fingerprint {persisted_split_fp}"
                )

            persisted_scorer_fp = persisted_contract.constraints.get("scorer_fingerprint")
            if persisted_scorer_fp != scorer_fp:
                raise RuntimeError(
                    f"current scorer fingerprint {scorer_fp} does not match persisted campaign scorer fingerprint {persisted_scorer_fp}"
                )

            persisted_eval_hash = persisted_contract.constraints.get("evaluation_contract_hash")
            if persisted_eval_hash != eval_contract_hash:
                raise RuntimeError(
                    f"current evaluation contract hash {eval_contract_hash} does not match persisted campaign evaluation contract hash {persisted_eval_hash}"
                )

            persisted_profile_id = persisted_contract.constraints.get("research_profile_id")
            if persisted_profile_id != profile.profile_id:
                raise RuntimeError(
                    f"persisted research_profile_id {persisted_profile_id!r} != {profile.profile_id!r}"
                )

            persisted_profile_fp = persisted_contract.constraints.get("research_profile_fingerprint")
            if persisted_profile_fp != profile.profile_fingerprint:
                raise RuntimeError(
                    f"persisted profile_fingerprint {persisted_profile_fp} != {profile.profile_fingerprint}"
                )

            persisted_gen_id = persisted_contract.constraints.get("proposal_generator_identity")
            if persisted_gen_id != resolved_generator_identity:
                raise RuntimeError(
                    f"current proposal_generator_identity {resolved_generator_identity!r} does not match persisted {persisted_gen_id!r}"
                )

            budget_policy.validate_compatible(persisted_contract.budget)
            contract = persisted_contract

        # 4. Save evaluation contract if not present
        try:
            store.load_evaluation_contract(eval_contract_hash)
        except ResearchStoreNotFoundError:
            store.save_evaluation_contract(eval_contract)

        # 5. Save baseline hypothesis if not present
        try:
            store.load_hypothesis(baseline_hyp_id)
        except ResearchStoreNotFoundError:
            now_iso = _utc_now()
            split_name = profile.evaluation_config.research_split
            baseline_hypothesis = HypothesisV1(
                hypothesis_id=baseline_hyp_id,
                campaign_id=campaign_id,
                statement=f"Baseline target hypothesis on research split '{split_name}'.",
                mechanism=f"Evaluate untouched repository on research split '{split_name}'.",
                predicted_effect="Establish initial reference metric.",
                created_by="phase24.1-generic-bootstrap",
                created_at=now_iso,
                mode=HypothesisModeV1.RESEARCH_QUALITY,
                falsifier=f"Baseline target cannot be evaluated deterministically on research split '{split_name}'.",
                status=HypothesisStatusV1.ACTIVE,
            )
            store.save_hypothesis(baseline_hypothesis)

        # 6. Baseline Experiment Spec
        try:
            persisted_baseline = store.load_experiment_spec(baseline_exp_id)
        except ResearchStoreNotFoundError:
            now_iso = _utc_now()
            baseline_spec = ExperimentSpecV1(
                experiment_id=baseline_exp_id,
                campaign_id=campaign_id,
                hypothesis_id=baseline_hyp_id,
                strategy_id="strategy-baseline",
                code_base_commit=original_baseline_commit,
                dataset_fingerprint=dataset_fp,
                evaluation_contract_hash=eval_contract_hash,
                seed=0,
                created_at=now_iso,
                parameters=profile.baseline_parameters,
                execution={
                    # R8.7 materializes child experiments by copying the champion
                    # execution mapping.  Therefore the baseline must carry the
                    # real mutation objective even though baseline execution itself
                    # is intercepted as a no-op by _BaselineThenProductionExecutionPort.
                    "objective": (
                        profile.execution_goal_capsule
                        or "Implement the selected bounded research experiment within the allowed mutation scope; "
                           "treat MissionSpec.global_constraints['research_experiment_parameters'] as authoritative "
                           "for parameter mutations."
                    ),
                    "goal_capsule": (
                        profile.execution_goal_capsule
                        or "Implement the selected bounded research experiment within the allowed mutation scope."
                    ),
                    "mode": "CUMULATIVE",
                    "global_constraints": {
                        "research_required": False,
                        "allowed_edit_files": list(profile.allowed_mutation_scope),
                        "forbidden_mutation_paths": list(profile.forbidden_mutation_scope),
                        "protected_no_feedback_paths": list(profile.protected_no_feedback_paths),
                        **plain(profile.execution_global_constraints),
                    },
                },
            )
        else:
            if persisted_baseline.campaign_id != campaign_id:
                raise RuntimeError(
                    f"persisted baseline campaign_id {persisted_baseline.campaign_id} != {campaign_id}"
                )
            if persisted_baseline.hypothesis_id != baseline_hyp_id:
                raise RuntimeError(
                    f"persisted baseline hypothesis_id {persisted_baseline.hypothesis_id} != {baseline_hyp_id}"
                )
            if persisted_baseline.code_base_commit != original_baseline_commit:
                raise RuntimeError(
                    f"target HEAD {original_baseline_commit} does not match persisted baseline code_base_commit {persisted_baseline.code_base_commit}"
                )
            if persisted_baseline.dataset_fingerprint != dataset_fp:
                raise RuntimeError(
                    f"dataset fingerprint {dataset_fp} does not match persisted baseline {persisted_baseline.dataset_fingerprint}"
                )
            if persisted_baseline.evaluation_contract_hash != eval_contract_hash:
                raise RuntimeError(
                    f"evaluation contract hash {eval_contract_hash} does not match persisted baseline {persisted_baseline.evaluation_contract_hash}"
                )
            if dict(plain(persisted_baseline.parameters)) != dict(plain(profile.baseline_parameters)):
                raise RuntimeError(
                    f"baseline parameters {persisted_baseline.parameters} do not match profile baseline_parameters {profile.baseline_parameters}"
                )
            baseline_spec = persisted_baseline

        # 7. Wire Evaluator
        evaluator = CommandMetricEvaluationPortV1(
            target=target,
            scratch_root=state_root / "eval_scratch",
            python=python,
            config=profile.evaluation_config,
            allowed_mutation_scope=profile.allowed_mutation_scope,
            original_baseline_commit=original_baseline_commit,
            evaluation_contract=eval_contract,
        )

        # 8. Wire Execution Port
        if execution_port_override is not None:
            exec_port = _BaselineThenProductionExecutionPort(
                original_baseline_commit=original_baseline_commit,
                delegate=execution_port_override,
                baseline_experiment_id=baseline_exp_id,
            )
        else:
            raw_exec: ExperimentExecutionPort = ProductionSwarmExperimentExecutionPortV1(
                swarm_root=swarm_root,
                workspace=target,
                state_root=state_root / "swarm_exec",
                require_model_runtime_environment=require_model_runtime_environment,
            )
            exact_parameter_policy = exact_parameter_policy_from_profile(profile)
            if exact_parameter_policy is not None:
                raw_exec = DeterministicExactParameterExperimentExecutionPortV1(
                    delegate=raw_exec,
                    workspace=target,
                    state_root=state_root / "exact_parameter_exec",
                    baseline_spec=baseline_spec,
                    policy=exact_parameter_policy,
                    clock=_utc_now,
                )
            exec_port = _BaselineThenProductionExecutionPort(
                original_baseline_commit=original_baseline_commit,
                delegate=raw_exec,
                baseline_experiment_id=baseline_exp_id,
            )

        # 9. Wire Strategy Planner & Evidence Acquirer
        base_planner = DeterministicResearchStrategyPlannerV1(
            strategy_space=profile.strategy_space,
            mutation_scope_by_dimension={
                item.name: profile.allowed_mutation_scope for item in profile.strategy_space.dimensions
            },
        )

        if proposal_generator_factory is not None:
            def _validated_model_generator_factory(packet: ResearchEvidencePacketV1) -> ModelBackedResearchProposalGeneratorV1:
                gen = proposal_generator_factory(packet)
                if not isinstance(gen, ModelBackedResearchProposalGeneratorV1):
                    raise TypeError(
                        f"proposal_generator_factory must return ModelBackedResearchProposalGeneratorV1, got {type(gen).__name__}"
                    )
                return gen

            base_evidence_acquirer = CampaignResearchEvidenceAcquirerV1(
                repository_root=target,
                clock=_utc_now,
                external_research=None,
                external_required=False,
            )
            evidence_acquirer = ProtectedEvidenceAcquirerV1(
                delegate=base_evidence_acquirer,
                protected_paths=profile.protected_no_feedback_paths,
            )
            planner = DurableAutonomousStrategyPlannerV1(
                base_planner=base_planner,
                store=store,
                evidence_acquirer=evidence_acquirer,
                model_generator_factory=_validated_model_generator_factory,
                clock=_utc_now,
                generator_identity=resolved_generator_identity,
            )
        else:
            planner = base_planner

        # 10. Wire Search Policy & Stop Policy
        search_policy = _GenericObservationPolicy(
            baseline=baseline_spec,
            metric_direction=profile.metric_direction,
            baseline_experiment_id=baseline_exp_id,
        )
        stop_policy = ResearchStopPolicyV1(
            plateau_valid_experiments=budget_policy.max_no_improvement_window,
            plateau_epsilon=0.0,
            technical_failure_limit=budget_policy.max_consecutive_crashes,
        )

        # 11. Wire Controller & Runner
        controller = ResearchCampaignControllerV1(
            store=store,
            search_policy=search_policy,
            execution_port=exec_port,
            evaluation_port=evaluator,
            clock=_utc_now,
            strategy_planner=planner,
            stop_policy=stop_policy,
        )
        runner = ResearchCampaignRunnerV1(
            controller,
            ResearchCampaignRunPolicyV1(
                max_ticks=contract.budget.max_experiments + 8,
            ),
        )

        return ResearchCampaignBundleV1(
            campaign_id=campaign_id,
            profile=profile,
            contract=contract,
            evaluation_contract=eval_contract,
            baseline_spec=baseline_spec,
            store=store,
            controller=controller,
            runner=runner,
            evaluator=evaluator,
            execution=exec_port,
            original_baseline_commit=original_baseline_commit,
            generator_mode=resolved_generator_identity,
        )
