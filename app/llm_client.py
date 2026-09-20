"""
LLM API wrapper. Points at MOCK_LLM_URL for now (Phase 1) instead of a real
hosted LLM, so no API key is required. Swapping in a real provider later
means changing this module only.
"""
import os
import time

import httpx

MOCK_LLM_URL = os.getenv("MOCK_LLM_URL", "http://localhost:9000")
COST_PER_CALL = float(os.getenv("MOCK_LLM_COST_PER_CALL", "0.002"))


class LLMResult:
    def __init__(self, answer: str, latency_s: float, cost: float):
        self.answer = answer
        self.latency_s = latency_s
        self.cost = cost


async def ask_llm(question: str) -> LLMResult:
    start = time.perf_counter()
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(f"{MOCK_LLM_URL}/v1/complete", json={"question": question})
        resp.raise_for_status()
        data = resp.json()
    latency_s = time.perf_counter() - start

    return LLMResult(answer=data["answer"], latency_s=latency_s, cost=COST_PER_CALL)
