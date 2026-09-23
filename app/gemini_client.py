"""
Real LLM backend — calls Google's Gemini API via the newer Interactions API
(POST /v1beta/interactions). Used when LLM_PROVIDER=gemini (see
app/llm_client.py for the switch). Requires GEMINI_API_KEY in the
environment.

Note: the older generateContent endpoint 404s for several recent model
names on free-tier keys ("no longer available to new users") — the
Interactions API is what Google's own error messages point to instead.
"""
import logging
import os
import time

import httpx

logger = logging.getLogger("app.gemini_client")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"

# Published per-token pricing for gemini-3.6-flash, in $ per 1 million tokens.
# Update these if Google changes pricing or the model changes.
INPUT_PRICE_PER_1M_TOKENS = float(os.getenv("GEMINI_INPUT_PRICE_PER_1M", "0.75"))
OUTPUT_PRICE_PER_1M_TOKENS = float(os.getenv("GEMINI_OUTPUT_PRICE_PER_1M", "3.75"))


class GeminiResult:
    def __init__(self, answer: str, latency_s: float, cost: float, prompt_tokens: int, output_tokens: int):
        self.answer = answer
        self.latency_s = latency_s
        self.cost = cost
        self.prompt_tokens = prompt_tokens
        self.output_tokens = output_tokens


def _compute_cost(prompt_tokens: int, output_tokens: int) -> float:
    # output_tokens includes "thought" tokens (usage.total_output_tokens
    # already folds these in) — matches how Google's $/1M pricing counts them.
    input_cost = (prompt_tokens / 1_000_000) * INPUT_PRICE_PER_1M_TOKENS
    output_cost = (output_tokens / 1_000_000) * OUTPUT_PRICE_PER_1M_TOKENS
    return input_cost + output_cost


def _extract_answer(data: dict) -> str:
    # Interactions API returns a list of "steps" — some are internal
    # "thought" steps, the actual reply is the step with type "model_output".
    for step in data.get("steps", []):
        if step.get("type") == "model_output":
            parts = step.get("content", [])
            text_parts = [p["text"] for p in parts if p.get("type") == "text"]
            if text_parts:
                return "".join(text_parts)

    raise ValueError(f"no model_output step found in Gemini response: {data}")


async def ask_gemini(question: str) -> GeminiResult:
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not set — required when LLM_PROVIDER=gemini")

    payload = {
        "model": GEMINI_MODEL,
        "input": question,
    }
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": GEMINI_API_KEY,  # Interactions API takes the key as a header, not a query param
    }

    logger.info("gemini request: model=%s question=%r", GEMINI_MODEL, question)

    start = time.perf_counter()
    try:
        # gemini-3.6-flash does internal "thinking" before answering (see
        # usage.total_thought_tokens in the response), which can push
        # latency past a 30s timeout — 60s gives it more room.
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(GEMINI_URL, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError:
        latency_s = time.perf_counter() - start
        logger.exception("gemini request failed after %.3fs", latency_s)
        raise
    latency_s = time.perf_counter() - start

    answer = _extract_answer(data)

    # usage carries the real token counts for this exact call — this is
    # what lets us compute real cost instead of a flat guess.
    usage = data.get("usage", {})
    prompt_tokens = usage.get("total_input_tokens", 0)
    output_tokens = usage.get("total_output_tokens", 0)
    cost = _compute_cost(prompt_tokens, output_tokens)

    logger.info(
        "gemini response: latency=%.3fs prompt_tokens=%d output_tokens=%d cost=$%.6f answer=%r",
        latency_s, prompt_tokens, output_tokens, cost, answer,
    )

    return GeminiResult(
        answer=answer,
        latency_s=latency_s,
        cost=cost,
        prompt_tokens=prompt_tokens,
        output_tokens=output_tokens,
    )
