from __future__ import annotations

import hashlib
import html
import mimetypes
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable

from .models import ExtractedFragment, FileExtractionV1
from .policy import ResearchEnvironmentPolicy


class UnsupportedResearchFile(ValueError):
    pass


class MissingOptionalDependency(RuntimeError):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class _HTMLText(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag.casefold() in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag.casefold() in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)

    def text(self):
        raw = html.unescape("".join(self.parts))
        lines = [re.sub(r"\s+", " ", line).strip() for line in raw.splitlines()]
        return "\n".join(line for line in lines if line)


class FileResearchProvider:
    def __init__(self, policy: ResearchEnvironmentPolicy, *, allowed_roots: Iterable[Path]):
        self.policy = policy
        self.allowed_roots = tuple(Path(root).resolve() for root in allowed_roots)
        if not self.allowed_roots:
            raise ValueError("at least one allowed file root is required")

    def extract(self, path: Path) -> FileExtractionV1:
        target = Path(path).resolve()
        self._validate_path(target)
        ext = target.suffix.casefold()
        if ext not in self.policy.supported_extensions:
            raise UnsupportedResearchFile(f"extension not supported: {ext}")
        size = target.stat().st_size
        if size > self.policy.max_file_bytes:
            raise UnsupportedResearchFile(f"file exceeds max_file_bytes: {size}")

        digest = _sha256(target)
        media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        artifact_uri = f"artifact://phase20a/{digest}/{target.name}"

        if ext in {".txt", ".md", ".json", ".csv"}:
            fragments = self._fragment(target.read_text(encoding="utf-8", errors="replace"), "file")
        elif ext in {".html", ".htm"}:
            parser = _HTMLText()
            parser.feed(target.read_text(encoding="utf-8", errors="replace"))
            fragments = self._fragment(parser.text(), "html")
        elif ext == ".pdf":
            fragments = self._extract_pdf(target)
        elif ext == ".docx":
            fragments = self._extract_docx(target)
        elif ext == ".pptx":
            fragments = self._extract_pptx(target)
        elif ext == ".xlsx":
            fragments = self._extract_xlsx(target)
        else:
            raise UnsupportedResearchFile(f"use MediaResearchProvider for {ext}")

        return FileExtractionV1(
            str(target), artifact_uri, digest, media_type, size, _now(),
            tuple(self._limit(fragments)), {"extension": ext}
        )

    def _validate_path(self, path):
        if not path.is_file():
            raise FileNotFoundError(path)
        if not any(path == root or root in path.parents for root in self.allowed_roots):
            raise PermissionError(f"file outside allowed roots: {path}")

    def _fragment(self, text, locator, metadata=None):
        text = str(text or "").strip()
        out = []
        for start in range(0, len(text), self.policy.max_fragment_chars):
            piece = text[start:start+self.policy.max_fragment_chars].strip()
            if not piece:
                continue
            n = start // self.policy.max_fragment_chars
            fid = hashlib.sha256(f"{locator}:{n}:{piece}".encode()).hexdigest()[:20]
            out.append(ExtractedFragment(
                f"frag20a:{fid}", piece,
                locator if n == 0 else f"{locator}#chunk={n+1}",
                metadata or {}
            ))
        return out

    def _extract_pdf(self, path):
        try:
            from pypdf import PdfReader
        except Exception as exc:
            raise MissingOptionalDependency("pypdf is required for PDF") from exc
        out = []
        for index, page in enumerate(PdfReader(str(path)).pages, 1):
            out.extend(self._fragment(page.extract_text() or "", f"page:{index}", {"page": index}))
        return out

    def _extract_docx(self, path):
        try:
            from docx import Document
        except Exception as exc:
            raise MissingOptionalDependency("python-docx is required for DOCX") from exc
        document = Document(str(path))
        out = []
        for index, p in enumerate(document.paragraphs, 1):
            out.extend(self._fragment(p.text, f"paragraph:{index}", {"paragraph": index}))
        for index, table in enumerate(document.tables, 1):
            rows = [" | ".join(cell.text.strip() for cell in row.cells) for row in table.rows]
            out.extend(self._fragment("\n".join(rows), f"table:{index}", {"table": index}))
        return out

    def _extract_pptx(self, path):
        try:
            from pptx import Presentation
        except Exception as exc:
            raise MissingOptionalDependency("python-pptx is required for PPTX") from exc
        out = []
        for index, slide in enumerate(Presentation(str(path)).slides, 1):
            texts = [getattr(shape, "text", "") for shape in slide.shapes if getattr(shape, "text", "")]
            out.extend(self._fragment("\n".join(texts), f"slide:{index}", {"slide": index}))
        return out

    def _extract_xlsx(self, path):
        try:
            from openpyxl import load_workbook
        except Exception as exc:
            raise MissingOptionalDependency("openpyxl is required for XLSX") from exc
        book = load_workbook(str(path), read_only=True, data_only=True)
        out = []
        try:
            for sheet in book.worksheets:
                rows = []
                for row in sheet.iter_rows(values_only=True):
                    values = ["" if value is None else str(value) for value in row]
                    if any(values):
                        rows.append(" | ".join(values))
                out.extend(self._fragment("\n".join(rows), f"sheet:{sheet.title}", {"sheet": sheet.title}))
        finally:
            book.close()
        return out

    def _limit(self, fragments):
        remaining = self.policy.max_total_chars
        for item in fragments:
            if remaining <= 0:
                break
            text = item.text[:remaining]
            if text:
                yield ExtractedFragment(item.fragment_id, text, item.locator, item.metadata)
                remaining -= len(text)
