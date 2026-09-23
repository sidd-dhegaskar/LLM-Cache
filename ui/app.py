"""
Streamlit UI for the LLM cache API. Talks to the FastAPI app over HTTP
(same /ask and /stats endpoints used by curl/Invoke-RestMethod) — it's a
thin client, no caching logic lives here.

Run: streamlit run ui/app.py
"""
import os

import requests
import streamlit as st

# Where the FastAPI app (app/main.py) is running — override with API_BASE_URL
# if it's not on the default local port.
API_BASE_URL = os.getenv("API_BASE_URL", "http://127.0.0.1:8000")

st.set_page_config(page_title="LLM Cache", page_icon="🧠", layout="centered")

# outcome -> (label, color) used for the result badge below the answer
OUTCOME_STYLE = {
    "exact_hit": ("Exact-match cache hit", "green"),
    "semantic_hit": ("Semantic cache hit", "blue"),
    "miss": ("Cache miss — LLM called", "orange"),
}


def fetch_stats() -> dict | None:
    # Called on every rerun to keep the sidebar's running totals live —
    # /stats is cheap (in-memory counters on the API side), so no caching needed.
    try:
        resp = requests.get(f"{API_BASE_URL}/stats", timeout=5)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException:
        return None


def ask_question(question: str) -> dict | None:
    try:
        resp = requests.post(f"{API_BASE_URL}/ask", json={"question": question}, timeout=35)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        st.error(f"Request failed: {e}")
        return None


st.title("🧠 LLM Semantic Cache")

# --- sidebar: cumulative stats across all requests the API has ever served ---
with st.sidebar:
    st.header("Running stats")
    stats = fetch_stats()
    if stats is None:
        st.warning(f"Can't reach API at {API_BASE_URL}")
    else:
        st.metric("Total requests", stats["request_count"])
        st.metric("Hit rate", f"{stats['hit_rate'] * 100:.1f}%")
        col1, col2 = st.columns(2)
        col1.metric("Exact hits", stats["exact_hits"])
        col2.metric("Semantic hits", stats["semantic_hits"])
        st.metric("LLM calls", stats["llm_call_count"])
        st.metric("Total cost (est.)", f"${stats['total_cost_usd']:.4f}")
        st.caption(
            f"avg LLM latency: {stats['avg_llm_latency_s']}s · "
            f"avg embedding latency: {stats['avg_embedding_latency_s']}s"
        )

# --- main panel: ask a question, see where the answer came from ---
question = st.text_input("Ask a question", placeholder="How do I get a refund?")

if st.button("Ask", type="primary") and question.strip():
    with st.spinner("Thinking..."):
        result = ask_question(question)

    if result is not None:
        st.markdown("### Answer")
        st.write(result["answer"])

        label, color = OUTCOME_STYLE.get(result["outcome"], (result["outcome"], "gray"))
        st.markdown(f":{color}[**{label}**]")

        # per-request metrics — this is the "where did this answer come from" panel
        m1, m2, m3 = st.columns(3)
        m1.metric("Latency", f"{result['latency_s']}s")
        m2.metric("Cost", f"${result['cost_usd']:.6f}")
        if result.get("similarity") is not None:
            m3.metric("Similarity", f"{result['similarity']:.4f}")

        if result.get("prompt_tokens") or result.get("output_tokens"):
            st.caption(
                f"Tokens — prompt: {result.get('prompt_tokens', 0)}, "
                f"output: {result.get('output_tokens', 0)}"
            )

        if result.get("matched_question"):
            st.caption(f"Matched cached question: \"{result['matched_question']}\"")
