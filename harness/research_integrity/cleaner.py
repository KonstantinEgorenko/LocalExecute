"""Deterministic HTML cleaner with metadata preservation.

The heuristics are intentionally small: structural tags, navigation indicators,
link density and text density.  This is a Swarm-native implementation, not an
embedded Defuddle dependency.
"""
from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from typing import Mapping

_BLOCK = {"article", "main", "section", "div", "p", "li", "pre", "code", "table", "tr", "td", "th", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6"}
_SKIP = {"script", "style", "noscript", "svg", "canvas"}
_NAV = re.compile(r"\b(nav|menu|sidebar|footer|cookie|advert|sponsor|subscribe|newsletter|related|recommended|share|social|trending)\b", re.I)
_CONTENT = re.compile(r"\b(article|content|main|post|story|documentation|docs|readme)\b", re.I)


class _BlockParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[dict] = []
        self.blocks: list[dict] = []
        self.title = ""
        self.canonical_hint = ""
        self.meta: dict[str, str] = {}
        self.skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        tag = tag.casefold()
        attr = {str(k).casefold(): str(v or "") for k, v in attrs}
        if tag in _SKIP:
            self.skip += 1
        if tag == "title":
            self._in_title = True
        if tag == "link" and attr.get("rel", "").casefold() == "canonical":
            self.canonical_hint = attr.get("href", "")
        if tag == "meta":
            key = attr.get("property") or attr.get("name")
            if key and attr.get("content"):
                self.meta[key.casefold()] = attr["content"]
        if tag in _BLOCK:
            self.stack.append({"tag": tag, "attrs": attr, "text": [], "links": 0, "link_text": 0})
        elif tag == "a":
            for block in self.stack:
                block["links"] += 1

    def handle_endtag(self, tag):
        tag = tag.casefold()
        if tag in _SKIP and self.skip:
            self.skip -= 1
        if tag == "title":
            self._in_title = False
        if tag in _BLOCK and self.stack:
            block = self.stack.pop()
            text = re.sub(r"\s+", " ", " ".join(block["text"])).strip()
            block["text"] = text
            if text:
                self.blocks.append(block)
            if self.stack and text:
                self.stack[-1]["text"].append(text)

    def handle_data(self, data):
        if self.skip:
            return
        text = data.strip()
        if not text:
            return
        if self._in_title and not self.title:
            self.title = text
        for block in self.stack:
            block["text"].append(text)


def _score(block: Mapping) -> float:
    text = str(block.get("text") or "")
    words = max(len(text.split()), 1)
    attrs = block.get("attrs") if isinstance(block.get("attrs"), Mapping) else {}
    marker = " ".join((str(attrs.get("class", "")), str(attrs.get("id", "")), str(attrs.get("role", ""))))
    score = float(words)
    tag = str(block.get("tag") or "")
    if tag in {"article", "main", "pre", "code", "table"}:
        score += 30
    if _CONTENT.search(marker):
        score += 20
    if _NAV.search(marker):
        score -= 60
    links = int(block.get("links", 0))
    score -= min(links / words, 0.5) * score
    if tag in {"h1", "h2", "h3", "time"}:
        score += 10
    return score


class StructuralContentCleaner:
    def clean(self, raw_html: str) -> tuple[str, dict[str, str]]:
        parser = _BlockParser()
        parser.feed(raw_html)
        chosen = []
        seen = set()
        for block in parser.blocks:
            text = str(block["text"])
            if text in seen:
                continue
            seen.add(text)
            if _score(block) >= 8 or str(block.get("tag")) in {"pre", "code", "table"}:
                chosen.append(text)
        if not chosen:
            # Conservative fallback: never turn a parseable page into no evidence.
            stripped = re.sub(r"<[^>]+>", " ", raw_html)
            chosen = [re.sub(r"\s+", " ", html.unescape(stripped)).strip()]
        metadata = {
            "title": parser.title,
            "canonical_hint": parser.canonical_hint,
            "author": parser.meta.get("author", "") or parser.meta.get("article:author", ""),
            "published_at": parser.meta.get("article:published_time", "") or parser.meta.get("date", ""),
            "og_url": parser.meta.get("og:url", ""),
        }
        return "\n\n".join(x for x in chosen if x).strip(), metadata
