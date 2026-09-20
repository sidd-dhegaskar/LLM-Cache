"""
Phase 2 — Exact-Match Cache.
In-memory dict[question] = answer in front of the LLM call. Only helps when
the incoming question string matches a previously seen one exactly (after
normalization) — paraphrases still miss, which is the problem Phase 3 fixes.
"""


def normalize(question: str) -> str:
    return question.strip().lower()


class ExactMatchCache:
    def __init__(self):
        self._store: dict[str, str] = {}

    def get(self, question: str) -> str | None:
        return self._store.get(normalize(question))

    def set(self, question: str, answer: str) -> None:
        self._store[normalize(question)] = answer

    def __len__(self) -> int:
        return len(self._store)
