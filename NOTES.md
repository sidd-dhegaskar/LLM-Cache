# Project Notes

Running log of what we've built, phase by phase, and the design decisions behind it.

## Phase 1 — Baseline LLM API

`POST /ask` with no caching: every request calls the LLM and returns the answer.

**No real API key**: instead of a real LLM provider, `app/mock_llm.py` is a standalone FastAPI
service that stands in for one. It adds artificial latency (base + random jitter) to simulate
real response times, and returns a deterministic answer per question. `app/llm_client.py` is
the wrapper the main app calls — over HTTP (`MOCK_LLM_URL`), like a real provider — keeping the
shape production-realistic and setting up Phase 4+ (multiple services) naturally.

**Tracked from the start**: request count, LLM call count, average LLM latency, total estimated
cost — exposed via `GET /stats`.

**Verified**: N requests -> N LLM calls -> cost and latency tracked correctly.

## Phase 2 — Exact-Match Cache

`app/cache/exact_cache.py`: an in-memory `dict[normalized_question] = answer` checked before
calling the LLM.

**Verified behavior**:
- Exact repeat of a question -> cache hit, 0 cost, near-0 latency.
- Paraphrase of the same question -> still a miss, still calls the LLM. Exact string matching
  can't recognize that two different phrasings mean the same thing — the motivation for Phase 3.

## Phase 3 — Semantic Cache

**Layering**: semantic search sits behind the exact-match cache rather than replacing it.
Exact-match is a cheap O(1) check that catches literal repeats without paying embedding cost;
only on an exact-match miss do we embed the question and do a similarity search.

**Embeddings**: `sentence-transformers` (`all-MiniLM-L6-v2`) running locally — no embedding API
key needed. `app/embeddings.py` wraps it; `normalize_embeddings=True` produces unit vectors so
cosine similarity reduces to a plain dot product (`matrix @ vector`).

**Semantic cache** (`app/cache/semantic_cache.py`): stores `{question, answer, embedding,
created_at}` per entry. On lookup, stacks all stored embeddings into one matrix and computes
similarity against the query in a single vectorized numpy operation. On a miss, the query
embedding computed for the lookup is reused when storing the new entry.

### Calibrating the similarity threshold

The README's suggested thresholds (0.80-0.95) were tuned for embedding providers that tend to
cluster higher; `all-MiniLM-L6-v2` produces meaningfully lower cosine similarities for genuine
paraphrases. Testing at 0.80-0.95 gave near-zero hit rates.

**Approach**: `eval/inspect_similarities.py` prints raw similarity scores for known-paraphrase
pairs (within-cluster, should be high) and known-unrelated pairs (across-cluster, should be
low), so the threshold is picked from the model's actual score distribution instead of assumed.

The eval dataset (`eval/paraphrase_clusters.json`) matters here — it needs genuinely equivalent
phrasings per cluster (same underlying question, different wording), not just topically related
questions, or within-cluster and across-cluster scores overlap and no threshold cleanly
separates them.

**Result at threshold = 0.65** (`eval/run_eval.py`):
- hit rate: 93.3%
- false-hit rate: 0% (across the full 0.55-0.80 range tested — never once returned a wrong
  cached answer)
- false-miss rate: 6.7%

Beats the README's placeholder milestone (~70% hit rate). Config: `SIMILARITY_THRESHOLD` env
var, default `0.65` (`.env.example`).

### Mock LLM keyword routing

`app/mock_llm.py` originally picked a canned answer by hashing the question text — deterministic
(needed for believable caching behavior) but topically arbitrary, so a refund question could get
a password-reset answer even though the cache logic itself was working correctly. Added
`KEYWORD_ROUTES` to match question text against topic keywords (refund, password, shipping,
support, subscription) before falling back to hash-based selection, so manual testing reads
sensibly.

## Redis

### What it is, and why "in-memory" matters

Redis is a database that keeps its data in RAM rather than on disk. A normal database like
Postgres writes to disk on every change, and disk I/O — even on a fast SSD — is orders of
magnitude slower than reading/writing RAM. Redis trades some durability for that speed: if the
process is killed with no persistence configured, whatever was in memory is gone. Redis does
offer optional persistence (periodic snapshots to disk, or an append-only log of every write) so
it can survive a restart, but the reason to reach for Redis at all is the speed, not the
durability — for a cache, that tradeoff is fine, because a cache is allowed to lose data (worst
case, you fall through to the LLM again, which is exactly the "fail open" behavior Phase 8
builds on purpose).

At its core, Redis is a key-value store: `SET key value`, `GET key`. But "value" isn't limited to
a single string — Redis has several built-in data structures, each with its own commands:
- **String**: the basic case, one value per key.
- **Hash**: a key maps to a set of field-value pairs, like a small dict nested under one key
  (`HSET user:1 name "Sid" age "5"`, `HGET user:1 name`). This is the structure we use for cache
  entries — one hash per cached Q&A, with fields `question`, `answer`, `embedding`, `created_at`.
