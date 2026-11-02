'research contracts composition root reusing frozen research architecture.'
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
from harness.research_reasoning import ResearchReasoningPolicy

from .policy import ResearchSemanticPolicy
from .runtime import ContractReliableOrnithResearchReasoner


def build_contract_reliable_research_service(
    root: Path,
    *,
    state_root: Path | None = None,
) -> AutonomousResearchService:
    root = Path(root)
    policy = ResearchWorkflowPolicy.from_project_root(root)
    transport = ResearchReasoningPolicy.from_project_root(root)
    semantic = ResearchSemanticPolicy.from_project_root(root)
    reasoner = ContractReliableOrnithResearchReasoner(
        root,
        policy,
        transport_policy=transport,
        semantic_policy=semantic,
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
