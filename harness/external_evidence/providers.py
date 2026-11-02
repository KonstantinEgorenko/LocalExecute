'Read-only external research providers for external evidence.\n\nNo provider exposes repository mutation or shell execution. Search backends may\nbe private/self-hosted when explicitly configured, but discovered/fetched source\nURLs remain subject to the external-target SSRF policy.\n'
from __future__ import annotations

import html
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any, Mapping, Sequence

from .models import (
    FetchedSourceV1,
    ResearchWorkflowPolicy,
    ResearchChannel,
    ResearchQueryV1,
    SearchHitV1,
    SourceKind,
)
from .provenance import classify_source
from .security import canonicalize_url, validate_external_url


class ResearchProviderError(RuntimeError):
    pass


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lower = tag.casefold()
        if lower in {"script", "style", "noscript", "svg"}:
            self._skip += 1
        if lower == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        lower = tag.casefold()
        if lower in {"script", "style", "noscript", "svg"} and self._skip:
            self._skip -= 1
        if lower == "title":
            self._in_title = False
        if lower in {"p", "div", "section", "article", "li", "br", "h1", "h2", "h3", "h4", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        text = data.strip()
        if not text:
            return
        if self._in_title and not self.title:
            self.title = text
        self.parts.append(text)

    def text(self) -> str:
        value = " ".join(self.parts)
        value = re.sub(r"[ \t\r\f\v]+", " ", value)
        value = re.sub(r"\n\s*\n+", "\n\n", value)
        return html.unescape(value).strip()


class HttpReadOnlyClient:
    def __init__(self, policy: ResearchWorkflowPolicy) -> None:
        self.policy = policy

    def _request(self, url: str, *, headers: Mapping[str, str] | None = None, allow_private: bool = False) -> tuple[str, int, str, bytes]:
        current = canonicalize_url(url) if allow_private else validate_external_url(url, block_private=self.policy.block_private_network_targets)
        request_headers = {"User-Agent": "SwarmAgents-Phase18/1.0", "Accept": "text/html,application/json,text/plain;q=0.9,*/*;q=0.2"}
        request_headers.update(dict(headers or {}))
        for _ in range(self.policy.max_redirects + 1):
            request = urllib.request.Request(current, headers=request_headers, method="GET")
            opener = urllib.request.build_opener(_NoRedirectHandler())
            try:
                response = opener.open(request, timeout=self.policy.request_timeout_seconds)
            except urllib.error.HTTPError as exc:
                if exc.code in {301, 302, 303, 307, 308}:
                    location = exc.headers.get("Location")
                    if not location:
                        raise ResearchProviderError(f"redirect without Location: {current}") from exc
                    next_url = urllib.parse.urljoin(current, location)
                    current = canonicalize_url(next_url) if allow_private else validate_external_url(next_url, block_private=self.policy.block_private_network_targets)
                    continue
                raise ResearchProviderError(f"HTTP {exc.code} for {current}") from exc
            except (OSError, urllib.error.URLError) as exc:
                raise ResearchProviderError(f"request failed for {current}: {exc}") from exc
            with response:
                status = int(getattr(response, "status", 200))
                content_type = response.headers.get_content_type() or "application/octet-stream"
                data = response.read(self.policy.max_source_bytes + 1)
                if len(data) > self.policy.max_source_bytes:
                    raise ResearchProviderError("source exceeds max_source_bytes")
                final_url = response.geturl() or current
                if not allow_private:
                    final_url = validate_external_url(final_url, block_private=self.policy.block_private_network_targets)
                return final_url, status, content_type, data
        raise ResearchProviderError("redirect limit exceeded")

    def get_json(self, url: str, *, headers: Mapping[str, str] | None = None, allow_private: bool = False) -> Mapping[str, Any]:
        final_url, status, content_type, data = self._request(url, headers=headers, allow_private=allow_private)
        if status != 200:
            raise ResearchProviderError(f"unexpected HTTP status={status} for {final_url}")
        if content_type not in {"application/json", "text/json", "text/plain"}:
            raise ResearchProviderError(f"unexpected JSON content type: {content_type}")
        try:
            raw = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ResearchProviderError(f"invalid JSON from {final_url}: {exc}") from exc
        if not isinstance(raw, Mapping):
            raise ResearchProviderError("JSON response must be object")
        return raw

    def fetch_text(self, url: str) -> tuple[str, str, str, int]:
        final_url, status, content_type, data = self._request(url)
        if status != 200:
            raise ResearchProviderError(f"unexpected HTTP status={status}")
        if content_type in {"text/html", "application/xhtml+xml"}:
            decoded = data.decode("utf-8", errors="replace")
            parser = _TextExtractor()
            parser.feed(decoded)
            text = parser.text()
            title = parser.title or final_url
        elif content_type.startswith("text/") or content_type in {"application/json", "application/xml", "application/xhtml+xml"}:
            text = data.decode("utf-8", errors="replace").strip()
            title = final_url
        else:
            raise ResearchProviderError(f"unsupported content type: {content_type}")
        text = text[: self.policy.max_source_text_chars]
        return final_url, title, text, len(data)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


class SearxNgProvider:
    provider_id = "searxng"

    def __init__(self, policy: ResearchWorkflowPolicy, *, base_url: str | None = None, client: HttpReadOnlyClient | None = None) -> None:
        self.policy = policy
        self.client = client or HttpReadOnlyClient(policy)
        self.base_url = (base_url or os.getenv(policy.searxng_base_url_env, "")).strip().rstrip("/")
        if not self.base_url:
            raise ResearchProviderError(f"SearXNG base URL missing; set {policy.searxng_base_url_env}")

    @staticmethod
    def parse_results(raw: Mapping[str, Any], query: ResearchQueryV1, *, limit: int) -> list[SearchHitV1]:
        results = raw.get("results")
        if not isinstance(results, list):
            raise ResearchProviderError("SearXNG response missing results list")
        hits: list[SearchHitV1] = []
        for item in results:
            if not isinstance(item, Mapping):
                continue
            url = item.get("url")
            title = item.get("title") or url
            if not isinstance(url, str) or not isinstance(title, str):
                continue
            hits.append(SearchHitV1(
                "searxng", query.query_id, len(hits) + 1, title, url,
                str(item.get("content") or ""), SourceKind.WEB_PAGE,
                {"engine": item.get("engine"), "engines": item.get("engines") or []},
            ))
            if len(hits) >= limit:
                break
        return hits

    def search(self, query: ResearchQueryV1, *, limit: int) -> Sequence[SearchHitV1]:
        if query.channel is not ResearchChannel.WEB:
            return ()
        params = urllib.parse.urlencode({"q": query.text, "format": "json", "safesearch": "1"})
        raw = self.client.get_json(f"{self.base_url}/search?{params}", allow_private=self.policy.searxng_allow_private_backend)
        return self.parse_results(raw, query, limit=limit)

    def fetch(self, hit: SearchHitV1) -> FetchedSourceV1:
        final_url, title, text, bytes_read = self.client.fetch_text(hit.url)
        kind, tier = classify_source(final_url, hit.kind)
        return FetchedSourceV1(self.provider_id, final_url, title or hit.title, kind, tier, "text/plain", text, bytes_read, hit.metadata)


class GitHubRestProvider:
    provider_id = "github-rest"

    def __init__(self, policy: ResearchWorkflowPolicy, *, token: str | None = None, client: HttpReadOnlyClient | None = None) -> None:
        self.policy = policy
        self.client = client or HttpReadOnlyClient(policy)
        self.token = (token if token is not None else os.getenv(policy.github_token_env, "")).strip()
        self.base = policy.github_api_base_url.rstrip("/")

    def _headers(self, *, raw: bool = False) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github.raw+json" if raw else "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    @staticmethod
    def parse_search(raw: Mapping[str, Any], query: ResearchQueryV1, *, limit: int) -> list[SearchHitV1]:
        items = raw.get("items")
        if not isinstance(items, list):
            raise ResearchProviderError("GitHub search response missing items")
        hits: list[SearchHitV1] = []
        for item in items:
            if not isinstance(item, Mapping):
                continue
            if query.channel is ResearchChannel.GITHUB_REPOSITORIES:
                url = item.get("html_url")
                title = item.get("full_name")
                kind = SourceKind.GITHUB_REPOSITORY
                metadata = {"full_name": item.get("full_name"), "default_branch": item.get("default_branch"), "api_url": item.get("url")}
                snippet = str(item.get("description") or "")
            elif query.channel is ResearchChannel.GITHUB_CODE:
                url = item.get("html_url")
                repo = item.get("repository") if isinstance(item.get("repository"), Mapping) else {}
                title = f"{repo.get('full_name', 'repo')}:{item.get('path', '')}"
                kind = SourceKind.GITHUB_CODE
                metadata = {"repository": repo.get("full_name"), "path": item.get("path"), "api_url": item.get("url")}
                snippet = ""
            else:
                url = item.get("html_url")
                title = item.get("title") or item.get("html_url")
                kind = SourceKind.GITHUB_ISSUE
                metadata = {"api_url": item.get("url"), "repository_url": item.get("repository_url")}
                snippet = str(item.get("body") or "")[:2000]
            if isinstance(url, str) and isinstance(title, str):
                hits.append(SearchHitV1("github-rest", query.query_id, len(hits) + 1, title, url, snippet, kind, metadata))
            if len(hits) >= limit:
                break
        return hits

    def search(self, query: ResearchQueryV1, *, limit: int) -> Sequence[SearchHitV1]:
        endpoint = {
            ResearchChannel.GITHUB_REPOSITORIES: "repositories",
            ResearchChannel.GITHUB_CODE: "code",
            ResearchChannel.GITHUB_ISSUES: "issues",
        }.get(query.channel)
        if endpoint is None:
            return ()
        params = urllib.parse.urlencode({"q": query.text, "per_page": min(limit, 100)})
        raw = self.client.get_json(f"{self.base}/search/{endpoint}?{params}", headers=self._headers())
        return self.parse_search(raw, query, limit=limit)

    def fetch(self, hit: SearchHitV1) -> FetchedSourceV1:
        metadata = dict(hit.metadata)
        text = hit.snippet
        title = hit.title
        content_type = "application/json"
        bytes_read = len(text.encode("utf-8"))
        if hit.kind is SourceKind.GITHUB_REPOSITORY:
            full_name = metadata.get("full_name")
            if isinstance(full_name, str) and full_name:
                try:
                    final_url, status, ctype, data = self.client._request(
                        f"{self.base}/repos/{full_name}/readme", headers=self._headers(raw=True)
                    )
                    if status == 200:
                        text = data.decode("utf-8", errors="replace")[: self.policy.max_source_text_chars]
                        bytes_read = len(data)
                        content_type = ctype
                        metadata["readme_api_url"] = final_url
                except ResearchProviderError:
                    pass
        elif isinstance(metadata.get("api_url"), str):
            raw = self.client.get_json(str(metadata["api_url"]), headers=self._headers())
            text = json.dumps(raw, ensure_ascii=False, sort_keys=True)[: self.policy.max_source_text_chars]
            bytes_read = len(text.encode("utf-8"))
        kind, tier = classify_source(hit.url, hit.kind)
        return FetchedSourceV1(self.provider_id, canonicalize_url(hit.url), title, kind, tier, content_type, text, bytes_read, metadata)
