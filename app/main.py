"""
Phase 3 — Semantic Cache.
POST /ask -> exact-match cache -> semantic cache -> LLM only on miss.
"""
import os
import time

from fastapi import FastAPI
from pydantic import BaseModel

from app.cache.exact_cache import ExactMatchCache
from app.cache.semantic_cache import SemanticCache
from app.embeddings import embed
from app.llm_client import ask_llm

app = FastAPI(title="LLM Cache — Phase 3: Semantic Cache")

SIMILARITY_THRESHOLD = float(os.getenv("SIMILARITY_THRESHOLD", "0.65"))


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
exact_cache = ExactMatchCache()
semantic_cache = SemanticCache(threshold=SIMILARITY_THRESHOLD)


class AskRequest(BaseModel):
    question: str


class AskResponse(BaseModel):
    answer: str
    outcome: str  # "exact_hit" | "semantic_hit" | "miss"
    similarity: float | None = None
    matched_question: str | None = None
    latency_s: float
    cost_usd: float


@app.post("/ask", response_model=AskResponse)
async def ask(req: AskRequest):
    start = time.perf_counter()

    # 1. exact-match fast path — no embedding needed
    exact_answer = exact_cache.get(req.question)
    if exact_answer is not None:
        latency_s = time.perf_counter() - start
        stats.record(request_latency_s=latency_s, outcome="exact_hit")
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
        return AskResponse(
            answer=semantic_result.answer,
            outcome="semantic_hit",
            similarity=round(semantic_result.similarity, 4),
            matched_question=semantic_result.matched_question,
            latency_s=round(latency_s, 3),
            cost_usd=0.0,
        )

    # 3. miss — call the LLM, store in both caches
    llm_result = await ask_llm(req.question)
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
    return AskResponse(
        answer=llm_result.answer,
        outcome="miss",
        similarity=round(semantic_result.similarity, 4) if semantic_result.similarity is not None else None,
        latency_s=round(latency_s, 3),
        cost_usd=llm_result.cost,
    )


@app.get("/stats")
async def get_stats():
    return stats.snapshot()


@app.get("/health")
async def health():
    return {"status": "ok"}
