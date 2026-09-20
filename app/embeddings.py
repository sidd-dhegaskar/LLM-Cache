"""
Local embedding model wrapper. Runs sentence-transformers locally so no
embedding API key is required, matching the no-real-API-key approach used
for the LLM in Phase 1.
"""
import time

from sentence_transformers import SentenceTransformer

_MODEL_NAME = "all-MiniLM-L6-v2"
_model: SentenceTransformer | None = None


def _get_model() -> SentenceTransformer:
    global _model
    if _model is None:
        _model = SentenceTransformer(_MODEL_NAME)
    return _model


class EmbeddingResult:
    def __init__(self, vector, latency_s: float):
        self.vector = vector
        self.latency_s = latency_s


def embed(text: str) -> EmbeddingResult:
    start = time.perf_counter()
    vector = _get_model().encode(text, normalize_embeddings=True)
    latency_s = time.perf_counter() - start
    return EmbeddingResult(vector=vector, latency_s=latency_s)
