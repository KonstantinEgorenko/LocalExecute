'runtime hardening channel-aware guard for frozen GitHub discovery.'
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

from harness.external_evidence.models import ResearchChannel, ResearchQueryV1, SearchHitV1
from harness.external_evidence.providers import GitHubRestProvider, ResearchProviderError

_MAX_GITHUB_QUERY_CHARS = 256

_GITHUB_REF = re.compile(
    r"(?P<prefix>github:|https?://github\.com/)"
    r"(?P<owner>[A-Za-z0-9_.-]+)/"
    r"(?P<repo>[A-Za-z0-9_.-]+)"
    r"(?P<suffix>/[^\s,;)\]}]+)?",
    re.IGNORECASE,
)

_REPO_QUALIFIER = re.compile(
    r"\brepo:(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)\b",
    re.IGNORECASE,
)

_STOPWORDS = {
    "about", "already", "and", "architecture", "claim", "collect", "content",
    "corroborating", "correct", "distinct", "document", "documents", "evidence",
    "exact", "existing", "explicitly", "fetch", "file", "files", "find", "for",
    "from", "github", "goal", "if", "in", "instructions", "license", "material",
    "not", "of", "or", "original", "path", "prefer", "present", "primary",
    "query", "raw", "record", "refuting", "repository", "research", "root",
    "search", "statement", "text", "that", "the", "this", "to", "topic",
    "truthful", "uncertainty", "under", "verify", "whether", "with", "without",
}


@dataclass(frozen=True, slots=True)
class GitHubQueryNormalization:
    original: ResearchQueryV1
    normalized: ResearchQueryV1
    repository: str
    path: str


def _extract_repo(text: str) -> tuple[str, str, str]:
    match = _GITHUB_REF.search(text)
    if match:
        owner = match.group("owner")
        repo = match.group("repo").removesuffix(".git")
        suffix = (match.group("suffix") or "").strip("/")
        path = suffix

        # Normalize normal GitHub web URLs:
        # https://github.com/owner/repo/blob/main/path/to/file
        parts = path.split("/") if path else []
        if len(parts) >= 3 and parts[0].casefold() in {"blob", "tree"}:
            path = "/".join(parts[2:])

        return owner, repo, path

    qualifier = _REPO_QUALIFIER.search(text)
    if qualifier:
        return (
            qualifier.group("owner"),
            qualifier.group("repo").removesuffix(".git"),
            "",
        )

    return "", "", ""


def _keywords(text: str, *, owner: str = "", repo: str = "") -> list[str]:
    result = []
    seen = set()
    excluded = {owner.casefold(), repo.casefold()}
    for token in re.findall(r"[A-Za-z0-9_.-]{2,}", text):
        cleaned = token.strip(".,:;()[]{}")
        folded = cleaned.casefold().strip(".-_")
        if not folded or folded in _STOPWORDS or folded in excluded:
            continue
        if cleaned.lower().startswith(("http", "github")):
            continue
        if folded in seen:
            continue
        seen.add(folded)
        result.append(cleaned)
        if len(result) >= 5:
            break
    return result


def _fit(text: str) -> str:
    text = " ".join(text.split()).strip()
    if len(text) <= _MAX_GITHUB_QUERY_CHARS:
        return text
    clipped = text[:_MAX_GITHUB_QUERY_CHARS]
    if " " in clipped:
        clipped = clipped.rsplit(" ", 1)[0]
    return clipped.strip()


def normalize_github_query(query: ResearchQueryV1) -> GitHubQueryNormalization:
    if query.channel not in {
        ResearchChannel.GITHUB_REPOSITORIES,
        ResearchChannel.GITHUB_CODE,
        ResearchChannel.GITHUB_ISSUES,
    }:
        return GitHubQueryNormalization(query, query, "", "")

    owner, repo, path = _extract_repo(query.text)
    words = _keywords(query.text, owner=owner, repo=repo)
    full_name = f"{owner}/{repo}" if owner and repo else ""

    if query.channel is ResearchChannel.GITHUB_REPOSITORIES:
        if full_name:
            normalized_text = f"{repo} user:{owner} in:name"
        else:
            normalized_text = " ".join(words) or query.text

    elif query.channel is ResearchChannel.GITHUB_CODE:
        filename = path.rsplit("/", 1)[-1] if path else ""
        strong = filename or (words[0] if words else "")
        if full_name and strong:
            normalized_text = f"{strong} repo:{full_name}"
        elif full_name:
            normalized_text = f"README repo:{full_name}"
        else:
            normalized_text = " ".join(words) or query.text

    else:
        terms = " ".join(words[:3])
        if full_name and terms:
            normalized_text = f"{terms} repo:{full_name}"
        elif full_name:
            normalized_text = f"is:issue repo:{full_name}"
        else:
            normalized_text = " ".join(words) or query.text

    normalized_text = _fit(normalized_text)
    normalized = ResearchQueryV1(query.query_id, normalized_text, query.channel)
    return GitHubQueryNormalization(query, normalized, full_name, path)


def _is_validation_422(exc: ResearchProviderError) -> bool:
    message = str(exc)
    return "HTTP 422" in message or "status=422" in message


def _fallback_query(
    normalization: GitHubQueryNormalization,
) -> ResearchQueryV1 | None:
    query = normalization.original
    repo_ref = normalization.repository
    path = normalization.path

    if query.channel is ResearchChannel.GITHUB_REPOSITORIES and repo_ref:
        owner, repo = repo_ref.split("/", 1)
        text = f"{repo} user:{owner}"
    elif query.channel is ResearchChannel.GITHUB_CODE and repo_ref:
        term = path.rsplit("/", 1)[-1] if path else "README"
        text = f"{term} repo:{repo_ref}"
    elif query.channel is ResearchChannel.GITHUB_ISSUES and repo_ref:
        text = f"is:issue repo:{repo_ref}"
    else:
        words = _keywords(query.text)
        if not words:
            return None
        text = " ".join(words[:2])

    text = _fit(text)
    if not text or text == normalization.normalized.text:
        return None
    return ResearchQueryV1(query.query_id, text, query.channel)


class BoundedGitHubProvider:
    """Discovery guard with frozen GitHub fetch semantics.

    Only GitHub validation failures caused by the generated search query receive
    a bounded fallback. Authentication, rate-limit, server, and network errors
    remain fatal provider errors.
    """

    provider_id = "github-rest"

    def __init__(self, inner: GitHubRestProvider) -> None:
        self.inner = inner

    def search(
        self,
        query: ResearchQueryV1,
        *,
        limit: int,
    ) -> Sequence[SearchHitV1]:
        normalization = normalize_github_query(query)
        try:
            return self.inner.search(normalization.normalized, limit=limit)
        except ResearchProviderError as exc:
            if not _is_validation_422(exc):
                raise
            fallback = _fallback_query(normalization)
            if fallback is None:
                return ()
            try:
                return self.inner.search(fallback, limit=limit)
            except ResearchProviderError as retry_exc:
                if _is_validation_422(retry_exc):
                    return ()
                raise

    def fetch(self, hit: SearchHitV1):
        return self.inner.fetch(hit)

# Verified compatibility aliases for historical Python interfaces.
Phase20RGitHubProvider = BoundedGitHubProvider
