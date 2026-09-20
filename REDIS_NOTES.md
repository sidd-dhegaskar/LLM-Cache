# Redis Vector Search Notes

The easiest way to understand this is to separate index creation, adding vectors, and searching vectors.

For your project, the flow is essentially:

```
                    Redis Stack
                       │
              ┌────────┴────────┐
              │                 │
        Redis Hashes       RediSearch Index
              │                 │
        actual data       search structure
              │                 │
              └────────┬────────┘
                       │
                 KNN vector search
```

## 1. What does "creating an index" actually mean?

Suppose you store:

```
cache:vec:1
    question = "How do I get a refund?"
    answer   = "..."
    embedding = [0.12, -0.43, 0.81, ...]   # 384 numbers
```

and:

```
cache:vec:2
    question = "What is your refund policy?"
    answer   = "..."
    embedding = [0.15, -0.41, 0.79, ...]
```

Redis itself just sees these as hashes.

It doesn't automatically know:

> "embedding is a 384-dimensional vector that I should search by cosine similarity."

So you create an index with something conceptually like:

```
FT.CREATE cache_idx
ON HASH
PREFIX 1 "cache:vec:"
SCHEMA
    question TEXT
    answer TEXT
    embedding VECTOR FLAT
        DIM 384
        DISTANCE_METRIC COSINE
```

This is basically telling RediSearch:

> "Whenever you see a hash whose key starts with `cache:vec:`, look at its `embedding` field and maintain a vector-search index for it."

## 2. What does the index actually contain?

This is the important conceptual part.

You have:

Redis data:

```
cache:vec:1 → embedding A
cache:vec:2 → embedding B
cache:vec:3 → embedding C
cache:vec:4 → embedding D
...
```

The index is additional data maintained by RediSearch that makes searching these vectors possible.

Think:

```
                 Redis
                   │
        ┌──────────┴──────────┐
        │                     │
     Hash data              Index
        │                     │
        │              vector search structure
        │                     │
        ▼                     ▼
 embedding A              A ── B
 embedding B              │ ╲
 embedding C              │  ╲ C
 embedding D              └── D
```

The exact structure depends on the algorithm you choose.

## 3. There are two main algorithms

RediSearch gives you:

- FLAT
- HNSW

They represent two very different approaches.

### FLAT = brute force

Imagine we have:

```
A
B
C
D
E
F
...
1 million vectors
```

New query:

```
Q = "How can I get my money back?"
```

Its embedding is:

```
Q = [0.13, -0.42, 0.80, ...]
```

With FLAT, Redis essentially does:

```
similarity(Q, A)
similarity(Q, B)
similarity(Q, C)
similarity(Q, D)
...
similarity(Q, 1,000,000)
```

Then:

```
sort/find best
       ↓
return top K
```

So:

```
                Q
                │
        ┌───────┼────────┐
        ↓       ↓        ↓
        A       B        C  ... all vectors
        │       │        │
        ↓       ↓        ↓
     similarity calculations
                │
                ↓
           best matches
```

**Why use FLAT?**

Because it is:

- exact
- simple
- predictable
- good for smaller datasets

But the fundamental cost is:

```
O(N)
```

You have to consider every vector.

## 4. HNSW is the interesting algorithm

For large-scale vector search, you generally don't want:

```
query → compare against 1,000,000 vectors
```

So HNSW creates a graph.

**HNSW = Hierarchical Navigable Small World**

The important concepts are:

### 1. Graph

Vectors become nodes.

Similar vectors are connected.

For example:

```
          A
        /   \
       B     C
      / \     \
     D   E     F
          \
           G
```

The edges basically mean:

> "These vectors are relatively close to each other."

### 2. Multiple layers

HNSW isn't just one graph.

It builds layers:

```
Layer 2:

          A -------- F


Layer 1:

       A ---- C ---- F
       |      |      |
       B ---- D ---- E


Layer 0:

 A -- B -- C -- D -- E -- F -- G -- H -- I ...
```

Higher layers contain fewer nodes.

Lower layers contain more nodes.

Think of it like:

```
Layer 2     very sparse
               ↓
Layer 1     somewhat sparse
               ↓
Layer 0     lots of vectors
```

This allows search to move quickly through the dataset.

## 5. How HNSW searches

Suppose your query is:

```
Q = "How do I get a refund?"
```

We want:

```
closest vector to Q
```

Instead of checking everything:

```
Q → A
Q → B
Q → C
Q → D
Q → E
...
```

HNSW does something more like:

```
                 start
                   ↓
                  A
                 / \
                B   F
                    |
                    C
                    |
                    D
```

It starts at an entry point in a high layer.

It asks:

> "Among my neighbors, which one is closer to Q?"

Suppose:

```
A → distance 0.80
F → distance 0.45
```

Move toward F.

Then:

```
F → neighbors
```

Maybe:

```
C → distance 0.25
D → distance 0.60
```

Move toward C.

Eventually:

