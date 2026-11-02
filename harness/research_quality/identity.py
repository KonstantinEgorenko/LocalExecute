"""Canonical document/source-family identity for research evidence."""
from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

from .models import SourceIdentityV1

_GITHUB = {"github.com", "www.github.com", "api.github.com", "raw.githubusercontent.com"}


def _clean_url(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parts = urlsplit(text)
    scheme = parts.scheme.casefold()
    host = (parts.hostname or "").casefold()
    port = parts.port
    netloc = host if port is None else f"{host}:{port}"
    path = re.sub(r"/+", "/", parts.path or "/")
    if path != "/":
        path = path.rstrip("/")
    return urlunsplit((scheme, netloc, path, "", ""))


def _github_identity(url: str) -> tuple[str, str, str, str] | None:
    parts = urlsplit(url)
    host = (parts.hostname or "").casefold()
    if host not in _GITHUB:
        return None

    seg = [item for item in parts.path.split("/") if item]
    owner = repo = ""
    document = "@repo-root"

    if host == "api.github.com" and len(seg) >= 3 and seg[0] == "repos":
        owner, repo = seg[1], seg[2]
        if len(seg) >= 5 and seg[3] == "contents":
            document = "/".join(seg[4:]) or "@repo-root"
    elif host == "raw.githubusercontent.com" and len(seg) >= 4:
        owner, repo = seg[0], seg[1]
        document = "/".join(seg[3:]) or "@repo-root"
    elif len(seg) >= 2:
        owner, repo = seg[0], seg[1]
        if len(seg) >= 5 and seg[2] in {"blob", "raw"}:
            document = "/".join(seg[4:]) or "@repo-root"
        elif len(seg) >= 4 and seg[2] == "tree":
            document = "@repo-root"

    if not owner or not repo:
        return None

    family = f"github:{owner.casefold()}/{repo.casefold()}"
    publisher = f"github:{owner.casefold()}"
    document_key = f"{family}:{document.casefold()}"
    return document_key, family, publisher, "github.com"


class SourceIdentityResolver:
    def resolve(self, source: dict) -> SourceIdentityV1:
        source_id = str(source.get("source_id", "") or "")
        url = _clean_url(
            source.get("canonical_url")
            or source.get("url")
            or source.get("display_url")
            or ""
        )
        tier = str(source.get("tier") or source.get("provenance_tier") or "UNKNOWN")
        kind = str(source.get("kind") or source.get("source_kind") or "UNKNOWN")

        github = _github_identity(url)
        if github is not None:
            document_key, family, publisher, domain = github
        else:
            parts = urlsplit(url)
            domain = (parts.hostname or "").casefold()
            document_key = url.casefold() if url else f"source:{source_id}"
            family = domain or f"source:{source_id}"
            publisher = domain or family

        return SourceIdentityV1(
            source_id=source_id,
            canonical_url=url,
            document_key=document_key,
            source_family=family,
            publisher_key=publisher,
            domain=domain,
            tier=tier.upper(),
            kind=kind.upper(),
        )
