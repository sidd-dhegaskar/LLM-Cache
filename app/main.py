"""
Phase 4 — Redis as the Shared Cache.
POST /ask -> exact-match cache -> semantic cache -> LLM only on miss.
Same flow as Phase 3; the caches are now backed by Redis instead of
in-process Python objects, so multiple app instances share cache state.
"""
import logging
import os
import time

from dotenv import load_dotenv

# must run before any app.* import that reads os.getenv() at module load time
# (app.gemini_client reads GEMINI_API_KEY as soon as it's imported)
load_dotenv()

# INFO-level logging is silent by default in Python — this makes the
# logger.info(...) calls in llm_client.py / gemini_client.py actually print.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s: %(message)s")
logger = logging.getLogger("app.main")

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from redis import Redis
import httpx

from app.cache.redis_exact_cache import RedisExactMatchCache
from app.cache.redis_semantic_cache import RedisSemanticCache
from app.embeddings import embed
from app.llm_client import ask_llm

app = FastAPI(title="LLM Cache — Phase 4: Redis Shared Cache")

SIMILARITY_THRESHOLD = float(os.getenv("SIMILARITY_THRESHOLD", "0.65"))
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")


class Stats:
    def __init__(self):
        self.request_count = 0
        self.llm_call_count = 0
        self.exact_hits = 0
        self.semantic_hits = 0
        self.cache_misses = 0
        self.total_llm_latency_s = 0.0
        self.total_embedding_latency_s = 0.0
        self.total_request_latency_s = 0.0
        self.total_cost = 0.0

    def record(
        self,
        request_latency_s: float,
        outcome: str,  # "exact_hit" | "semantic_hit" | "miss"
        embedding_latency_s: float = 0.0,
        llm_latency_s: float = 0.0,
        cost: float = 0.0,
    ):
        self.request_count += 1
        self.total_request_latency_s += request_latency_s
        self.total_embedding_latency_s += embedding_latency_s

        if outcome == "exact_hit":
            self.exact_hits += 1
        elif outcome == "semantic_hit":
            self.semantic_hits += 1
        else:
            self.cache_misses += 1
            self.llm_call_count += 1
            self.total_llm_latency_s += llm_latency_s
            self.total_cost += cost

    def snapshot(self) -> dict:
        cache_hits = self.exact_hits + self.semantic_hits
        avg_llm_latency = self.total_llm_latency_s / self.llm_call_count if self.llm_call_count else 0.0
        avg_request_latency = (
            self.total_request_latency_s / self.request_count if self.request_count else 0.0
        )
        avg_embedding_latency = (
            self.total_embedding_latency_s / self.request_count if self.request_count else 0.0
        )
        hit_rate = cache_hits / self.request_count if self.request_count else 0.0
        return {
            "request_count": self.request_count,
            "llm_call_count": self.llm_call_count,
            "exact_hits": self.exact_hits,
            "semantic_hits": self.semantic_hits,
            "cache_misses": self.cache_misses,
            "hit_rate": round(hit_rate, 4),
            "avg_llm_latency_s": round(avg_llm_latency, 3),
            "avg_embedding_latency_s": round(avg_embedding_latency, 4),
            "avg_request_latency_s": round(avg_request_latency, 3),
            "total_cost_usd": round(self.total_cost, 4),
        }


stats = Stats()
# one shared Redis connection, handed to both caches — see NOTES.md "Phase 4"
redis_client = Redis.from_url(REDIS_URL)
exact_cache = RedisExactMatchCache(redis_client)
semantic_cache = RedisSemanticCache(redis_client, threshold=SIMILARITY_THRESHOLD)


class AskRequest(BaseModel):
    question: str


class AskResponse(BaseModel):
    answer: str
    outcome: str  # "exact_hit" | "semantic_hit" | "miss"
    similarity: float | None = None
    matched_question: str | None = None
    latency_s: float
    cost_usd: float
    prompt_tokens: int = 0
    output_tokens: int = 0


@app.post("/ask", response_model=AskResponse)
async def ask(req: AskRequest):
    start = time.perf_counter()
    logger.info("ask request: question=%r", req.question)

    # 1. exact-match fast path — no embedding needed
    exact_answer = exact_cache.get(req.question)
    if exact_answer is not None:
        latency_s = time.perf_counter() - start
        stats.record(request_latency_s=latency_s, outcome="exact_hit")
        logger.info("ask response: outcome=exact_hit latency=%.3fs", latency_s)
        return AskResponse(answer=exact_answer, outcome="exact_hit", latency_s=round(latency_s, 3), cost_usd=0.0)

    # 2. semantic path — embed once, reuse for lookup and (on miss) storage
    embedding_result = embed(req.question)
    query_vector = embedding_result.vector

    semantic_result = semantic_cache.lookup(query_vector)
    if semantic_result.hit:
        exact_cache.set(req.question, semantic_result.answer)  # speed up exact repeats of this phrasing
        latency_s = time.perf_counter() - start
        stats.record(
            request_latency_s=latency_s,
            outcome="semantic_hit",
            embedding_latency_s=embedding_result.latency_s,
        )
        logger.info(
            "ask response: outcome=semantic_hit similarity=%.4f latency=%.3fs",
            semantic_result.similarity, latency_s,
        )
        return AskResponse(
            answer=semantic_result.answer,
            outcome="semantic_hit",
            similarity=round(semantic_result.similarity, 4),
            matched_question=semantic_result.matched_question,
            latency_s=round(latency_s, 3),
            cost_usd=0.0,
        )

    # 3. miss — call the LLM, store in both caches
    try:
        llm_result = await ask_llm(req.question)
    except httpx.HTTPError as e:
        # LLM provider timed out / errored / was unreachable — surface a
        # clean 502 instead of letting the raw exception crash the request.
        logger.exception("ask failed: LLM request error")
        raise HTTPException(status_code=502, detail=f"LLM request failed: {e}") from e

    exact_cache.set(req.question, llm_result.answer)
    semantic_cache.add(req.question, llm_result.answer, query_vector)

    latency_s = time.perf_counter() - start
    stats.record(
        request_latency_s=latency_s,
        outcome="miss",
        embedding_latency_s=embedding_result.latency_s,
        llm_latency_s=llm_result.latency_s,
        cost=llm_result.cost,
    )
    logger.info(
        "ask response: outcome=miss latency=%.3fs cost=$%.6f prompt_tokens=%d output_tokens=%d",
        latency_s, llm_result.cost, llm_result.prompt_tokens, llm_result.output_tokens,
    )
    return AskResponse(
        answer=llm_result.answer,
        outcome="miss",
        similarity=round(semantic_result.similarity, 4) if semantic_result.similarity is not None else None,
        latency_s=round(latency_s, 3),
        cost_usd=llm_result.cost,
        prompt_tokens=llm_result.prompt_tokens,
        output_tokens=llm_result.output_tokens,
    )


@app.get("/stats")
async def get_stats():
    return stats.snapshot()


@app.get("/health")
async def health():
    return {"status": "ok"}
