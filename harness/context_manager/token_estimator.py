'Deterministic model-agnostic token estimation for context budgeting.\n\ncontext management deliberately avoids importing model tokenizers. The estimator is a\nconservative, reproducible UTF-8 byte heuristic and can be replaced later by a\nmodel-aware estimator without changing ContextBuilder semantics.\n'
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Utf8TokenEstimator:
    bytes_per_token: float = 4.0

    def __post_init__(self) -> None:
        if (
            not isinstance(self.bytes_per_token, (int, float))
            or isinstance(self.bytes_per_token, bool)
            or not math.isfinite(float(self.bytes_per_token))
            or self.bytes_per_token <= 0
        ):
            raise ValueError("bytes_per_token must be a finite positive number")

    def __call__(self, content: str) -> int:
        if not isinstance(content, str):
            raise ValueError("content must be a string")
        byte_count = len(content.encode("utf-8"))
        if byte_count == 0:
            return 0
        return max(1, math.ceil(byte_count / float(self.bytes_per_token)))
