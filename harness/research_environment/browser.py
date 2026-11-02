from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .models import BrowserActionRecord, BrowserElement, BrowserObservation
from .policy import ResearchEnvironmentPolicy
from .url_policy import ResearchUrlPolicy, UnsafeResearchUrl


class BrowserBackendUnavailable(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name or "download.bin").strip("._")
    return (cleaned or "download.bin")[:180]


class ScriptedBrowserSession:
    def __init__(self, observations: Iterable[BrowserObservation]) -> None:
        self._observations = list(observations)
        if not self._observations:
            raise ValueError("at least one observation is required")
        self._index = 0

    def observe(self) -> BrowserObservation:
        return self._observations[self._index]

    def navigate(self, url: str) -> BrowserObservation:
        if self._index + 1 < len(self._observations):
            self._index += 1
        return self.observe()


class PlaywrightBrowserSession:
    def __init__(self, policy: ResearchEnvironmentPolicy, *, downloads_dir: Path) -> None:
        self.policy = policy
        self.url_policy = ResearchUrlPolicy(policy)
        self.downloads_dir = Path(downloads_dir).resolve()
        self.downloads_dir.mkdir(parents=True, exist_ok=True)
        self._trail: list[BrowserActionRecord] = []
        self._sequence = 0
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None

    def __enter__(self):
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            raise BrowserBackendUnavailable(
                "Playwright missing. Run scripts/install_research_environment_dependencies.ps1"
            ) from exc

        self._playwright = sync_playwright().start()
        try:
            self._browser = self._playwright.chromium.launch(headless=self.policy.headless)
        except Exception as exc:
            self._playwright.stop()
            self._playwright = None
            raise BrowserBackendUnavailable(
                "Chromium unavailable. Run: python -m playwright install chromium"
            ) from exc

        self._context = self._browser.new_context(accept_downloads=True)
        self._page = self._context.new_page()
        self._page.set_default_navigation_timeout(self.policy.navigation_timeout_ms)
        self._page.set_default_timeout(self.policy.action_timeout_ms)
        self._page.route("**/*", self._route_guard)
        return self

    def __exit__(self, exc_type, exc, tb):
        for item in (self._context, self._browser):
            try:
                if item is not None:
                    item.close()
            except Exception:
                pass
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
        self._page = self._context = self._browser = self._playwright = None

    def _require_page(self):
        if self._page is None:
            raise BrowserBackendUnavailable("browser session is not started")
        return self._page

    def _route_guard(self, route):
        try:
            self.url_policy.validate_network_request(route.request.url)
        except UnsafeResearchUrl:
            route.abort("blockedbyclient")
        else:
            route.continue_()

    def _record(self, action, target, detail, url_before, *, success, error=""):
        page = self._require_page()
        self._sequence += 1
        self._trail.append(
            BrowserActionRecord(
                self._sequence, action, target, detail, url_before, page.url,
                _now(), success, error
            )
        )
        if len(self._trail) > self.policy.max_action_trail:
            self._trail = self._trail[-self.policy.max_action_trail:]

    def navigate(self, url: str) -> BrowserObservation:
        page = self._require_page()
        normalized = self.url_policy.validate_top_level(url)
        before = page.url
        try:
            page.goto(normalized, wait_until="domcontentloaded")
            self.url_policy.validate_top_level(page.url)
        except Exception as exc:
            self._record("NAVIGATE", normalized, "", before, success=False, error=str(exc))
            raise
        self._record("NAVIGATE", normalized, "", before, success=True)
        return self.observe()

    def observe(self) -> BrowserObservation:
        page = self._require_page()
        raw = page.evaluate(
            """
            (limit) => {
              const selector = 'a[href],button,input,textarea,select,[role="button"],[role="link"]';
              const nodes = Array.from(document.querySelectorAll(selector));
              return nodes.filter((el) => {
                const r = el.getBoundingClientRect();
                const s = window.getComputedStyle(el);
                return r.width > 0 && r.height > 0 &&
                       s.visibility !== 'hidden' && s.display !== 'none';
              }).slice(0, limit).map((el, index) => {
                const bid = `swarm-${index + 1}`;
                el.setAttribute('data-swarm-bid', bid);
                return {
                  element_id: bid,
                  tag: (el.tagName || '').toLowerCase(),
                  role: el.getAttribute('role') || '',
                  text: (el.innerText || el.value || el.textContent || '').trim().slice(0, 500),
                  name: (el.getAttribute('aria-label') || el.getAttribute('name') || '').slice(0, 300),
                  href: (el.href || '').slice(0, 2000),
                  input_type: (el.getAttribute('type') || '').slice(0, 100)
                };
              });
            }
            """,
            self.policy.max_interactive_elements,
        )
        text = page.locator("body").inner_text(timeout=self.policy.action_timeout_ms)
        return BrowserObservation(
            url=page.url,
            title=page.title(),
            text=text[:self.policy.max_observation_chars],
            elements=tuple(BrowserElement(**item) for item in raw),
            observed_at=_now(),
            action_trail=tuple(self._trail),
        )

    def _locator(self, element_id: str):
        return self._require_page().locator(f'[data-swarm-bid="{element_id}"]')

    def click(self, element_id: str) -> BrowserObservation:
        page = self._require_page()
        before = page.url
        try:
            self._locator(element_id).click()
            page.wait_for_timeout(150)
            if page.url.startswith(("http://", "https://")):
                self.url_policy.validate_top_level(page.url)
        except Exception as exc:
            self._record("CLICK", element_id, "", before, success=False, error=str(exc))
            raise
        self._record("CLICK", element_id, "", before, success=True)
        return self.observe()

    def fill(self, element_id: str, text: str) -> BrowserObservation:
        page = self._require_page()
        before = page.url
        try:
            self._locator(element_id).fill(text)
        except Exception as exc:
            self._record("FILL", element_id, f"chars={len(text)}", before, success=False, error=str(exc))
            raise
        self._record("FILL", element_id, f"chars={len(text)}", before, success=True)
        return self.observe()

    def scroll(self, delta_y: int) -> BrowserObservation:
        page = self._require_page()
        before = page.url
        page.evaluate("(dy) => window.scrollBy(0, dy)", int(delta_y))
        self._record("SCROLL", "", f"delta_y={int(delta_y)}", before, success=True)
        return self.observe()

    def back(self) -> BrowserObservation:
        page = self._require_page()
        before = page.url
        page.go_back(wait_until="domcontentloaded")
        if page.url.startswith(("http://", "https://")):
            self.url_policy.validate_top_level(page.url)
        self._record("BACK", "", "", before, success=True)
        return self.observe()

    def forward(self) -> BrowserObservation:
        page = self._require_page()
        before = page.url
        page.go_forward(wait_until="domcontentloaded")
        if page.url.startswith(("http://", "https://")):
            self.url_policy.validate_top_level(page.url)
        self._record("FORWARD", "", "", before, success=True)
        return self.observe()

    def download_click(self, element_id: str) -> Path:
        page = self._require_page()
        before = page.url
        try:
            with page.expect_download() as info:
                self._locator(element_id).click()
            download = info.value
            target = self.downloads_dir / _safe_filename(download.suggested_filename)
            download.save_as(str(target))
            if target.stat().st_size > self.policy.max_download_bytes:
                target.unlink(missing_ok=True)
                raise ValueError("download exceeds max_download_bytes")
        except Exception as exc:
            self._record("DOWNLOAD", element_id, "", before, success=False, error=str(exc))
            raise
        self._record("DOWNLOAD", element_id, f"path={target.name}", before, success=True)
        return target
