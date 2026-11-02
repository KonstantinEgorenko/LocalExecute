'Canonical serialization and validation helpers for research campaign research contracts.'
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_GIT_REF_RE = re.compile(r"^[0-9A-Fa-f]{7,64}$")


def text(value: object, name: str, *, max_length: int | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty string")
    if max_length is not None and len(value) > max_length:
        raise ValueError(f"{name} exceeds max_length={max_length}")
    return value


def optional_text(value: object, name: str, *, max_length: int | None = None) -> str | None:
    if value is None:
        return None
    return text(value, name, max_length=max_length)


def safe_id(value: object, name: str) -> str:
    raw = text(value, name)
    if not _ID_RE.fullmatch(raw):
        raise ValueError(f"{name} must match {_ID_RE.pattern}")
    return raw


def optional_safe_id(value: object, name: str) -> str | None:
    if value is None:
        return None
    return safe_id(value, name)


def sha256_hex(value: object, name: str) -> str:
    raw = text(value, name, max_length=64)
    if not _SHA256_RE.fullmatch(raw):
        raise ValueError(f"{name} must be lowercase SHA-256 hex")
    return raw


def optional_sha256_hex(value: object, name: str) -> str | None:
    if value is None:
        return None
    return sha256_hex(value, name)


def git_commit(value: object, name: str) -> str:
    raw = text(value, name, max_length=64)
    if not _GIT_REF_RE.fullmatch(raw):
        raise ValueError(f"{name} must be 7..64 hexadecimal Git object id")
    return raw.lower()


def non_negative_int(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be integer >= 0")
    return value


def positive_int(value: object, name: str) -> int:
    result = non_negative_int(value, name)
    if result == 0:
        raise ValueError(f"{name} must be > 0")
    return result


def non_negative_number(value: object, name: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"{name} must be number >= 0")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{name} must be finite number >= 0")
    return result


def finite_number(value: object, name: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"{name} must be finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite number")
    return result


def unique_text(
    values: object,
    name: str,
    *,
    maximum: int | None = None,
    item_max_length: int = 4_000,
) -> tuple[str, ...]:
    if not isinstance(values, (tuple, list)):
        raise ValueError(f"{name} must be list/tuple")
    result = tuple(text(item, name, max_length=item_max_length) for item in values)
    if len(set(result)) != len(result):
        raise ValueError(f"{name} must not contain duplicates")
    if maximum is not None and len(result) > maximum:
        raise ValueError(f"{name} exceeds maximum={maximum}")
    return result


def unique_ids(values: object, name: str, *, maximum: int | None = None) -> tuple[str, ...]:
    if not isinstance(values, (tuple, list)):
        raise ValueError(f"{name} must be list/tuple")
    result = tuple(safe_id(item, name) for item in values)
    if len(set(result)) != len(result):
        raise ValueError(f"{name} must not contain duplicates")
    if maximum is not None and len(result) > maximum:
        raise ValueError(f"{name} exceeds maximum={maximum}")
    return result


def freeze_json(value: Any, name: str = "value") -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{name} must not contain non-finite float")
        return value
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{name} mapping keys must be strings")
            frozen[key] = freeze_json(item, f"{name}.{key}")
        return MappingProxyType(frozen)
    if isinstance(value, (tuple, list)):
        return tuple(freeze_json(item, name) for item in value)
    raise ValueError(f"{name} must be JSON-safe")


def plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [plain(item) for item in value]
    if isinstance(value, StrEnum):
        return value.value
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return value.to_dict()
    return value


def timestamp(value: object, name: str) -> str:
    raw = text(value, name, max_length=128)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must include timezone")
    return raw


def optional_timestamp(value: object, name: str) -> str | None:
    if value is None:
        return None
    return timestamp(value, name)


def timestamp_value(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def canonical_json(value: object) -> str:
    return json.dumps(plain(value), sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def stable_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