- **List, Set, Sorted Set**: ordered/unordered collections, not used in this project yet but
  relevant later (Phase 7's locking uses plain keys with expiry, not these).

### Why a second Python process can't just "see" the first one's cache

Right now, `ExactMatchCache` and `SemanticCache` are Python objects living inside one running
`uvicorn` process's memory — specifically, inside variables (`exact_cache`, `semantic_cache`)
that exist for as long as that process is alive. If you start a second instance of the app (a
second `uvicorn` process, maybe even on a different machine), Python gives that process its own
completely separate memory space. There is no shared RAM between two OS processes by default —
that's an operating system-level isolation guarantee, not something our code controls. So instance
B's `exact_cache` dict starts empty and stays completely independent of instance A's, no matter
how many requests A answers.

Redis solves this by moving the cache out of any single app process and into its own separate
process (running in a Docker container in our case), reachable over the network via a TCP
connection. Every app instance — no matter how many you run — connects to that *same* Redis
process and issues commands to it. The data lives in Redis's memory, not any app's memory, so all
instances see the same state. This is the general pattern behind "shared state" in distributed
systems: state that needs to be shared can't live inside any one worker process; it has to live
in a separate service all workers talk to.

### Why plain Redis can't do what our semantic cache needs

Every Redis command so far (`GET`, `SET`, `HSET`, `HGET`) operates on data **you already know the
key for** — you ask for `cache:refund_question` and Redis hands back exactly that. That's an
O(1) lookup: fast, but only useful if you already know which key holds what you want.

Our semantic cache doesn't work that way. Given a new question's embedding (a list of ~384
floating point numbers), we don't know in advance *which* stored key is the closest match — we
have to compare the new embedding against every stored embedding and find the best one. Plain
Redis has no built-in operation for "compare this value against every value under a pattern and
rank by similarity." That capability comes from an optional add-on module called **RediSearch**,
which layers indexing and search on top of base Redis — including full-text search, numeric range
queries, and, relevantly, a **VECTOR** field type with similarity search.

"Redis Stack" is simply the official Docker image that ships Redis with RediSearch (and a couple
of other modules) pre-installed and enabled, so you get the extra commands (`FT.CREATE`,
`FT.SEARCH`, etc.) without manually compiling and loading the module into a vanilla Redis build.

### The filing cabinet analogy for indexing

Think of Redis as a giant filing cabinet full of labeled folders (keys). By default, Redis
doesn't know or care what's *inside* a folder — it just hands you the folder when you ask for its
label. If you wanted to ask "which folder's contents are most similar to this new piece of
paper," Redis with no index would have to physically open every single folder, compare it, and
report back — technically possible, but it means the "compare against everything" work happens
one at a time, in order, with no shortcuts.

An **index** is a standing instruction you give Redis once: "for every folder whose label matches
this pattern, keep a special lookup structure organized specifically for finding 'closest
matches' quickly — watch this particular field inside each folder (`embedding`), treat it as a
list of exactly N numbers, and use this specific method for comparing closeness." You say this
once via a command called `FT.CREATE`. From that point on:
- Any folder you add later that matches the pattern gets automatically slotted into that lookup
  structure — you never issue a second command telling Redis "hey, index this new one too," it
  happens as a side effect of writing the hash.
- Any "find closest matches" query goes through that lookup structure instead of manually
  scanning every folder, which is what makes it fast even as the number of folders grows into the
  thousands or millions.

### What `FT.CREATE` actually specifies

Concretely, the index definition needs to answer several questions:
1. **Which keys does this index cover?** Usually a key-name prefix, e.g. all keys starting with
   `cache:`. Redis watches writes to any key matching that prefix.
2. **Which field holds the vector, and how many numbers is it?** Our embeddings are 384-dimensional
   (that's `all-MiniLM-L6-v2`'s fixed output size — every sentence, regardless of length, becomes
   exactly 384 numbers). The index needs to know this dimension count up front.
3. **What similarity metric?** We use cosine similarity — the same metric our brute-force numpy
   version already uses (`matrix @ vector`, since our vectors are pre-normalized to length 1).
   RediSearch supports cosine, Euclidean (L2), and dot product; we tell it cosine so behavior
   matches what we already validated in Phase 3's eval.
4. **What indexing algorithm?** `FLAT` (brute-force, exact, compares against everything — fine at
   our scale, and matches our current numpy approach exactly) or `HNSW` (Hierarchical Navigable
   Small World graph — an approximate method that trades a small chance of missing the true best
   match for much faster search at large scale, by pre-building a graph where similar vectors are
   linked so search can "hop" toward good answers instead of checking everything).

### What a query looks like conceptually

Once the index exists, a lookup is a single `FT.SEARCH` command carrying the query embedding and
a "K" (how many nearest neighbors to return — we only need the single best match, so K=1).
Internally, Redis uses the index to compute the nearest neighbor(s) and returns them along with
their similarity scores, without your application code ever pulling every stored vector back over
the network to compare in Python. That's the practical performance win over our current
`SemanticCache`: our numpy version has to hold every embedding in the app's own memory and
loop/matrix-multiply through all of them on every request; Redis does the equivalent search
server-side, keeps the data in one shared place, and lets any number of app instances query it
without duplicating the whole embedding set in each instance's memory.

### Mapping this onto what we've already built

The two cache classes we wrote in Phase 2/3 become thin Redis-backed replacements with the same
external interface, so `main.py`'s `/ask` flow (exact-match check, then semantic check, then LLM
call, layered the same way) doesn't need to change conceptually — only what's underneath each
`.get()`/`.lookup()`/`.set()`/`.add()` call does:

