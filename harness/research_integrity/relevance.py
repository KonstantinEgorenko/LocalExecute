"""Small deterministic BM25-style evidence ranker.

This is clean-room code inspired by the general idea of query-oriented content
filtering; it is not a copy of Crawl4AI implementation.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from typing import Mapping, Sequence

_TOKEN = re.compile(r"[A-Za-z0-9_./:+-]{2,}")


def tokenize(text: str) -> list[str]:
    return [x.casefold() for x in _TOKEN.findall(text)]


class BM25EvidenceSelector:
    def __init__(self, *, k1: float = 1.5, b: float = 0.75) -> None:
        self.k1 = float(k1)
        self.b = float(b)

    def select(self, query: str, evidence: Sequence[Mapping], *, limit: int) -> list[Mapping]:
        docs = list(evidence)
        if len(docs) <= limit:
            return docs
        q = tokenize(query)
        if not q:
            return docs[:limit]
        tokens = [tokenize(str(x.get("excerpt") or x.get("text") or x.get("content") or "")) for x in docs]
        avgdl = sum(len(x) for x in tokens) / max(len(tokens), 1)
        df = Counter()
        for row in tokens:
            df.update(set(row))
        scores: list[tuple[float, int]] = []
        n = len(docs)
        for idx, row in enumerate(tokens):
            tf = Counter(row)
            dl = len(row)
            score = 0.0
            for term in q:
                freq = tf.get(term, 0)
                if not freq:
                    continue
                idf = math.log(1.0 + (n - df[term] + 0.5) / (df[term] + 0.5))
                denom = freq + self.k1 * (1.0 - self.b + self.b * dl / max(avgdl, 1e-9))
                score += idf * (freq * (self.k1 + 1.0)) / denom
            text = " ".join(row)
            if any(term in text for term in q):
                score += 0.05
            scores.append((score, idx))
        ranked = sorted(scores, key=lambda x: (-x[0], x[1]))[:limit]
        selected = {idx for _, idx in ranked}
        # Preserve source/document order for coherent context once relevance is chosen.
        return [item for idx, item in enumerate(docs) if idx in selected]
