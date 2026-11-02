"""Target-entity binding and canonical-document identity V2."""
from __future__ import annotations

import re
from collections import Counter
from typing import Mapping, Sequence
from urllib.parse import urlsplit

from harness.research_quality.identity import SourceIdentityResolver

from .models import (
    CanonicalDocumentV2,
    EntityBindingStatus,
    EntityBindingV1,
    EntityKind,
    TargetEntityV1,
)

_GITHUB_HOSTS = {"github.com", "www.github.com", "api.github.com", "raw.githubusercontent.com"}


_QUALITY_IDENTITY = SourceIdentityResolver()


def quality_document_key(source: Mapping) -> str:
    'Project a source onto frozen research quality canonical-document identity.\n\n    research integrity keeps richer artifact identity (for example @metadata versus an\n    exact repository file), but final quality acceptance is computed by the\n    frozen research quality SourceIdentityResolver.  Any diversity/closure gate that\n    predicts research quality quality must therefore count documents in this namespace.\n    The github-repo prefix is normalized only so it can be compared with\n    research integrity remediation constraints; the underlying research quality document path is\n    unchanged.\n    '
    key = _QUALITY_IDENTITY.resolve(dict(source)).document_key
    if key.startswith("github:"):
        return "github-repo:" + key[len("github:"):]
    return key


def _text(value: object) -> str:
    return str(value or "").strip()


def github_repo_from_source(source: Mapping) -> tuple[str, str] | None:
    metadata = source.get("metadata") if isinstance(source.get("metadata"), Mapping) else {}
    for key in ("repository", "full_name", "repository_full_name"):
        value = _text(metadata.get(key))
        if value.count("/") == 1:
            owner, repo = value.split("/", 1)
            if owner and repo:
                return owner.casefold(), repo.removesuffix(".git").casefold()
    repository_url = _text(metadata.get("repository_url"))
    if repository_url:
        parsed = github_repo_from_url(repository_url)
        if parsed:
            return parsed
    return github_repo_from_url(_text(source.get("canonical_url") or source.get("url")))


def github_repo_from_url(url: str) -> tuple[str, str] | None:
    if not url:
        return None
    parts = urlsplit(url)
    host = (parts.hostname or "").casefold()
    if host not in _GITHUB_HOSTS:
        return None
    seg = [x for x in parts.path.split("/") if x]
    if host == "api.github.com":
        if len(seg) >= 3 and seg[0] == "repos":
            return seg[1].casefold(), seg[2].removesuffix(".git").casefold()
        return None
    if host == "raw.githubusercontent.com":
        if len(seg) >= 2:
            return seg[0].casefold(), seg[1].removesuffix(".git").casefold()
        return None
    if len(seg) >= 2:
        return seg[0].casefold(), seg[1].removesuffix(".git").casefold()
    return None


def _artifact_kind_from_path(path: str) -> str:
    normalized = _text(path).replace("\\", "/").strip("/")
    if not normalized:
        return ""
    lower = normalized.casefold()
    name = lower.rsplit("/", 1)[-1]
    if name.startswith("license") or name.startswith("copying"):
        return "LICENSE"
    if "licens" in lower:
        return "LICENSE_POLICY"
    if name.startswith("readme"):
        return "README"
    return "FILE"


def _github_path(source: Mapping, repo: tuple[str, str]) -> tuple[str, str, str]:
    metadata = source.get("metadata") if isinstance(source.get("metadata"), Mapping) else {}
    artifact_kind = _text(metadata.get("artifact_kind")).upper()
    artifact_path = _text(metadata.get("artifact_path") or metadata.get("path"))
    revision = _text(metadata.get("sha") or metadata.get("blob_sha") or metadata.get("revision"))
    if artifact_kind == "REPOSITORY_METADATA":
        return "@metadata", revision or _text(metadata.get("observed_at")), "REPOSITORY_METADATA"
    if artifact_kind == "LICENSE":
        return artifact_path or "LICENSE", revision, "LICENSE"
    if artifact_kind == "README":
        return artifact_path or "README.md", revision, "README"
    if artifact_kind and artifact_path:
        return artifact_path, revision, artifact_kind
    url = _text(source.get("canonical_url") or source.get("url"))
    parts = urlsplit(url)
    host = (parts.hostname or "").casefold()
    seg = [x for x in parts.path.split("/") if x]
    if not revision:
        if host == "raw.githubusercontent.com" and len(seg) >= 3:
            revision = seg[2]
        elif host in {"github.com", "www.github.com"} and len(seg) >= 4 and seg[2] in {"blob", "raw", "tree"}:
            revision = seg[3]
    # external evidence/GitHub code-search sources already retain the exact repository
    # path even when they do not carry Phase20Q's explicit artifact_kind.
    # Recover the semantic artifact class from that trusted repository path
    # before falling back to URL parsing.  This prevents exact LICENSE /
    # licensing-policy files from collapsing to REPOSITORY_ROOT/FILE merely
    # because they entered through the older provider representation.
    if artifact_path:
        inferred = _artifact_kind_from_path(artifact_path)
        if inferred:
            return artifact_path, revision, inferred
    if host == "api.github.com" and len(seg) >= 4 and seg[0] == "repos":
        if seg[3] == "license":
            return "LICENSE", revision, "LICENSE"
        if seg[3] == "readme":
            return artifact_path or "README.md", revision, "README"
        if seg[3] == "contents":
            return "/".join(seg[4:]) or "@repo-root", revision, "FILE"
        return "@metadata", revision, "REPOSITORY_METADATA"
    if host == "raw.githubusercontent.com" and len(seg) >= 4:
        path = "/".join(seg[3:]) or "@repo-root"
        return path, seg[2], _artifact_kind_from_path(path) or "FILE"
    if host in {"github.com", "www.github.com"} and len(seg) >= 2:
        if len(seg) >= 5 and seg[2] in {"blob", "raw"}:
            path = "/".join(seg[4:]) or "@repo-root"
            return path, seg[3], _artifact_kind_from_path(path) or "FILE"
        if len(seg) >= 4 and seg[2] == "tree":
            return "@repo-root", seg[3], "REPOSITORY_ROOT"
    return "@repo-root", revision, "REPOSITORY_ROOT"


