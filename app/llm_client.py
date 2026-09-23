"""
LLM API wrapper. Dispatches to either the mock LLM (Phase 1, default — no
API key needed) or a real Gemini call, based on LLM_PROVIDER. main.py only
ever calls ask_llm() and doesn't need to know which backend served it.
"""
import logging
import os
import time

import httpx

from app.gemini_client import ask_gemini

logger = logging.getLogger("app.llm_client")

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "mock")  # "mock" | "gemini"

MOCK_LLM_URL = os.getenv("MOCK_LLM_URL", "http://localhost:9000")
MOCK_COST_PER_CALL = float(os.getenv("MOCK_LLM_COST_PER_CALL", "0.002"))


class LLMResult:
    def __init__(self, answer: str, latency_s: float, cost: float, prompt_tokens: int = 0, output_tokens: int = 0):
        self.answer = answer
        self.latency_s = latency_s
        self.cost = cost
        self.prompt_tokens = prompt_tokens
        self.output_tokens = output_tokens


async def _ask_mock_llm(question: str) -> LLMResult:
    logger.info("mock llm request: question=%r", question)
    start = time.perf_counter()
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(f"{MOCK_LLM_URL}/v1/complete", json={"question": question})
        resp.raise_for_status()
        data = resp.json()
    latency_s = time.perf_counter() - start

    logger.info("mock llm response: latency=%.3fs answer=%r", latency_s, data["answer"])
    return LLMResult(answer=data["answer"], latency_s=latency_s, cost=MOCK_COST_PER_CALL)


async def ask_llm(question: str) -> LLMResult:
    if LLM_PROVIDER == "gemini":
        result = await ask_gemini(question)
        return LLMResult(
            answer=result.answer,
            latency_s=result.latency_s,
            cost=result.cost,
            prompt_tokens=result.prompt_tokens,
            output_tokens=result.output_tokens,
        )

    return await _ask_mock_llm(question)
