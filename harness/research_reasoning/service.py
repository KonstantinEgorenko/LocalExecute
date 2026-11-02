'research reasoning composition root reusing all frozen external evidence research components.'
from __future__ import annotations

import os
from pathlib import Path

from harness.external_evidence import (
    AutonomousResearchService,
    GitHubRestProvider,
    JsonResearchStore,
    MissionResearchBridge,
    ResearchWorkflowPolicy,
    ResearchChannel,
    ResearchController,
    ResearchProviderError,
    SearxNgProvider,
)

from .policy import ResearchReasoningPolicy
from .runtime import ResilientOrnithResearchReasoner


def build_resilient_research_service(
    root: Path,
    *,
    state_root: Path | None = None,
) -> AutonomousResearchService:
    root = Path(root)
    policy = ResearchWorkflowPolicy.from_project_root(root)
    reliability = ResearchReasoningPolicy.from_project_root(root)
    reasoner = ResilientOrnithResearchReasoner(
        root,
        policy,
        reliability_policy=reliability,
    )
    store = JsonResearchStore(state_root or (root / "state/research"))

    discovery = {}
    fetchers = {}

    searx_url = os.getenv(policy.searxng_base_url_env, "").strip()
    if policy.searxng_enabled and searx_url:
        searx = SearxNgProvider(policy, base_url=searx_url)
        discovery[ResearchChannel.WEB.value] = searx
        fetchers[searx.provider_id] = searx

    if policy.github_enabled:
        github = GitHubRestProvider(policy)
        discovery[ResearchChannel.GITHUB_REPOSITORIES.value] = github
        discovery[ResearchChannel.GITHUB_CODE.value] = github
        discovery[ResearchChannel.GITHUB_ISSUES.value] = github
        fetchers[github.provider_id] = github

    if not discovery:
        raise ResearchProviderError(
            f"no Phase 18 providers configured; set {policy.searxng_base_url_env} or enable GitHub"
        )

    controller = ResearchController(
        policy=policy,
        discovery=discovery,
        fetchers=fetchers,
        synthesis=reasoner,
        store=store,
    )
    return AutonomousResearchService(
        reasoner=reasoner,
        bridge=MissionResearchBridge(controller, store),
    )
