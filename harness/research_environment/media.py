from __future__ import annotations

import hashlib
import mimetypes
import shutil
import subprocess
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .models import MediaObservationV1
from .policy import ResearchEnvironmentPolicy


class UnsupportedResearchMedia(ValueError):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class MediaResearchProvider:
    def __init__(self, policy: ResearchEnvironmentPolicy, *, allowed_roots: Iterable[Path]):
        self.policy = policy
        self.allowed_roots = tuple(Path(root).resolve() for root in allowed_roots)
        if not self.allowed_roots:
            raise ValueError("at least one allowed media root is required")

    def inspect(self, path: Path) -> MediaObservationV1:
        target = Path(path).resolve()
        self._validate(target)
        digest = _sha256(target)
        ext = target.suffix.casefold()
        media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        metadata = {"extension": ext, "size_bytes": target.stat().st_size}

        if ext in {".png", ".jpg", ".jpeg", ".webp"}:
            metadata.update(self._image_metadata(target))
        elif ext == ".wav":
            metadata.update(self._wav_metadata(target))
        elif ext in {".mp3", ".mp4", ".mkv", ".webm"}:
            metadata.update(self._ffprobe_metadata(target))
        else:
            raise UnsupportedResearchMedia(ext)

        return MediaObservationV1(
            path=str(target),
            artifact_uri=f"artifact://phase20a/{digest}/{target.name}",
            sha256=digest,
            media_type=media_type,
            observed_at=_now(),
            metadata=metadata,
            semantic_text="",
            semantic_method="",
        )

    def transcribe_local(self, path: Path, *, model_size: str = "large-v3-turbo"):
        target = Path(path).resolve()
        self._validate(target)
        try:
            from faster_whisper import WhisperModel
        except Exception as exc:
            raise RuntimeError(
                "faster-whisper missing; install requirements-research-media.txt"
            ) from exc

        model = WhisperModel(model_size)
        segments, info = model.transcribe(str(target))
        text = "\n".join(
            f"[{segment.start:.2f}-{segment.end:.2f}] {segment.text.strip()}"
            for segment in segments
        )
        base = self.inspect(target)
        metadata = dict(base.metadata)
        metadata["transcription_language"] = getattr(info, "language", "")
        metadata["transcription_probability"] = getattr(info, "language_probability", None)
        return MediaObservationV1(
            base.path, base.artifact_uri, base.sha256, base.media_type, _now(),
            metadata, text, f"faster-whisper:{model_size}"
        )

    def _validate(self, path):
        if not path.is_file():
            raise FileNotFoundError(path)
        if not any(path == root or root in path.parents for root in self.allowed_roots):
            raise PermissionError(f"media outside allowed roots: {path}")
        if path.stat().st_size > self.policy.max_file_bytes:
            raise UnsupportedResearchMedia("media exceeds max_file_bytes")

    @staticmethod
    def _image_metadata(path):
        try:
            from PIL import Image
        except Exception as exc:
            raise RuntimeError("Pillow is required for images") from exc
        with Image.open(path) as image:
            return {
                "width": int(image.width),
                "height": int(image.height),
                "mode": str(image.mode),
                "format": str(image.format or ""),
            }

    @staticmethod
    def _wav_metadata(path):
        with wave.open(str(path), "rb") as wav:
            frames = wav.getnframes()
            rate = wav.getframerate()
            return {
                "channels": wav.getnchannels(),
                "sample_width_bytes": wav.getsampwidth(),
                "sample_rate_hz": rate,
                "frames": frames,
                "duration_seconds": frames / rate if rate else 0.0,
            }

    @staticmethod
    def _ffprobe_metadata(path):
        if shutil.which("ffprobe") is None:
            return {"ffprobe": "UNAVAILABLE"}
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries",
                "format=duration,format_name",
                "-of", "default=noprint_wrappers=1:nokey=0",
                str(path),
            ],
            capture_output=True, text=True, timeout=20, check=False,
        )
        return {
            "ffprobe": "AVAILABLE",
            "ffprobe_returncode": result.returncode,
            "ffprobe_output": result.stdout[:4000],
        }