- **`ExactMatchCache`** (a Python `dict`) → a plain Redis string per normalized question:
  `SET cache:exact:<normalized question> <answer>`, looked up with `GET`. No RediSearch needed
  for this half — it's just key-value.
- **`SemanticCache`** (a Python list of entries + a numpy matrix) → one Redis hash per entry under
  the indexed key prefix (`HSET cache:vec:<id> question ... answer ... embedding <packed bytes>
  created_at ...`), with the one-time `FT.CREATE` index describing the `embedding` field as
  described above, and lookups done via `FT.SEARCH` with a KNN clause instead of our own
  `matrix @ vector` dot product.

The embeddings themselves, the threshold (0.65), and the layering order (exact-match first, then
semantic) all carry over unchanged from Phase 3 — Phase 4 only relocates *where* the cache state
lives, not how caching decisions are made.

## Phase 4 — Redis as the Shared Cache

### The problem being fixed

`ExactMatchCache` and `SemanticCache` are Python objects living inside one `uvicorn` process's
memory. Run a second instance of the app and it gets its own separate memory — there is no shared
RAM between two OS processes by default. So instance B's cache starts empty and stays independent
of instance A's, no matter how many questions A has already answered and cached. Redis fixes this
by moving the cache out of any single app process into its own separate process that every app
instance connects to over the network — the data lives in Redis's memory, not any app's memory.

### Exact-match cache → Redis strings

`app/cache/redis_exact_cache.py` replaces the Python `dict` with plain Redis `GET`/`SET`:

```python
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
```

- Takes an already-connected `Redis` client rather than opening its own connection, so `main.py`
  can create one connection and share it across both caches.
- `_key()` builds the actual Redis key string, e.g. `"How do I get a refund?"` →
  `cache:exact:how do i get a refund?`, reusing the same `normalize()` used in Phase 2.
- `redis-py` returns raw **bytes**, not `str` — Redis itself has no concept of text encoding, it
  just stores byte strings. `get()` has to `.decode("utf-8")` to hand back a normal Python string;
  encoding on the way in (`set()`) is handled automatically by the client.
- Same public interface (`get`/`set`) as the original in-memory `ExactMatchCache`, so `main.py`'s
  calling code doesn't change — only which class gets instantiated, and where the bytes end up.

### Semantic cache → Redis + RediSearch vector index

Plain Redis commands (`GET`, `SET`, `HSET`) only work when you already know the exact key you
want. Semantic lookup is the opposite problem — given a new embedding, we don't know which stored
key is closest; we have to compare against everything and rank by similarity. That capability
comes from **RediSearch**, an add-on module (bundled in the `redis/redis-stack` Docker image) that
adds a `VECTOR` field type and similarity search on top of base Redis.

**The filing-cabinet way to think about indexing**: Redis by itself is a cabinet of labeled
folders — it hands you a folder by its label and doesn't know what's inside. An index is a
standing instruction given once: "watch every folder matching this pattern, and keep a separate,
self-updating lookup structure organized around one particular field inside it, so I can ask
'find me the closest match' quickly later." You say this once (`FT.CREATE`); after that, every
new folder written that matches the pattern is automatically slotted into that structure as a side
effect of the write — no second "index this" call needed per entry.

**Creating the index** (`app/cache/redis_semantic_cache.py`):

```python
self._redis.ft(INDEX_NAME).create_index(
    fields=[
        TextField("question"),
        TextField("answer"),
        NumericField("created_at"),
        VectorField(
            "embedding",
            "FLAT",
            {
                "TYPE": "FLOAT32",
                "DIM": EMBEDDING_DIM,
                "DISTANCE_METRIC": "COSINE",
            },
        ),
    ],
    definition=IndexDefinition(prefix=[KEY_PREFIX], index_type=IndexType.HASH),
)
```

