"""
Phase 3 — Semantic Cache.
Embed question -> brute-force cosine similarity against stored embeddings ->
nearest question -> threshold check -> hit/miss. Redis vector search
replaces the brute-force scan in Phase 4; this in-memory version is fine at
small scale and makes the core algorithm easy to see.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np


@dataclass
class CacheEntry:
    question: str
    answer: str
    embedding: np.ndarray
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class SemanticLookupResult:
    hit: bool
    answer: str | None
    similarity: float | None
    matched_question: str | None


class SemanticCache:
    def __init__(self, threshold: float = 0.85):
        self.threshold = threshold
        self._entries: list[CacheEntry] = []
        self._matrix: np.ndarray | None = None  # (n_entries, dim), rows are unit vectors

    def _rebuild_matrix(self):
        if self._entries:
            self._matrix = np.stack([e.embedding for e in self._entries])
        else:
            self._matrix = None

    def lookup(self, embedding: np.ndarray) -> SemanticLookupResult:
        if self._matrix is None:
            return SemanticLookupResult(hit=False, answer=None, similarity=None, matched_question=None)

        # embeddings are pre-normalized, so cosine similarity == dot product
        similarities = self._matrix @ embedding
        best_idx = int(np.argmax(similarities))
        best_score = float(similarities[best_idx])

        if best_score >= self.threshold:
            entry = self._entries[best_idx]
            return SemanticLookupResult(
                hit=True, answer=entry.answer, similarity=best_score, matched_question=entry.question,
            )

        return SemanticLookupResult(hit=False, answer=None, similarity=best_score, matched_question=None)

    def add(self, question: str, answer: str, embedding: np.ndarray) -> None:
        self._entries.append(CacheEntry(question=question, answer=answer, embedding=embedding))
        self._rebuild_matrix()

    def __len__(self) -> int:
        return len(self._entries)
