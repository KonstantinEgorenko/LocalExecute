"""Deterministic source provenance classification."""
from __future__ import annotations

from urllib.parse import urlsplit

from .models import SourceKind, SourceTier

_OFFICIAL_SUFFIXES = (
    "docs.python.org", "docs.github.com", "github.com", "developer.mozilla.org",
    "openai.com", "pytorch.org", "huggingface.co", "kubernetes.io", "docker.com",
)
_PAPER_HOSTS = {"arxiv.org", "aclanthology.org", "openreview.net", "doi.org"}
_COMMUNITY_HOSTS = {"reddit.com", "www.reddit.com", "news.ycombinator.com", "stackoverflow.com"}


def source_domain(url: str) -> str:
    return (urlsplit(url).hostname or "").casefold().removeprefix("www.")


def classify_source(url: str, kind: SourceKind) -> tuple[SourceKind, SourceTier]:
    domain = source_domain(url)
    if kind in {SourceKind.GITHUB_REPOSITORY, SourceKind.GITHUB_CODE}:
        return kind, SourceTier.PRIMARY
    if kind in {SourceKind.GITHUB_ISSUE, SourceKind.GITHUB_PULL_REQUEST}:
        return kind, SourceTier.COMMUNITY
    if domain in _PAPER_HOSTS or any(domain.endswith("." + host) for host in _PAPER_HOSTS):
        return SourceKind.RESEARCH_PAPER, SourceTier.PRIMARY
    if any(domain == host or domain.endswith("." + host) for host in _OFFICIAL_SUFFIXES):
        return SourceKind.OFFICIAL_DOC, SourceTier.PRIMARY
    if domain in _COMMUNITY_HOSTS:
        return kind, SourceTier.COMMUNITY
    return kind, SourceTier.SECONDARY if domain else SourceTier.UNKNOWN
