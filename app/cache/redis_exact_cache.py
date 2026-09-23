"""
Phase 4 — Exact-Match Cache, backed by Redis instead of an in-process dict.
Same normalize-then-lookup behavior as app/cache/exact_cache.py, but the
state now lives in Redis so every app instance shares it.
"""
from redis import Redis

from app.cache.exact_cache import normalize

KEY_PREFIX = "cache:exact:"


class RedisExactMatchCache:
    def __init__(self, redis_client: Redis):
        self._redis = redis_client

    def _key(self, question: str) -> str:
        return f"{KEY_PREFIX}{normalize(question)}"

    def get(self, question: str) -> str | None:
        value = self._redis.get(self._key(question))
        return value.decode("utf-8") if value is not None else None

    def set(self, question: str, answer: str) -> None:
        self._redis.set(self._key(question), answer)
