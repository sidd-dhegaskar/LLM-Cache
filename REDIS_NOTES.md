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

## 12. Redis basics — Hashes, HSET, HGET

### What is Redis?

Redis stores data in memory using different data structures.

For example:

```
key              value
-------------------------
name             "Alice"
age              25
```

You can think of Redis as a giant in-memory dictionary:

```
KEY → DATA
```

For simple values, you can use:

```
SET name "Alice"
GET name
```

Result:

```
Alice
```

But Redis can store more than just one value per key.

### What is a Hash?

A Redis Hash is basically a small collection of field → value pairs stored under one Redis key.

Think of it like a JSON object:

```
{
  "name": "Alice",
  "age": "25",
  "city": "Bangalore"
}
```

In Redis, you could represent this as:

```
Key: user:123

Fields:
    name → Alice
    age  → 25
    city → Bangalore
```

So:

```
user:123
   │
   ├── name → Alice
   ├── age  → 25
   └── city → Bangalore
```

This entire thing is called a Redis Hash.

### What does HSET mean?

HSET means:

> Set a field inside a Hash.

For example:

```
HSET user:123 name "Alice"
```

You're saying:

> Inside the Redis key `user:123`, create/update the field `name` with value `"Alice"`.

Now Redis has:

```
user:123
   │
   └── name → Alice
```

You can add more fields:

```
HSET user:123 age 25
HSET user:123 city "Bangalore"
```

Now:

```
user:123
   ├── name → Alice
   ├── age  → 25
   └── city → Bangalore
```

You can also set multiple fields at once:

```
HSET user:123 name "Alice" age 25 city "Bangalore"
```

### What does HGET mean?

HGET means:

> Get one field from a Hash.

For example:

```
HGET user:123 name
```

Redis returns:

```
Alice
```

Or:

```
HGET user:123 age
```

returns:

```
25
```

So the basic relationship is:

```
HSET = write a field
HGET = read a field
```

### SET/GET vs HSET/HGET

This distinction is important.

**Normal Redis key**

```
SET name "Alice"
```

You have:

```
name → Alice
```

There is one value associated with `name`.

**Redis Hash**

```
HSET user:123 name "Alice"
HSET user:123 age 25
```

You have:

```
user:123
   ├── name → Alice
   └── age  → 25
```

So:

```
SET/GET
    key → value

HSET/HGET
    key → {
        field → value
        field → value
        field → value
    }
```

That's the fundamental idea.

### Connecting this to the vector-cache example

```
HSET cache:vec:1 question "How do I get a refund?" answer "..." embedding <bytes> created_at "..."
```

This is creating a Redis Hash:

```
cache:vec:1
   │
   ├── question   → "How do I get a refund?"
   ├── answer     → "..."
   ├── embedding  → <vector bytes>
   └── created_at → "..."
```

Redis isn't treating this as some special "AI object." It's simply a Hash containing fields.

You could retrieve individual fields:

```
HGET cache:vec:1 question
```

→

```
How do I get a refund?
```

Or:

```
HGET cache:vec:1 answer
```

→

```
...
```

Or retrieve everything:

```
HGETALL cache:vec:1
```

which gives you all the fields and values.

### Why use a Hash for this?

Because one cache entry naturally contains multiple pieces of information:

```
cache entry #1

question
answer
embedding
created_at
```

Instead of creating four unrelated Redis keys:

```
cache:vec:1:question
cache:vec:1:answer
cache:vec:1:embedding
cache:vec:1:created_at
```

you group them together:

```
cache:vec:1
   ├── question
   ├── answer
   ├── embedding
   └── created_at
```

This is what Redis Hashes are useful for.

### Data vs. Index — two separate things

There are two separate things:

**Your actual data** — you explicitly create it:

```
HSET cache:vec:1 ...
```

Result:

```
cache:vec:1
   ├── question
   ├── answer
   ├── embedding
   └── created_at
```

