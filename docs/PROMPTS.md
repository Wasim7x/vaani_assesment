# Prompts I gave the assistant

Running log of prompts given to the assistant during the project.

---

## The Three Prompts That Mattered Most

### 1. The Boundary & Invariant Prompt (Phase 0)
> "You are helping me build a take-home assignment called 'The Relay'. The full brief is in `./The_Relay_Assignment_Brief.pdf`. Read it completely before writing any code.
> 
> Hard boundaries that must NEVER be violated:
> 1. Cap real Groq requests at a strict ceiling of 250 requests/day (tracked in a ledger). Do not burn the free-tier quota.
> 2. Every pipeline answer must cite where it came from using `[paper, §section, p.N]`, or reply with exactly: 'I cannot answer from the provided papers.' No hallucinations.
> 3. Stand-in service must replicate real Groq behaviors (slow, fail, 429 rate limit with Retry-After, load slowdown, streaming chunks).
> 4. Results tables must be generated directly from raw recorded measurements (`results.csv` and `probe_raw.jsonl`). Zero hand-crafted numbers.
> Build the 4-step pipeline (Understand, Retrieve, Draft, Verify) with at least one tool call, one branch, and one redraft loop."

*Why it mattered:* This prompt locked down the system's invariants from minute zero. Without the 250 daily cap and strict table generation from raw files, the assistant would have exhausted the Groq API key on early synthetic tests and risked fabricating figures.

---

### 2. The Stand-in Calibration Prompt (Phase 3)
> "Do not write arbitrary `sleep(random())` delays for the stand-in model service. Read the real measurements from `results/probe_raw.jsonl`. 
> 
> Write `standin/calibrate.py` to fit an empirical log-normal distribution ($\mu, \sigma$) directly to the observed TTFT of successful probe calls. For rate limits, extract the exact headers and bodies from the probe's 429 responses (`Limit 30` RPM, `Limit 7000` ITPM, `Limit 8000` TPM) and mirror them in `standin/server.py`. Implement load degradation as TTFT scaled by excess in-flight requests. Also document clearly where this mathematical approximation differs from real Groq."

*Why it mattered:* This forced the stand-in to be an honest, empirically grounded proxy rather than a fake mock server. It made our heavy load testing (`burst`, `overload`, `halved`) reflect true upstream queuing dynamics.

---

### 3. The Scheduler Degradation & Protection Prompt (Phase 5)
> "Build the scheduler in `relay/scheduler.py` with defensive queuing and multi-tier rate limiting:
> 1. Use an 80% safety margin (not 90%) over measured limits: effective caps of 24 RPM, 6,400 TPM, and 5,600 ITPM.
> 2. Implement a bounded admission queue (max depth 64) with a hard 15-second request deadline. If the required rate wait exceeds the request's deadline, drop it immediately before dispatching to save quota.
> 3. Implement daily degradation: when remaining daily budget is $<25$, cap `max_tokens` at 128 (soft landing); when remaining is 0, drop immediately in $<1$ ms without an HTTP call.
> 4. Include a 3-state circuit breaker (tripping after 5 consecutive 5xx/timeouts for 10s)."

*Why it mattered:* This gave the system realistic production resilience. It decoupled admission queuing from upstream calls, prevented cascading 429 storms, and implemented graceful soft landing as daily quotas drained.

---

## Chronological Prompt Log

### Phase 0 — Scoping
> You are helping me build a take-home assignment called "The Relay".
> The full brief is in the PDF at ./The_Relay_Assignment_Brief.pdf.
> Read it fully before doing anything. Every requirement in it is
> mandatory. Extra features are fine, but only after every requirement
> is done.

### Phase 1 — Retrieval & Ingestion
> Download the 3 arXiv papers at their exact required versions (1706.03762v7, 2410.00037v1, 2507.07955v2). Save their SHA256 hashes to `manifest.json`. Parse the PDFs into section-aware text chunks and build the BM25 index with settings A, B, and C.

### Phase 2 — Real Service Probe
> Probe Groq's free tier with a conservative sequence of small requests. Measure TTFT distribution, trigger RPM and TPM 429s, capture headers and response bodies, and verify `Retry-After`. Stop immediately once limits are verified to stay well within our budget.

### Phase 3 — Stand-in Calibration
> Implement the stand-in FastAPI service with SSE streaming matching OpenAI chat completions format. Fit parameters from probe data.

### Phase 4 — Pipeline Architecture
> Assemble the four pipeline steps: Understand (with `list_sections` tool), Retrieve (with paper filter branch), Draft (Groq model call), and Verify (local citation check + single Redraft loop).

### Phase 5 — Scheduler & Rate Limiter
> Implement the token bucket scheduler, sliding window rate limiter, bounded queue, retry policy, circuit breaker, and daily budget ledger.

### Phase 6 — Load & Quality Evaluation
> Run steady, burst, sustained overload, halved limit, and exhausted budget scenarios against the stand-in. Run the naive vs policy retry A/B test. Run the 19 evaluation questions across settings A, B, and C against real Groq. Generate tables via `eval.make_tables`.