```
C → very close
```

Then it drops down to the next layer and searches more precisely.

So conceptually:

```
        High layer
             │
             ▼
        jump quickly
             │
             ▼
        Medium layer
             │
             ▼
        narrow down
             │
             ▼
        Bottom layer
             │
             ▼
       nearest vectors
```

That's why HNSW can search a huge collection without comparing the query against every vector.

## 6. Important: HNSW is approximate

This is one of the most important concepts.

FLAT:

```
check everything
      ↓
true nearest neighbor
```

HNSW:

```
navigate graph
      ↓
likely nearest neighbors
```

It is possible for HNSW to miss the mathematically closest vector.

That's the approximate nearest neighbor (ANN) tradeoff:

```
             Search speed
                  ↑
                  │
             HNSW │
                  │
                  │
                  │
             FLAT │
                  └────────────→ exactness
```

You gain speed by accepting a small possibility of not finding the absolute best match.

## 7. What happens when a new cache entry is added?

This is where your earlier statement becomes important:

> "Any folder you add later gets automatically slotted into that lookup structure."

Suppose you execute:

```
HSET cache:vec:100
    question "How do refunds work?"
    answer "..."
    embedding <384 floats>
```

RediSearch notices:

```
key = cache:vec:100
```

matches:

```
PREFIX "cache:vec:"
```

So it indexes the vector.

### With FLAT

Conceptually, it simply becomes another vector in the searchable collection:

```
A
B
C
D
NEW
```

No complicated graph construction.

### With HNSW

Redis has to insert the new vector into the HNSW graph.

Conceptually:

```
new vector
    ↓
find nearby existing vectors
    ↓
choose neighbors
    ↓
create edges
    ↓
insert into appropriate HNSW layers
```

So the graph might change from:

```
A ─── B ─── C
      │
      D
```

to:

```
A ─── B ─── C
      │ \   │
      D ─ NEW
```

The exact HNSW construction is more sophisticated than this diagram, but this is the right mental model.

## 8. Now let's walk through your actual cache request

Suppose the user asks:

```
"How do I get a refund?"
```

Your application first generates an embedding:

```
question
   │
   ▼
embedding model
   │
   ▼
[0.12, -0.43, 0.81, ...]
```

384 numbers.

Call it:

```
Q
```

### Step 1 — Exact cache

Your application first checks:

```
SET/GET cache:exact:<normalized question>
```

If it finds an exact match:

```
Redis
  │
  ▼
exact answer
```

Done.

No vector search needed.

## 9. If exact match misses → semantic search

Now we have:

```
Q = [384-dimensional vector]
```

The application sends Q to RediSearch through `FT.SEARCH`.

Conceptually:

```
FT.SEARCH cache_idx
    "*=>[KNN 1 @embedding $query_vector]"
```

Meaning:

```
cache_idx
   ↓
search embedding field
   ↓
find 1 nearest vector
   ↓
using query_vector
```

## 10. Redis performs the vector search

Suppose your database contains:

```
Vector     Question

V1         "How do refunds work?"
V2         "What is your refund policy?"
V3         "How do I change my password?"
V4         "Where is my order?"
V5         "Can I get my money back?"
```

Your query:

```
Q = "How do I get a refund?"
```

The embedding model converts it to:

```
Q
```

RediSearch compares Q against the indexed vectors.

With FLAT:

```
Q
│
├── similarity(V1)
├── similarity(V2)
├── similarity(V3)
├── similarity(V4)
└── similarity(V5)
```

Suppose:

```
V1 → 0.91
V2 → 0.87
V3 → 0.12
V4 → 0.08
V5 → 0.89
```

K = 1 means:

```
return V1
```

## 11. Then comes your threshold

This is an important distinction:

Redis finds the nearest vector.

Your application decides:

> "Is that vector similar enough to trust?"

For example:

```
best similarity = 0.91

threshold = 0.65
```

Therefore:

```
0.91 >= 0.65
       ↓
CACHE HIT
```

Return the cached answer.

But:

```
best similarity = 0.43

0.43 < 0.65
       ↓
CACHE MISS
       ↓
call LLM
```

So your complete architecture is:

```
             User question
                   │
                   ▼
          normalize question
                   │
                   ▼
             Exact Redis
                   │
          ┌────────┴────────┐
          │                 │
        HIT                MISS
          │                 │
          ▼                 ▼
       answer          embedding model
                            │
                            ▼
                     vector embedding
                            │
                            ▼
                     RediSearch KNN
                            │
                            ▼
                    nearest vector
                            │
                            ▼
                    similarity score
                            │
                    ┌───────┴───────┐
                    │               │
                >= 0.65          < 0.65
                    │               │
                    ▼               ▼
                CACHE HIT       CACHE MISS
                    │               │
                    ▼               ▼
                  answer           LLM
                                    │
                                    ▼
                                  answer
                                    │
                                    ▼
                             store new vector
```
</content>
</invoke>