class TargetEntityResolver:
    """Infer a repository target from the original claim's cited evidence first.

    The resolver is deliberately conservative.  It does not infer a target from
    arbitrary targeted-search results, which is the path that caused the LoopX
    entity leak.
    """

    def resolve_for_claim(self, report: Mapping, claim: Mapping) -> TargetEntityV1:
        evidence_by_id = {
            _text(x.get("evidence_id")): x
            for x in report.get("evidence", []) if isinstance(x, Mapping)
        }
        source_by_id = {
            _text(x.get("source_id")): x
            for x in report.get("sources", []) if isinstance(x, Mapping)
        }
        repos: list[tuple[str, str]] = []
        canonical_urls: list[str] = []
        for ref in claim.get("evidence", []):
            if not isinstance(ref, Mapping):
                continue
            ev = evidence_by_id.get(_text(ref.get("evidence_id")))
            if not isinstance(ev, Mapping):
                continue
            sid = _text(ev.get("source_id") or ev.get("source") or ev.get("source_ref"))
            source = source_by_id.get(sid)
            if not isinstance(source, Mapping):
                continue
            repo = github_repo_from_source(source)
            if repo:
                repos.append(repo)
                canonical_urls.append(_text(source.get("canonical_url")))
        if not repos:
            # Fallback only to an unambiguous GitHub repo present in the question.
            question = _text(report.get("question"))
            found = re.findall(r"(?:github\.com/)?([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)", question)
            unique = {(a.casefold(), b.removesuffix(".git").casefold()) for a, b in found}
            if len(unique) == 1:
                repos = list(unique)
        if not repos:
            return TargetEntityV1(EntityKind.UNKNOWN, _text(report.get("question"))[:120], "unknown")
        counts = Counter(repos)
        repo, count = counts.most_common(1)[0]
        if len(counts) > 1 and count == counts.most_common(2)[1][1]:
            return TargetEntityV1(EntityKind.UNKNOWN, _text(report.get("question"))[:120], "ambiguous")
        owner, name = repo
        key = f"github-repo:{owner}/{name}"
        return TargetEntityV1(
            EntityKind.GITHUB_REPOSITORY,
            f"{owner}/{name}",
            key,
            github_owner=owner,
            github_repo=name,
            canonical_urls=tuple(x for x in canonical_urls if x),
        )


class EntityBindingResolver:
    def bind(self, source: Mapping, target: TargetEntityV1) -> EntityBindingV1:
        if target.kind is not EntityKind.GITHUB_REPOSITORY:
            return EntityBindingV1(EntityBindingStatus.UNKNOWN, target.canonical_key, "", "target identity unavailable")
        repo = github_repo_from_source(source)
        target_repo = (target.github_owner.casefold(), target.github_repo.casefold())
        if repo is not None:
            source_key = f"github-repo:{repo[0]}/{repo[1]}"
            if repo == target_repo:
                url = _text(source.get("canonical_url"))
                parts = urlsplit(url)
                seg = [x for x in parts.path.split("/") if x]
                status = EntityBindingStatus.EXACT if len(seg) <= 2 else EntityBindingStatus.OFFICIAL_CHILD
                return EntityBindingV1(status, target.canonical_key, source_key, "GitHub owner/repository match")
            return EntityBindingV1(EntityBindingStatus.MISMATCH, target.canonical_key, source_key, "different GitHub repository")
        # Non-GitHub pages may corroborate, but are never exact repository artifacts.
        domain = (urlsplit(_text(source.get("canonical_url"))).hostname or "").casefold()
        if domain:
            return EntityBindingV1(EntityBindingStatus.RELATED, target.canonical_key, f"web:{domain}", "non-GitHub source; relationship not structurally exact")
        return EntityBindingV1(EntityBindingStatus.UNKNOWN, target.canonical_key, "", "source entity unavailable")


class CanonicalDocumentResolverV2:
    def resolve(self, source: Mapping, target: TargetEntityV1 | None = None) -> CanonicalDocumentV2:
        repo = github_repo_from_source(source)
        if repo:
            entity_key = f"github-repo:{repo[0]}/{repo[1]}"
            path, revision, artifact_kind = _github_path(source, repo)
            normalized = path.replace("\\", "/").strip("/") or "@repo-root"
            document_key = f"{entity_key}:{normalized.casefold()}"
            return CanonicalDocumentV2(entity_key, document_key, revision, artifact_kind, normalized)
        url = _text(source.get("canonical_url") or source.get("url"))
        parts = urlsplit(url)
        domain = (parts.hostname or "").casefold().removeprefix("www.")
        path = re.sub(r"/+", "/", parts.path or "/").rstrip("/") or "/"
        key = f"web:{domain}:{path.casefold()}" if domain else f"source:{_text(source.get('source_id'))}"
        return CanonicalDocumentV2(f"web:{domain}" if domain else "unknown", key, "", "WEB_DOCUMENT", path)

# Verified compatibility aliases for historical Python interfaces.
phase19_compatible_document_key = quality_document_key
