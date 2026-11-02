'Default external evidence autonomous-research composition root.'
from __future__ import annotations

import os
from pathlib import Path
from typing import Sequence

from .controller import ResearchController
from .models import ResearchWorkflowPolicy, ResearchChannel
from .mission_bridge import MissionResearchBridge, ResearchMissionObservationV1
from .providers import GitHubRestProvider, ResearchProviderError, SearxNgProvider
from .reasoning import OrnithResearchReasoner
from .store import JsonResearchStore


class AutonomousResearchService:
    """Question -> Ornith query plan -> read-only research -> evidence-backed observation."""

    def __init__(
        self,
        *,
        reasoner: OrnithResearchReasoner,
        bridge: MissionResearchBridge,
    ) -> None:
        self.reasoner = reasoner
        self.bridge = bridge

    def research_question(
        self,
        *,
        research_id: str,
        question: str,
        required_topics: Sequence[str],
        root_task_sha256: str = "",
    ) -> ResearchMissionObservationV1:
        request = self.reasoner.create_request(
            research_id=research_id,
            question=question,
            required_topics=required_topics,
            root_task_sha256=root_task_sha256,
        )
        return self.bridge.research(request)


def build_default_research_service(
    root: Path,
    *,
    state_root: Path | None = None,
) -> AutonomousResearchService:
    root = Path(root)
    policy = ResearchWorkflowPolicy.from_project_root(root)
    reasoner = OrnithResearchReasoner(root, policy)
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
    return AutonomousResearchService(reasoner=reasoner, bridge=MissionResearchBridge(controller, store))
