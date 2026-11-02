'research diagnostics.2 additive dispatcher above the frozen production Swarm runtime.'
from __future__ import annotations
from harness.compatibility.configuration import legacy_keywords

import json
import os
from pathlib import Path
from typing import Any, Callable

from ..mission.models import MissionSpecV1
from .bootstrap import ResearchCampaignBootstrapV1
from .mission_models import (
    MissionDispatchResultV1,
    MissionModeV1,
    get_mission_mode,
    get_proposal_generator_identity,
    get_research_profile_id,
)
from .profile_registry import (
    ModelProposalFactoryRegistryV1,
    ResearchCampaignProfileRegistryV1,
)
from .serialization import stable_sha256


class MissionDispatchIdentityError(RuntimeError):
    pass


class _ResearchDispatchIdentityStoreV1:
    """Atomic immutable marker for the outer research request; not a lifecycle store."""

    def __init__(self, state_root: Path) -> None:
        self.state_root = Path(state_root).resolve(strict=False)

    def path_for(self, mission_id: str) -> Path:
        return self.state_root / "research-dispatch" / mission_id / "dispatch.json"

    def exists(self, mission_id: str) -> bool:
        return self.path_for(mission_id).exists()

    @staticmethod
    def _payload(spec: MissionSpecV1) -> dict[str, Any]:
        spec_payload = spec.to_dict()
        return {
            "schema_version": 1,
            "mission_mode": MissionModeV1.RESEARCH_CAMPAIGN.value,
            "mission_id": spec.mission_id,
            "mission_spec_fingerprint": stable_sha256(spec_payload),
            "mission_spec": spec_payload,
        }

    def ensure(self, spec: MissionSpecV1) -> None:
        expected = self._payload(spec)
        path = self.path_for(spec.mission_id)
        if path.exists():
            try:
                actual = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise MissionDispatchIdentityError(
                    f"cannot read research dispatch identity {path}: {exc}"
                ) from exc
            if actual != expected:
                raise MissionDispatchIdentityError(
                    "existing research dispatch specification does not match requested mission"
                )
            return

        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + f".tmp-{os.getpid()}")
        data = json.dumps(expected, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
        try:
            with temp.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
        finally:
            if temp.exists():
                temp.unlink(missing_ok=True)


class MissionDispatcherV1:
    'Route ordinary execution missions or empirical research campaigns.\n\n    The dispatcher sits above mission. RESEARCH_CAMPAIGN never creates an outer\n    mission mission; only individual experiments enter the existing mission ->\n    workflow production runtime through ResearchCampaignBootstrapV1.\n    '

    @legacy_keywords({'require_phase20v_environment': 'require_model_runtime_environment'})
    def __init__(
        self,
        *,
        swarm_root: Path,
        workspace: Path,
        state_root: Path,
        python: Path,
        execution_handler: Callable[[MissionSpecV1], Any],
        profile_registry: ResearchCampaignProfileRegistryV1,
        proposal_factory_registry: ModelProposalFactoryRegistryV1,
        bootstrap_builder: Callable[..., Any] = ResearchCampaignBootstrapV1.build_campaign,
        require_model_runtime_environment: bool = True,
        research_invocation_observer: Any | None = None,
    ) -> None:
        self.swarm_root = Path(swarm_root).resolve(strict=True)
        self.workspace = Path(workspace).resolve(strict=True)
        self.state_root = Path(state_root).resolve(strict=False)
        self.python = Path(python).resolve(strict=True)
        self.execution_handler = execution_handler
        self.profile_registry = profile_registry
        self.proposal_factory_registry = proposal_factory_registry
        self.bootstrap_builder = bootstrap_builder
        self.require_model_runtime_environment = require_model_runtime_environment
        self.research_invocation_observer = research_invocation_observer
        self.identity_store = _ResearchDispatchIdentityStoreV1(self.state_root)

    def _validate_workspace_binding(self, spec: MissionSpecV1) -> None:
        declared = spec.global_constraints.get("workspace")
        if declared is None:
            return
        if not isinstance(declared, str) or not declared.strip():
            raise ValueError("global_constraints.workspace must be non-empty string")
        current = Path(declared).resolve(strict=False)
        if current != self.workspace:
            raise ValueError(
                f"mission workspace {current} does not match dispatcher workspace {self.workspace}"
            )

    def _mission_checkpoint_path(self, spec: MissionSpecV1) -> Path:
        return self.state_root / "missions" / spec.mission_id / "mission.json"

    def dispatch(self, spec: MissionSpecV1) -> MissionDispatchResultV1:
        if not isinstance(spec, MissionSpecV1):
            raise TypeError("spec must be MissionSpecV1")
        self._validate_workspace_binding(spec)
        mode = get_mission_mode(spec)

        if mode is MissionModeV1.EXECUTION:
            if self.identity_store.exists(spec.mission_id):
                raise MissionDispatchIdentityError(
                    "state_root already belongs to a RESEARCH_CAMPAIGN mission"
                )
            if "research_profile_id" in spec.global_constraints:
                raise ValueError("EXECUTION mission cannot declare research_profile_id")
            if "proposal_generator_identity" in spec.global_constraints:
                raise ValueError("EXECUTION mission cannot declare proposal_generator_identity")
            result = self.execution_handler(spec)
            return MissionDispatchResultV1(mode=mode, execution_result=result)

        # An existing mission checkpoint under the same outer state root proves
        # this identity already belongs to an execution mission.
        if self._mission_checkpoint_path(spec).exists():
            raise MissionDispatchIdentityError(
                "existing Phase17 execution mission cannot be resumed as RESEARCH_CAMPAIGN"
            )

        if spec.global_constraints.get("research_required") is True:
            raise ValueError(
                "Phase18 research_required and Phase24 RESEARCH_CAMPAIGN are distinct; "
                "top-level RESEARCH_CAMPAIGN must not set research_required=true"
            )

        profile_id = get_research_profile_id(spec)
        profile = self.profile_registry.resolve(profile_id)
        generator_identity = get_proposal_generator_identity(spec)
        proposal_factory = None
        if generator_identity != "deterministic":
            proposal_factory = self.proposal_factory_registry.resolve(generator_identity)
            if self.research_invocation_observer is not None:
                try:
                    proposal_factory = self.research_invocation_observer.wrap_proposal_factory(
                        spec=spec,
                        generator_identity=generator_identity,
                        proposal_factory=proposal_factory,
                    )
                except Exception as exc:
                    if hasattr(self.research_invocation_observer, "last_error"):
                        self.research_invocation_observer.last_error = (
                            "research proposal observer factory decoration failed: "
                            f"{type(exc).__name__}: {exc}"
                        )

        # Persist exact outer research request before campaign bootstrap so an
        # early crash cannot later be resumed under a different mode/spec.
        self.identity_store.ensure(spec)

        bundle = self.bootstrap_builder(
            campaign_id=spec.mission_id,
            profile=profile,
            objective=spec.raw_objective,
            target=self.workspace,
            state_root=self.state_root / "research-campaign",
            swarm_root=self.swarm_root,
            python=self.python,
            proposal_generator_factory=proposal_factory,
            generator_identity=generator_identity,
            require_model_runtime_environment=self.require_model_runtime_environment,
        )
        try:
            run_result = bundle.runner.run(spec.mission_id)
        finally:
            if self.research_invocation_observer is not None:
                try:
                    self.research_invocation_observer.finalize(
                        spec=spec,
                        bundle=bundle,
                    )
                except Exception as exc:
                    if hasattr(self.research_invocation_observer, "last_error"):
                        self.research_invocation_observer.last_error = (
                            "research proposal observer finalization failed: "
                            f"{type(exc).__name__}: {exc}"
                        )
        summary = bundle.summarize(run_result)
        return MissionDispatchResultV1(
            mode=mode,
            research_run_result=run_result,
            research_summary=summary,
        )

# Verified compatibility aliases for historical Python interfaces.
MissionDispatcherV1._phase17_checkpoint_path = MissionDispatcherV1._mission_checkpoint_path
