# Decisions

Each design choice, the alternative, the measured evidence, and what it
cost. New entries go at the top.

---

## D-004 — Safety margin of 80% of measured RPM/TPM

- **Chose:** scheduler aims for 0.80 × measured limit (24 RPM, 6400 TPM
  effective, 5600 ITPM effective).
- **Alternative:** 90% (closer to the ceiling, more throughput, more
  429s) or 50% (never 429s but wastes a lot of free-tier capacity).
- **Evidence:** at 1.2 s spacing (effectively 50 RPM) 5/40 probe
  requests hit RPM 429 — the limit is a hard cliff, not a soft one.
  At 80% we have 5 requests/minute of headroom before we start
  shedding; given Groq's retry-after of 2 s on RPM 429, a brief
  overshoot is cheap, but a sustained one is not.
- **Cost:** we leave ~6 RPM on the table at peak.

## D-003 — Switched model from llama-3.1-8b-instant to qwen/qwen3.8-27b

- **Chose:** `qwen/qwen3.8-27b` as the primary pipeline model.
- **Alternative:** `openai/gpt-oss-20b` (smaller, likely faster TTFT) or
  `openai/gpt-oss-120b` (bigger, quality).
- **Evidence:** My first 10 real requests all came back with
  `{"error": {"code": "model_not_found", "message": "The model
  llama-3.1-8b-instant does not exist or you do not have access to
  it."}}`. Listing `/v1/models` on the user's key showed no llama
  models. User picked qwen/qwen3.8-27b over the gpt-oss options.
- **Cost:** Lost 10 real requests to 404s (budget ledger still counts
  them — honest accounting). Qwen's TTFT and TPM behaviour will be
  whatever the probe measures.

## D-002 — Three retrieval settings: small/8, medium/4, medium/12→4 with MMR

- **Chose:** A = 150-token chunks, top-8, no rerank. B = 400-token
  chunks, top-4, no rerank. C = 400-token chunks, fetch-12 then
  MMR-dedupe to top-4.
- **Alternatives:** TF-IDF+cosine, a cross-encoder rerank, semantic
  chunking by section boundary only.
- **Evidence:** not measured yet (Phase 6). Hypothesis: A has coverage
  but wastes tokens; B is cheap but misses when the answer straddles a
  chunk; C should beat B at similar token cost.
- **Cost:** three retrieval runs over all questions; tokens per answer
  for B and C stay roughly equal.

## D-001 — Model: llama-3.1-8b-instant

- **Chose:** `llama-3.1-8b-instant` for every pipeline step.
- **Alternative:** `llama-3.3-70b-versatile` for drafting, 8b for
  Understand/Verify.
- **Evidence:** not yet measured; the brief rewards staying inside
  limits more than raw answer quality, and 8b's higher RPM/TPM budget
  on the free tier lets us run more scenarios.
- **Cost:** some tricky questions may be answered worse. We keep the
  single-model choice until Phase 6 shows it is the bottleneck.
