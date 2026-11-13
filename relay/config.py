"""
Every knob I can tune lives here. Each value has a short note on why.
The first draft used placeholder numbers; after the real-service
probe, I replaced the limits with what I measured (see
docs/DECISIONS.md and docs/tables.md).
"""
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# --- paths ---
DATA_PDFS = ROOT / "data" / "pdfs"
DATA_TEXT = ROOT / "data" / "text"
MANIFEST = ROOT / "data" / "manifest.json"
RESULTS_CSV = ROOT / "results" / "results.csv"
TRACES_JSONL = ROOT / "results" / "traces.jsonl"
PROBE_RAW = ROOT / "results" / "probe_raw.jsonl"
BUDGET_JSON = ROOT / "results" / "budget.json"

# --- papers: exact arXiv versions required by the brief ---
PAPERS = {
    "attention": {
        "arxiv_id": "1706.03762",
        "version": "v7",
        "title": "Attention Is All You Need",
    },
    "moshi": {
        "arxiv_id": "2410.00037",
        "version": "v1",
        "title": "Moshi: a speech-text foundation model for real-time dialogue",
    },
    "hnet": {
        "arxiv_id": "2507.07955",
        "version": "v2",
        "title": "Dynamic Chunking for End-to-End Hierarchical Sequence Modeling",
    },
}

# --- retrieval settings (three required) ---
# Chunk size is in whitespace-tokens (not model tokens). Close enough for
# BM25; model tokens are ~1.3x these numbers for English prose.
RETRIEVAL_SETTINGS = {
    "A": {"chunk_tokens": 150, "overlap": 30, "fetch_k": 8, "final_k": 8, "rerank": False},
    "B": {"chunk_tokens": 400, "overlap": 60, "fetch_k": 4, "final_k": 4, "rerank": False},
    "C": {"chunk_tokens": 400, "overlap": 60, "fetch_k": 12, "final_k": 4, "rerank": True},
}
# A: many small pieces, hope coverage beats focus.
# B: fewer bigger pieces, less context switching for the drafter.
# C: over-fetch then MMR-dedupe; same output size as B but with redundancy
#    penalty so near-duplicate passages do not crowd out complementary ones.

# --- model ---
# Changed from llama-3.1-8b-instant to qwen/qwen3.8-27b after discovering
# that the user's free-tier account does not have the llama family
# available; the /models list for this key only has openai/gpt-oss-*,
# qwen/qwen3.8-27b and a few Arabic/TTS models. See DECISIONS D-003.
GROQ_MODEL = "qwen/qwen3.8-27b"
GROQ_BASE_URL = "https://api.groq.com/openai/v1"

# --- scheduler config (filled from Phase 2 probe measurements) ---
# Measured on 2026-11-10 against qwen/qwen3.8-27b on this free-tier key:
#   RPM = 30, TPM (total) = 8000, ITPM (input) = 7000, RPD = 1000.
# All from response headers + 429 bodies. See docs/tables.md.
@dataclass
class SchedulerConfig:
    rpm_limit: int = 30                  # x-ratelimit-limit-requests / 2 (per-min from daily)
    tpm_limit: int = 8000                # x-ratelimit-limit-tokens (total)
    itpm_limit: int = 7000               # input tokens per minute (from 429 body)
    rpd_limit: int = 1000                # x-ratelimit-limit-requests (daily)
    safety_margin: float = 0.80          # use 80% of the measured limit; see D-004
    max_in_flight: int = 4               # provisional; retuned in Phase 5
    deadline_ms: int = 15000             # p95 TTFT target for the person
    retry_budget_per_req: int = 3        # bounded; respect retry-after
    global_retry_budget: int = 50        # soft cap over a 60s window
    backoff_base_ms: int = 500
    backoff_cap_ms: int = 8000

SCHEDULER = SchedulerConfig()

# --- daily Groq budget (user-approved ceiling) ---
DAILY_REAL_REQUEST_CEILING = 250
BATCH_APPROVAL_THRESHOLD = 50  # ask before any batch bigger than this
