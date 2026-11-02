"""Exact, target-bound GitHub primary artifact acquisition."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Mapping

from harness.external_evidence.models import ResearchWorkflowPolicy
from harness.external_evidence.providers import HttpReadOnlyClient, ResearchProviderError

from .models import EntityKind, TargetEntityV1



_LICENSE_CONTEXT_PATHS = (
    "LICENSE-MIT",
    "LICENSE-APACHE",
    "LICENSE.md",
    "LICENSING.md",
    "COPYING",
    "COPYING.MIT",
    "COPYING.APACHE",
    "docs/project/licensing.md",
    "docs/licensing.md",
)


def _artifact_kind_for_path(path: str) -> str:
    lower = path.casefold()
    name = lower.rsplit("/", 1)[-1]
    if name.startswith("license") or name.startswith("copying"):
        return "LICENSE"
    if "licens" in lower:
        return "LICENSE_POLICY"
    return "FILE"

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sid(canonical_url: str) -> str:
    return "src20q:" + hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()[:24]


def _eid(source_id: str, locator: str, text: str) -> str:
    return "ev20q:" + hashlib.sha256(f"{source_id}\0{locator}\0{text}".encode("utf-8")).hexdigest()[:24]


class GitHubPrimaryResolver:
    def __init__(self, root, *, client: HttpReadOnlyClient | None = None, token: str = "") -> None:
        self.policy = ResearchWorkflowPolicy.from_project_root(root)
        self.client = client or HttpReadOnlyClient(self.policy)
        self.base = self.policy.github_api_base_url.rstrip("/")
        self.token = token or os.getenv(self.policy.github_token_env, "").strip()

    def _headers(self) -> dict[str, str]:
        out = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            out["Authorization"] = f"Bearer {self.token}"
        return out

    def _json(self, path: str) -> Mapping[str, Any]:
        return self.client.get_json(self.base + path, headers=self._headers())

    @staticmethod
    def _decode_content(raw: Mapping) -> str:
        content = raw.get("content")
        if isinstance(content, str) and raw.get("encoding") == "base64":
            try:
                return base64.b64decode(content.encode("ascii")).decode("utf-8", errors="replace")
            except Exception:
                return ""
        return str(content or "")

    def _acquire_exact_file(self, full: str, path: str, observed: str) -> tuple[dict, dict] | None:
        encoded = urllib.parse.quote(path, safe="/")
        try:
            raw = self._json(f"/repos/{full}/contents/{encoded}")
        except ResearchProviderError:
            return None
        if str(raw.get("type") or "file") not in {"", "file"}:
            return None
        text = self._decode_content(raw)
        if not text:
            return None
        actual_path = str(raw.get("path") or path)
        artifact_kind = _artifact_kind_for_path(actual_path)
        canonical = str(raw.get("html_url") or f"https://github.com/{full}/blob/main/{actual_path}")
        metadata = {
            "artifact_kind": artifact_kind,
            "artifact_path": actual_path,
            "repository": full,
            "sha": str(raw.get("sha") or ""),
            "observed_at": observed,
            "retrieved_at": observed,
            "acquisition": "EXACT_CONTENTS_API",
        }
        source = self._source(canonical, f"{full}:{actual_path}", "GITHUB_CODE", text, metadata, observed)
        return source, self._evidence(source, actual_path, text, observed)

    def acquire(self, target: TargetEntityV1, *, artifacts: tuple[str, ...]) -> tuple[list[dict], list[dict], dict]:
        if target.kind is not EntityKind.GITHUB_REPOSITORY:
            return [], [], {"status": "SKIPPED", "reason": "target is not GitHub repository"}
        full = target.github_full_name
        observed = _now()
        sources: list[dict] = []
        evidence: list[dict] = []
        metrics = {"status": "COMPLETE", "requested": list(artifacts), "acquired": [], "missing": [], "exact_paths_acquired": []}

        repo: Mapping[str, Any] | None = None
        if "repository_metadata" in artifacts:
            try:
                repo = self._json(f"/repos/{full}")
            except ResearchProviderError:
                metrics["missing"].append("repository_metadata")
            else:
                canonical = f"https://api.github.com/repos/{full}"
                metadata = {
                    "artifact_kind": "REPOSITORY_METADATA",
                    "artifact_path": "@metadata",
                    "repository": full,
                    "repository_id": str(repo.get("id") or ""),
                    "node_id": str(repo.get("node_id") or ""),
                    "observed_at": observed,
                    "retrieved_at": observed,
                }
                text_obj = {
                    "full_name": repo.get("full_name"),
                    "description": repo.get("description"),
                    "default_branch": repo.get("default_branch"),
                    "stargazers_count": repo.get("stargazers_count"),
                    "forks_count": repo.get("forks_count"),
                    "open_issues_count": repo.get("open_issues_count"),
                    "subscribers_count": repo.get("subscribers_count"),
                    "license": repo.get("license"),
                    "created_at": repo.get("created_at"),
                    "updated_at": repo.get("updated_at"),
                    "pushed_at": repo.get("pushed_at"),
                    "observed_at": observed,
                }
                text = json.dumps(text_obj, ensure_ascii=False, sort_keys=True)
                source = self._source(canonical, f"{full} repository metadata", "GITHUB_REPOSITORY", text, metadata, observed)
                sources.append(source)
                evidence.append(self._evidence(source, "repository_metadata", text, observed))
                metrics["acquired"].append("repository_metadata")

        if "readme" in artifacts:
            try:
                raw = self._json(f"/repos/{full}/readme")
            except ResearchProviderError:
                metrics["missing"].append("readme")
            else:
                text = self._decode_content(raw)
                if text:
                    path = str(raw.get("path") or "README.md")
                    sha = str(raw.get("sha") or "")
                    canonical = str(raw.get("html_url") or f"https://github.com/{full}/blob/main/{path}")
                    metadata = {"artifact_kind": "README", "artifact_path": path, "repository": full, "sha": sha, "observed_at": observed, "retrieved_at": observed}
                    source = self._source(canonical, f"{full}:{path}", "GITHUB_CODE", text, metadata, observed)
                    sources.append(source)
                    evidence.append(self._evidence(source, path, text, observed))
                    metrics["acquired"].append("readme")
                else:
                    metrics["missing"].append("readme")

        if "license" in artifacts:
            try:
                raw = self._json(f"/repos/{full}/license")
            except ResearchProviderError:
                metrics["missing"].append("license")
            else:
                text = self._decode_content(raw)
                license_meta = raw.get("license") if isinstance(raw.get("license"), Mapping) else {}
                path = str(raw.get("path") or "LICENSE")
                summary = {
                    "license_name": license_meta.get("name"),
                    "spdx_id": license_meta.get("spdx_id"),
                    "key": license_meta.get("key"),
                    "path": path,
                    "observed_at": observed,
                }
                combined = json.dumps(summary, ensure_ascii=False, sort_keys=True) + ("\n\n" + text if text else "")
                canonical = str(raw.get("html_url") or f"https://github.com/{full}/blob/main/{path}")
                metadata = {"artifact_kind": "LICENSE", "artifact_path": path, "repository": full, "sha": str(raw.get("sha") or ""), "spdx_id": str(license_meta.get("spdx_id") or ""), "observed_at": observed, "retrieved_at": observed}
                source = self._source(canonical, f"{full}:{path}", "GITHUB_CODE", combined, metadata, observed)
                sources.append(source)
                evidence.append(self._evidence(source, path, combined, observed))
                metrics["acquired"].append("license")

        if "license_context" in artifacts:
            existing_urls = {str(item.get("canonical_url", "")) for item in sources}
            acquired_any = False
            for candidate in _LICENSE_CONTEXT_PATHS:
                result = self._acquire_exact_file(full, candidate, observed)
                if result is None:
                    continue
                source, item = result
                if str(source.get("canonical_url", "")) in existing_urls:
                    continue
                existing_urls.add(str(source.get("canonical_url", "")))
                sources.append(source)
                evidence.append(item)
                metrics["exact_paths_acquired"].append(str(source["metadata"]["artifact_path"]))
                acquired_any = True
            if acquired_any:
                metrics["acquired"].append("license_context")
            else:
                metrics["missing"].append("license_context")
        return sources, evidence, metrics

    @staticmethod
    def _source(canonical: str, title: str, kind: str, text: str, metadata: Mapping, observed: str) -> dict:
        sid = _sid(canonical)
        return {
            "source_id": sid,
            "provider_id": "phase20q-github-primary",
            "canonical_url": canonical,
            "title": title,
            "kind": kind,
            "tier": "PRIMARY",
            "content_type": "application/json+text",
            "text": text,
            "bytes_read": len(text.encode("utf-8")),
            "content_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "retrieved_at": observed,
            "observed_at": observed,
            "metadata": dict(metadata),
        }

    @staticmethod
    def _evidence(source: Mapping, locator: str, text: str, observed: str) -> dict:
        excerpt = text[:12000]
        eid = _eid(str(source["source_id"]), locator, excerpt)
        return {
            "evidence_id": eid,
            "source_id": str(source["source_id"]),
            "locator": locator,
            "excerpt": excerpt,
            "text": excerpt,
            "topic": "",
            "observed_at": observed,
            "retrieved_at": observed,
            "metadata": {"provider": "phase20q-github-primary", **dict(source.get("metadata") or {})},
        }
