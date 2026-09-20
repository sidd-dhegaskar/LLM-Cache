# Production-Grade Semantic LLM Cache

A backend engineering project that builds a semantic caching layer for an LLM-backed FAQ/customer-support API — starting from the dumbest possible implementation and evolving it, phase by phase, into a distributed, observable, fault-tolerant system.

Each phase exists because the previous one broke in a specific, demonstrable way. The goal isn't to follow a tutorial — it's to hit a real problem (unnecessary LLM cost, cache stampede, a dead Redis node) and then build the concept that fixes it.

## Why this project

Semantic caching is the perfect vehicle for learning backend systems because a small, understandable domain (FAQ Q&A) forces you through most of the hard problems in distributed backend engineering: caching, concurrency, distributed state, coordination, failure handling, and observability — in that order, each motivated by the last.

## Tech stack (proposed)

- **API**: FastAPI (Python)
- **LLM**: any hosted LLM API (configurable via env var)
- **Embeddings**: sentence-transformers (local) or an embedding API
- **Cache/Vector store**: Redis Stack (RediSearch + vector similarity), Redis for locks/KV
- **Load testing**: k6
- **Observability**: Prometheus + Grafana
- **Containerization**: Docker Compose (for multi-app-server + Redis HA setups)

## Project structure (grows over phases)

```
LLM-Cache/
├── app/
│   ├── main.py              # FastAPI app
│   ├── llm_client.py        # LLM API wrapper + cost/latency tracking
│   ├── cache/
│   │   ├── exact_cache.py   # Phase 2
│   │   ├── semantic_cache.py# Phase 3+
│   │   └── redis_cache.py   # Phase 4+
│   ├── locking.py           # Phase 7
│   └── metrics.py           # Phase 11
├── loadtest/                 # k6 scripts (Phase 5)
├── eval/                     # semantic cache evaluation dataset (Phase 3)
├── docker-compose.yml         # multi-app-server + Redis (Phase 4+)
├── .env.example
└── README.md
```

## Phase progression

### Phase 1 — Baseline LLM API
Dumbest possible version: `POST /ask` → LLM → answer. No caching at all.

- Track: request count, LLM call count, LLM latency, estimated cost.
- **Milestone**: 100 requests → 100 LLM calls → known $cost → ~2-3s avg latency.

### Phase 2 — Exact-Match Cache
In-memory `dict[question] = answer` in front of the LLM call.

- Track: cache hits/misses, hit rate, LLM calls, latency, cost.
- **Milestone**: demonstrate ~12% hit rate on paraphrased questions — exact match fails on "How do I get a refund?" vs "What is your refund policy?".

### Phase 3 — Semantic Cache
Replace exact lookup with: embed question → vector similarity search → nearest question → threshold check → hit/miss.

- Store `{embedding, question, answer, created_at}`.
- Experiment with similarity thresholds: 0.80 / 0.85 / 0.90 / 0.92 / 0.95.
- Build a small eval dataset of known semantic equivalents; measure hit rate, false-hit rate, false-miss rate, embedding latency.
- **Milestone**: ~70% hit rate vs. 12% for exact match (illustrative — measure your own numbers).

### Phase 4 — Redis as the Shared Cache
Run 3 app server instances, each with its own local semantic cache — show that App 2 doesn't benefit from App 1's cached answer. Move the vector cache into Redis so all app servers share state.

- Learn: Redis, shared state, vector search, connection pooling, serialization, horizontal scaling.

### Phase 5 — Load Testing
Use k6 to generate 10 / 50 / 100 / 500 RPS.

- Measure p50/p95/p99 latency, cache hit rate, LLM req/s, Redis req/s, error rate.
- Build a before/after table (LLM calls/sec, p95 latency, hit rate, cost/min).

### Phase 6 — Cache Stampede
Send 100 simultaneous identical requests against a cold cache. Observe all 100 miss and hit the LLM — the cache provides zero protection under concurrent cold-start load.

### Phase 7 — Request Coalescing (Single-Flight Locking)
Introduce a per-key Redis lock (`SET lock:<hash> <id> NX EX 10`). First requester becomes leader and calls the LLM; the rest wait and read the result once populated.

- Learn: distributed locks, TTLs, lock ownership, race conditions, lock expiry.
- **Milestone**: 100 simultaneous identical requests → 1 LLM call.

### Phase 8 — Failure Handling (Fail-Open)
Kill Redis. Decide and implement behavior: treat cache-unavailable as a miss and fall through to the LLM rather than failing the request.

- Track `cache_available` and compare normal-cost vs. outage-cost explicitly.

### Phase 9 — Redis Replication / Failover
Add a Redis primary + replica (Sentinel or equivalent HA setup). App talks to the Redis service, not a hardcoded node.

- Test: populate cache → generate traffic → kill primary → observe failover → measure hit rate through the transition.

### Phase 10 — Production Hardening
- **TTLs** on cached answers (experiment: 1h / 6h / 24h / 7d).
- **Unsafe-match prevention**: semantically similar ≠ safe to reuse (e.g. "refund policy" vs. "why was MY refund rejected"). Add metadata (tenant, category, language, model, prompt_version) and require exact match on these dimensions in addition to similarity threshold.

### Phase 11 — Observability
Expose Prometheus metrics: `cache_hit_total`, `cache_miss_total`, `semantic_similarity_distribution`, `llm_requests_total`, `llm_errors_total`, `request_latency`, `embedding_latency`, `redis_latency`, `lock_acquired_total`, `lock_wait_total`, `cache_eviction_total`. Visualize in Grafana.

### Phase 12 — Chaos Testing
- Kill Redis → confirm app continues, LLM traffic rises.
- Kill Redis primary → confirm replica promotion + reconnect.
- 1000 identical concurrent requests → confirm ~1 LLM call.
- Scale to 3 app servers → confirm shared cache benefits all.
- 10x traffic → observe Redis/LLM/latency/CPU/memory/cost.

## What each phase teaches

| Phase | Core concept |
|---|---|
| 1 | HTTP APIs, latency measurement, basic observability |
| 2 | Caching fundamentals, hit rate |
| 3 | Embeddings, vector similarity, precision/recall tradeoffs |
| 4 | Shared state, horizontal scaling, why local state doesn't scale |
| 5 | Load testing, percentile latency, bottleneck identification |
| 6 | Concurrency failure modes (stampede) |
| 7 | Distributed locking, single-flight coordination |
| 8 | Graceful degradation, fail-open design |
| 9 | Replication, failover, high availability |
| 10 | Cache correctness beyond similarity (TTL, tenancy, category) |
| 11 | Metrics, dashboards, production observability |
| 12 | Chaos engineering, resilience validation |

## Running the project (fill in as phases are built)

Each phase should be independently demonstrable — a phase isn't "done" until its milestone can be shown with real measured numbers, not just working code.

```bash
# Phase 1+
cp .env.example .env   # set LLM API key
docker compose up -d   # once Redis is introduced (Phase 4+)
uvicorn app.main:app --reload
```

## Status

- [ ] Phase 1 — Baseline LLM API
- [ ] Phase 2 — Exact-Match Cache
- [ ] Phase 3 — Semantic Cache
- [ ] Phase 4 — Redis as the Shared Cache
- [ ] Phase 5 — Load Testing
- [ ] Phase 6 — Cache Stampede
- [ ] Phase 7 — Request Coalescing
- [ ] Phase 8 — Failure Handling
- [ ] Phase 9 — Redis Replication / Failover
- [ ] Phase 10 — Production Hardening
- [ ] Phase 11 — Observability
- [ ] Phase 12 — Chaos Testing
