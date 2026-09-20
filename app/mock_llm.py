"""
Standalone fake LLM server. Stands in for a real hosted LLM API so Phase 1
doesn't require an API key. Adds artificial latency and returns a
deterministic canned answer, like a real LLM would for a repeated prompt.
"""
import asyncio
import hashlib
import os
import random

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="Mock LLM Server")

LATENCY_MS = int(os.getenv("MOCK_LLM_LATENCY_MS", "800"))
LATENCY_JITTER_MS = int(os.getenv("MOCK_LLM_LATENCY_JITTER_MS", "400"))

CANNED_ANSWERS = [
    "You can request a refund within 30 days of purchase by visiting your account's order history.",
    "Our support team is available 24/7 via chat, email, or phone.",
    "To reset your password, click 'Forgot password' on the login page and follow the emailed link.",
    "Shipping typically takes 3-5 business days for standard orders.",
    "You can upgrade or downgrade your subscription plan at any time from account settings.",
]

# maps each canned answer's index to keywords that should select it
KEYWORD_ROUTES: list[tuple[int, list[str]]] = [
    (0, ["refund", "money back", "reimburse"]),
    (1, ["support", "customer service", "contact", "reach"]),
    (2, ["password", "login", "log in", "sign in"]),
    (3, ["shipping", "delivery", "deliver", "arrive", "order arrive"]),
    (4, ["subscription", "plan", "upgrade", "downgrade"]),
]


def _select_answer(question: str) -> str:
    normalized = question.strip().lower()
    for idx, keywords in KEYWORD_ROUTES:
        if any(kw in normalized for kw in keywords):
            return CANNED_ANSWERS[idx]

    idx = int(hashlib.sha256(normalized.encode()).hexdigest(), 16) % len(CANNED_ANSWERS)
    return CANNED_ANSWERS[idx]


class AskRequest(BaseModel):
    question: str


class AskResponse(BaseModel):
    answer: str
    model: str = "mock-llm-v1"


@app.post("/v1/complete", response_model=AskResponse)
async def complete(req: AskRequest):
    delay_ms = LATENCY_MS + random.randint(0, LATENCY_JITTER_MS)
    await asyncio.sleep(delay_ms / 1000)

    return AskResponse(answer=_select_answer(req.question))


@app.get("/health")
async def health():
    return {"status": "ok"}