**Redis Search index** — you create it with:

```
FT.CREATE ...
```

This is not another Hash. It's an additional data structure Redis maintains to efficiently search the data in those Hashes.

So eventually you'll have something conceptually like:

```
                 Redis

        ┌─────────────────────┐
        │ Your actual data    │
        │                     │
        │ cache:vec:1         │
        │   ├─ question       │
        │   ├─ answer         │
        │   └─ embedding      │
        │                     │
        │ cache:vec:2         │
        │   ├─ question       │
        │   ├─ answer         │
        │   └─ embedding      │
        └─────────────────────┘
                  │
                  │ indexed by
                  ▼
        ┌─────────────────────┐
        │ Redis Search Index  │
        │                     │
        │ vector → structure  │
        │ for fast searching  │
        └─────────────────────┘
```

The Hash is your data. HSET/HGET are how you write/read that data. The index is a separate structure used to search it efficiently.

### How the Hash is useful for search

The key idea is:

> The Hash stores the information. The index uses information from the Hash to make searching fast.

**1. Your data is stored in Hashes**

```
HSET cache:vec:1 question "How do I get a refund?" answer "You can request a refund..." embedding <vector>
HSET cache:vec:2 question "How can I return my order?" answer "Go to returns..." embedding <vector>
HSET cache:vec:3 question "How do I change my password?" answer "Go to settings..." embedding <vector>
```

Conceptually:

```
cache:vec:1
 ├── question  → "How do I get a refund?"
 ├── answer    → "You can request a refund..."
 └── embedding → [0.12, 0.83, ...]

cache:vec:2
 ├── question  → "How can I return my order?"
 ├── answer    → "Go to returns..."
 └── embedding → [0.15, 0.79, ...]

cache:vec:3
 ├── question  → "How do I change my password?"
 ├── answer    → "Go to settings..."
 └── embedding → [0.91, 0.12, ...]
```

The Hash itself doesn't make semantic search fast.

**2. The index looks at the embedding field**

When you create an index telling Redis that `embedding` is a vector field, Redis Search starts maintaining an index based on those vectors.

For example, with HNSW:

```
Hash data                     HNSW index

cache:vec:1 ────────────────→ vector 1
                                  ↕
cache:vec:2 ────────────────→ vector 2
                                  ↕
cache:vec:3 ────────────────→ vector 3
```

The important part is that the index associates:

```
vector → Redis key
```

So Redis can eventually say:

> "The vectors closest to this query vector correspond to cache:vec:1 and cache:vec:2."

**3. A user asks a new question**

Suppose the user asks:

```
"Can I get my money back?"
```

Your application converts that into an embedding:

```
"Can I get my money back?"
             ↓
[0.13, 0.81, 0.42, ...]
```

Now Redis Search searches the vector index for vectors close to this one. It might find:

```
Query vector
     │
     ├── closest → cache:vec:1
     │              "How do I get a refund?"
     │
     ├── next     → cache:vec:2
     │              "How can I return my order?"
     │
     └── far      → cache:vec:3
                    "How do I change my password?"
```

Then Redis gives you the matching keys.

**4. Then you use HGET to get the actual answer**

Suppose the search returns `cache:vec:1`. Now your application can do:

```
HGET cache:vec:1 answer
```

and get:

```
"You can request a refund..."
```

So the whole flow is:

```
User question
     │
     ▼
Create embedding
     │
     ▼
Search vector index
     │
     ▼
Find cache:vec:1
     │
     ▼
HGET cache:vec:1 answer
     │
     ▼
Return cached answer
```

**The important separation:**

```
HASH
└── Stores the actual data
    ├── question
    ├── answer
    └── embedding

INDEX
└── Makes finding relevant Hashes fast
    └── uses the embedding
```

So HGET isn't what performs the semantic search. HGET is basically the final step: "I found the relevant Redis key; now give me the answer stored inside it."
</content>
</invoke>
