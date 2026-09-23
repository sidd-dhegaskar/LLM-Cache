"""
Phase 4 — Semantic Cache, backed by Redis + RediSearch vector search.
Replaces the in-process numpy brute-force scan (app/cache/semantic_cache.py)
with a Redis-side vector index, so the cache is shared across app instances
and the nearest-neighbor search runs inside Redis instead of in our process.
"""
from dataclasses import dataclass

import numpy as np
from redis import Redis
from redis.commands.search.field import NumericField, TextField, VectorField
from redis.commands.search.indexDefinition import IndexDefinition, IndexType
from redis.commands.search.query import Query

INDEX_NAME = "idx:semantic_cache"
KEY_PREFIX = "cache:vec:"
EMBEDDING_DIM = 384  # fixed output size of all-MiniLM-L6-v2 — every embedding is this many floats


@dataclass
class SemanticLookupResult:
    hit: bool
    answer: str | None
    similarity: float | None
    matched_question: str | None


def _vector_to_bytes(vector: np.ndarray) -> bytes:
    # RediSearch stores/compares vectors as raw float32 bytes, not JSON or
    # text — this packs our numpy vector into that exact binary layout.
    return vector.astype(np.float32).tobytes()


class RedisSemanticCache:
    def __init__(self, redis_client: Redis, threshold: float = 0.65):
        self._redis = redis_client
        self.threshold = threshold
        self._ensure_index()

    def _ensure_index(self) -> None:
        # FT.CREATE is one-time setup: "watch every key under this prefix,
        # and treat its `embedding` field as a searchable vector." If the
        # index already exists (e.g. app restarted), Redis raises an error
        # on re-creation — we swallow only that specific error.
        try:
            self._redis.ft(INDEX_NAME).create_index(
                fields=[
                    TextField("question"),
                    TextField("answer"),
                    NumericField("created_at"),
                    VectorField(
                        "embedding",
                        "FLAT",  # brute-force, exact search — matches our numpy version's behavior
                        {
                            "TYPE": "FLOAT32",
                            "DIM": EMBEDDING_DIM,
                            "DISTANCE_METRIC": "COSINE",
                        },
                    ),
                ],
                definition=IndexDefinition(prefix=[KEY_PREFIX], index_type=IndexType.HASH),
            )
        except Exception as e:
            if "Index already exists" not in str(e):
                raise

    def lookup(self, embedding: np.ndarray) -> SemanticLookupResult:
        # KNN query: "find the 1 nearest neighbor to $vec, ranked by the
        # index's distance metric (cosine)." AS vector_score names the
        # resulting distance in the returned fields.
        query = (
            Query("*=>[KNN 1 @embedding $vec AS vector_score]")
            .sort_by("vector_score")
            .return_fields("question", "answer", "vector_score")
            .dialect(2)
        )
        params = {"vec": _vector_to_bytes(embedding)}

        results = self._redis.ft(INDEX_NAME).search(query, query_params=params)

        if not results.docs:
            # empty index (nothing cached yet) — trivially a miss
            return SemanticLookupResult(hit=False, answer=None, similarity=None, matched_question=None)

        best = results.docs[0]
        # RediSearch reports COSINE as a *distance* (0 = identical, larger =
        # more different), the inverse of the *similarity* score (1 =
        # identical) our Phase 3 threshold logic expects — flip it back.
        distance = float(best.vector_score)
        similarity = 1 - distance

        if similarity >= self.threshold:
            return SemanticLookupResult(
                hit=True, answer=best.answer, similarity=similarity, matched_question=best.question,
            )

        return SemanticLookupResult(hit=False, answer=None, similarity=similarity, matched_question=None)

    def add(self, question: str, answer: str, embedding: np.ndarray) -> None:
        # Each cache entry is one Redis hash. Writing it under a key matching
        # KEY_PREFIX is what makes RediSearch pick it up into the index
        # automatically — no separate "index this" call needed.
        #
        # INCR is an atomic counter kept in Redis itself (not in this
        # process), so every app instance draws from the same sequence —
        # a Python-side counter would reset per process and let two
        # instances both generate id=1, overwriting each other's entry.
        entry_id = self._redis.incr("cache:vec:next_id")
        key = f"{KEY_PREFIX}{entry_id}"
        self._redis.hset(
            key,
            mapping={
                "question": question,
                "answer": answer,
                "embedding": _vector_to_bytes(embedding),
                "created_at": 0,  # placeholder; wired up to a real timestamp later if needed
            },
        )
