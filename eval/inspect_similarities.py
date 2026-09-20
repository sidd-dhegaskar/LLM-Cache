"""
Diagnostic: print raw cosine similarity scores between paraphrase pairs
so we can pick a threshold grounded in real numbers instead of guessing.

Run: python -m eval.inspect_similarities
"""
import json
from pathlib import Path

import numpy as np

from app.embeddings import embed

CLUSTERS_PATH = Path(__file__).parent / "paraphrase_clusters.json"


def main():
    with open(CLUSTERS_PATH) as f:
        clusters = json.load(f)

    print("=== within-cluster similarities (should be HIGH) ===")
    for cluster in clusters:
        questions = cluster["questions"]
        vectors = [embed(q).vector for q in questions]
        seed_q, seed_v = questions[0], vectors[0]
        for q, v in zip(questions[1:], vectors[1:]):
            sim = float(np.dot(seed_v, v))
            print(f"  [{cluster['cluster_id']}] {sim:.4f}  '{seed_q}' vs '{q}'")

    print("\n=== across-cluster similarities (should be LOW) ===")
    seeds = [(c["cluster_id"], embed(c["questions"][0]).vector) for c in clusters]
    for i in range(len(seeds)):
        for j in range(i + 1, len(seeds)):
            id_a, vec_a = seeds[i]
            id_b, vec_b = seeds[j]
            sim = float(np.dot(vec_a, vec_b))
            print(f"  {sim:.4f}  '{id_a}' vs '{id_b}'")


if __name__ == "__main__":
    main()
