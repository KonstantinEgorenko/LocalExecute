'Resilient, target-scoped GitHub discovery for research integrity.\n\nFrozen external evidence keeps GitHub provider errors fatal during search.  research integrity treats\nGitHub code search as an optional discovery channel: exact target-bound primary\nartifacts are acquired separately, so unauthenticated code-search failures must\nnot abort the entire remediation run.\n'
from __future__ import annotations

import re
from typing import Sequence

from harness.external_evidence.models import ResearchChannel, ResearchQueryV1, SearchHitV1, SourceKind
from harness.external_evidence.providers import ResearchProviderError
from harness.runtime_hardening.github_guard import BoundedGitHubProvider

_REPO_QUALIFIER = re.compile(r"\brepo:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\b", re.IGNORECASE)


def _optional_code_search_unavailable(exc: ResearchProviderError) -> bool:
    message = str(exc)
    return any(marker in message for marker in ("HTTP 401", "HTTP 403", "status=401", "status=403"))


class IntegrityGitHubProvider:
    """Target-scope GitHub discovery and fail-soft optional code search.

    Repository identity and exact primary artifacts are handled independently by
    :class:`GitHubPrimaryResolver`.  This wrapper only controls broad discovery.
    """

    provider_id = "github-rest"

    def __init__(
        self,
        inner: BoundedGitHubProvider,
        *,
        target_repository: str = "",
        code_search_authenticated: bool = False,
    ) -> None:
        self.inner = inner
        self.target_repository = target_repository.strip()
        self.code_search_authenticated = bool(code_search_authenticated)
        self.code_search_auth_skips = 0

    def _scoped(self, query: ResearchQueryV1) -> ResearchQueryV1:
        if not self.target_repository:
            return query
        if query.channel is ResearchChannel.GITHUB_REPOSITORIES:
            return ResearchQueryV1(
                query.query_id,
                f"https://github.com/{self.target_repository}",
                query.channel,
            )
        if query.channel not in {ResearchChannel.GITHUB_CODE, ResearchChannel.GITHUB_ISSUES}:
            return query
        if _REPO_QUALIFIER.search(query.text):
            return query
        text = f"{query.text} repo:{self.target_repository}".strip()
        return ResearchQueryV1(query.query_id, text, query.channel)

    def search(self, query: ResearchQueryV1, *, limit: int) -> Sequence[SearchHitV1]:
        scoped = self._scoped(query)
        if query.channel is ResearchChannel.GITHUB_REPOSITORIES and self.target_repository:
            return (
                SearchHitV1(
                    self.provider_id,
                    query.query_id,
                    1,
                    self.target_repository,
                    f"https://github.com/{self.target_repository}",
                    "Exact target repository",
                    SourceKind.GITHUB_REPOSITORY,
                    {
                        "full_name": self.target_repository,
                        "phase20q_target_scoped": True,
                    },
                ),
            )
        if query.channel is ResearchChannel.GITHUB_CODE and not self.code_search_authenticated:
            self.code_search_auth_skips += 1
            return ()
        try:
            return self.inner.search(scoped, limit=limit)
        except ResearchProviderError as exc:
            if query.channel is ResearchChannel.GITHUB_CODE and _optional_code_search_unavailable(exc):
                self.code_search_auth_skips += 1
                return ()
            raise

    def fetch(self, hit: SearchHitV1):
        return self.inner.fetch(hit)

    def metrics(self) -> dict[str, int]:
        return {"code_search_auth_skips": self.code_search_auth_skips}

# Verified compatibility aliases for historical Python interfaces.
Phase20QGitHubProvider = IntegrityGitHubProvider
