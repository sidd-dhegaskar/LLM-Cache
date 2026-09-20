"""
Phase 3 eval — measures the semantic cache's hit rate, false-hit rate, and
false-miss rate across similarity thresholds using eval/paraphrase_clusters.json.

Run: python -m eval.run_eval
"""
import json
from pathlib import Path

from app.cache.semantic_cache import SemanticCache
from app.embeddings import embed

CLUSTERS_PATH = Path(__file__).parent / "paraphrase_clusters.json"
THRESHOLDS = [0.55, 0.60, 0.65, 0.70, 0.75, 0.80]


def load_clusters():
    with open(CLUSTERS_PATH) as f:
        return json.load(f)


def evaluate_threshold(clusters, threshold: float) -> dict:
    cache = SemanticCache(threshold=threshold)

    true_hits = 0      # matched, and matched question is from the SAME cluster
    false_hits = 0     # matched, but matched question is from a DIFFERENT cluster
    misses = 0         # no match found above threshold
    total = 0

    for cluster in clusters:
        cluster_id = cluster["cluster_id"]
        questions = cluster["questions"]

        # seed the cache with the first phrasing of this cluster
        seed_question = questions[0]
        seed_vector = embed(seed_question).vector
        cache.add(seed_question, f"[answer for {cluster_id}]", seed_vector)

        # query with the remaining phrasings, expecting a hit against the seed
        for question in questions[1:]:
            total += 1
            vector = embed(question).vector
            result = cache.lookup(vector)

            if not result.hit:
                misses += 1
                continue

            matched_cluster = next(
                c["cluster_id"] for c in clusters if result.matched_question in c["questions"]
            )
            if matched_cluster == cluster_id:
                true_hits += 1
            else:
                false_hits += 1

    return {
        "threshold": threshold,
        "total_queries": total,
        "true_hits": true_hits,
        "false_hits": false_hits,
        "misses": misses,
        "hit_rate": round((true_hits + false_hits) / total, 4) if total else 0.0,
        "false_hit_rate": round(false_hits / total, 4) if total else 0.0,
        "false_miss_rate": round(misses / total, 4) if total else 0.0,
    }


def main():
    clusters = load_clusters()
    print(f"{'threshold':>10} {'hit_rate':>10} {'false_hit_rate':>16} {'false_miss_rate':>17}")
    for threshold in THRESHOLDS:
        result = evaluate_threshold(clusters, threshold)
        print(
            f"{result['threshold']:>10} {result['hit_rate']:>10} "
            f"{result['false_hit_rate']:>16} {result['false_miss_rate']:>17}"
        )


if __name__ == "__main__":
    main()