- `.ft(INDEX_NAME)` switches into RediSearch's command namespace for an index named
  `idx:semantic_cache`; `.create_index(...)` wraps the raw `FT.CREATE` command.
- `fields=[...]` — only fields listed here are indexed/searchable; other hash fields are ignored
  for search purposes. `question`/`answer` are declared `TextField` (enables full-text search,
  and ensures they come back cleanly in results); `created_at` is `NumericField` (unused for now,
  placeholder value — would support range/sort queries later).
- `VectorField("embedding", "FLAT", {...})` is the core of it — declares the `embedding` field as
  a vector and tells Redis to build a similarity-search structure around it:
  - `"FLAT"` — brute-force exact search (compares the query against every stored vector), matching
    the exact-search behavior of the Phase 3 numpy version. The alternative, `"HNSW"`, is an
    approximate graph-based method built for large-scale search — not needed at this project's
    cache size.
  - `"TYPE": "FLOAT32"`, `"DIM": EMBEDDING_DIM` — each vector is 384 32-bit floats
    (`all-MiniLM-L6-v2`'s fixed output size), matching how `_vector_to_bytes` packs the numpy array.
  - `"DISTANCE_METRIC": "COSINE"` — matches the cosine similarity the Phase 3 eval and 0.65
    threshold were calibrated against.
- `IndexDefinition(prefix=[KEY_PREFIX], index_type=IndexType.HASH)` — scopes the index to only
  keys starting with `cache:vec:` (ignoring the exact-match cache's `cache:exact:` keys entirely),
  and declares the data is stored as Redis hashes (`HSET`), matching how `add()` writes entries.

Because `FT.CREATE` errors if the index already exists (e.g. on app restart), the call is wrapped
in a try/except that swallows that specific error only.

**Converting a numpy vector to what Redis expects**:

```python
def _vector_to_bytes(vector: np.ndarray) -> bytes:
    return vector.astype(np.float32).tobytes()
```

Redis stores/compares vectors as raw float32 bytes, not JSON or text. This packs the numpy array
into that exact binary layout — used both when writing a new cache entry and when sending the
query vector for a lookup, so both sides use the same byte format.

**Lookup, replacing the Phase 3 `matrix @ vector` line**: instead of computing similarity
ourselves in Python, a single `FT.SEARCH` query asks Redis to find the nearest neighbor using the
index built above — the comparison happens inside Redis, not in our process. One detail that
required care: RediSearch reports cosine as a *distance* (0 = identical, larger = more different),
the inverse of the *similarity* score (1 = identical) the Phase 3 threshold logic expects, so the
lookup code flips it back (`similarity = 1 - distance`) to keep reusing the same 0.65 cutoff.

**Adding an entry**: one `HSET` per cache entry, under a key matching `cache:vec:`. Because the
key matches the index's prefix, RediSearch picks it up into the index automatically — writing the
hash and indexing it are the same operation from the caller's point of view.

### What changes vs. what stays the same

`main.py`'s `/ask` flow — exact-match check, then semantic check, then LLM call — is unchanged in
shape. Only what's underneath each `.get()`/`.lookup()`/`.set()`/`.add()` call moved: from Python
objects in one process's RAM to a Redis container every app instance connects to. The embeddings,
the 0.65 threshold, and the exact-then-semantic layering order all carry over unchanged from
Phase 3.

### Running Redis for this project

`docker-compose.yml` runs the `redis/redis-stack` image (Redis + RediSearch bundled, avoiding a
manual module install), exposing port 6379 for the Redis protocol and port 8001 for RedisInsight
(a web UI for browsing keys and the vector index directly, at `http://localhost:8001`). Started
with `docker compose up -d`.

### `redis-py` import path gotcha

`IndexDefinition`/`IndexType` live at `redis.commands.search.indexDefinition` (camelCase module
name), not the more Pythonic `index_definition` — a reasonable guess that doesn't match the
actual package layout. Worth checking `import redis.commands.search` and listing its directory
rather than guessing snake_case for third-party module paths.

### Verified: cache is shared across instances

Started two separate `uvicorn` processes (`app.main:app` on ports 8000 and 8001), both pointed at
the same `REDIS_URL`. Asked instance A (8000) a question — cache miss, LLM called, answer stored.
Asked instance B (8001) the *same* question — came back as an `exact_hit`, despite instance B
never having called the LLM itself and having no in-process memory shared with instance A.

This is the concrete proof of the Phase 4 milestone: two OS processes with completely separate
Python memory now share cache state, because that state lives in Redis rather than in either
process. This is the same failure mode Phase 2/3's in-memory caches had (each process's cache was
invisible to every other process) — Phase 4 fixes it by relocating the cache to a shared service
instead of changing the caching logic itself.
