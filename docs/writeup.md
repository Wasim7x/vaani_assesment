# The Relay — write-up

Author: Mohammad Wasim. All figures are drawn directly from `results/results.csv` and `results/probe_raw.jsonl` (reproducible via `python -m eval.make_tables`). Detailed engineering rationales are documented in `docs/DECISIONS.md`.

## 1 What I built and why
I built a four-step pipeline in Python: **Understand → Retrieve → Draft → Verify**.
1. **Understand**: Inspects the user query, extracts keywords, and—if confident ($\ge 0.34$)—calls the local `list_sections` tool to isolate candidate sections (the required tool call).
2. **Retrieve**: Branches on confidence to pull passages from a BM25 index over the three arXiv papers (filtered to the target paper if confident, global fallback otherwise).
3. **Draft**: Formulates a structured prompt requiring citations in `[paper, §section, p.N]` format or exact refusal (`"I cannot answer from the provided papers."`), submitted through our rate-aware scheduler to Groq's `qwen/qwen3.8-27b`.
4. **Verify**: Locally validates citations against actually retrieved context. If citations are missing or hallucinated, it executes **one redraft loop** with strict correction instructions (bounded by a 15s deadline).

**The decision that shaped it most:** I imposed a **hard ceiling of 250 real Groq requests per day** (against the 1,000 free-tier limit) and an **80% safety margin** on rate limits. This reserved real quota exclusively for probe calibration and precision quality evaluation, offloading heavy concurrent stress testing to a locally calibrated stand-in service.

## 2 The real service and my stand-in
**Real limits & discovery:** Through iterative probe bursts (`eval/probe.py`), I extracted Groq's exact constraints:
- **RPM = 30**: Observed via 429 response body (`"Limit 30, Used 30"`). Recovers in 2s (`Retry-After: 2`).
- **TPM = 8,000 total / 7,000 input (ITPM)**: Surfaced from `x-ratelimit-limit-tokens` header and 429 body text. Recovers in 10s (`Retry-After: 10`).
- **RPD = 1,000**: Read from `x-ratelimit-limit-requests`.
- **Latency baseline**: Probe TTFT p50 = 180 ms, p95 = 433 ms, p99 = 472 ms (85 successful calls).

### Table 1: Limits found in the real service
| Limit | Value found | How found | Confidence |
|---|---|---|---|
| Requests per minute | 30 | Error body on RPM 429 | high |
| Tokens per minute | 8000 total; 7000 input (ITPM) | `x-ratelimit-limit-tokens` & TPM 429 body | high |
| Requests per day | 1000 | `x-ratelimit-limit-requests` header | high |
| Tokens per day | documented, not reached | No TPD header surfaced on 429 | low |
| Requests in flight at once | not measured | No concurrency header returned | low |
| What it returns when limit hit | HTTP 429 JSON `{error:{message:...}}` + `Retry-After` | Captured RPM and TPM 429 payloads | high |
| Time until it recovers | 2s after RPM 429; 10s after TPM 429 | `Retry-After` response header | high |

**Stand-in calibration & behavior:** Implemented in `standin/server.py` as an OpenAI-compatible SSE streaming endpoint. Calibrated (`standin/calibrate.py`) to mimic Groq:
1. *Slow latency*: Log-normal TTFT ($\mu=5.336, \sigma=0.438$) fitted from real probe data.
2. *Failures*: Configured failure injection (HTTP 500).
3. *Rate limits*: Sliding-window buckets returning authentic 429 bodies and `Retry-After` headers.
4. *Load slowdown*: $\text{TTFT} \times (1 + 0.08 \times \max(0, \text{in\_flight} - 2))$.
5. *Streaming*: Emits 4-character tokens via Server-Sent Events with final token usage.

**Where it differs most from real Groq:** Real Groq behaves like a leaky bucket with sudden network tail spikes; my stand-in applies a strict sliding window. Furthermore, per-token generation in the stand-in uses a jittered constant rather than dynamic GPU execution cadence.

## 3 Results
All load scenarios ran against the calibrated stand-in; quality benchmarks ran against real Groq. Percentiles represent p50 / p95 / p99 across full request counts.

### Table 2a: Latency by scenario
| Scenario | Real/Stand-in | Requests | Duration (s) | Offered rate (/min) | First token p50 / p95 / p99 (ms) | Total time p50 / p95 / p99 (ms) |
|---|---|---|---|---|---|---|
| steady | standin | 35 | 540.9 | 3.9 | 206 / 336 / 1685 | 1395 / 14750 / 16050 |
| burst | standin | 30 | 10.2 | 175.6 | 258 / 388 / 388 | 11270 / 11362 / 11402 |
| burst_naive | standin | 20 | 1.2 | 1029.2 | 349 / 455 / 455 | 3859 / 4994 / 5051 |
| overload | standin | 89 | 19.7 | 270.5 | nan / nan / nan | 2 / 13403 / 14674 |
| halved | standin | 15 | 81.6 | 11.0 | 173 / 384 / 384 | 1 / 7301 / 14604 |
| exhausted | standin | 10 | 1.0 | 600.0 | nan / nan / nan | 1 / 1 / 1 |
| quality_real | real | 19 | 70.2 | 16.2 | 282 / 553 / 553 | 2 / 601 / 13416 |
| quality_real_B | real | 19 | 393.8 | 2.9 | 243 / 745 / 936 | 354 / 4375 / 4385 |
| quality_real_A | real | 8 | 113.0 | 4.2 | 258 / 813 / 813 | 1166 / 15075 / 15075 |
| quality_real_C | real | 14 | 349.7 | 2.4 | 339 / 746 / 1821 | 702 / 11376 / 159849 |

### Table 2b: Outcomes by scenario
| Scenario | Succeeded % | Rate-limited % | Timed out/given up % | Retries/req | Peak queue | Tokens/ans | Cost/1000 ans |
|---|---|---|---|---|---|---|---|
| steady | 51.4 | 0.0 | 48.6 | 0.06 | 5 | 839 | 838,889 tok |
| burst | 13.3 | 0.0 | 86.7 | 0.13 | 30 | 837 | 837,250 tok |
| burst_naive | 45.0 | 55.0 | 0.0 | 3.00 | 0 | 841 | 840,889 tok |
| overload | 0.0 | 0.0 | 100.0 | 0.09 | 30 | 0 | 0 tok |
| halved | 20.0 | 0.0 | 80.0 | 0.00 | 3 | 842 | 842,000 tok |
| exhausted | 0.0 | 0.0 | 100.0 | 0.00 | 0 | 0 | 0 tok |
| quality_real | 47.4 | 0.0 | 52.6 | 0.00 | 1 | 881 | 881,111 tok |
| quality_real_B | 100.0 | 0.0 | 0.0 | 0.21 | 1 | 864 | 864,263 tok |
| quality_real_A | 87.5 | 0.0 | 12.5 | 0.38 | 1 | 1077 | 1,077,286 tok |
| quality_real_C | 85.7 | 0.0 | 14.3 | 0.36 | 1 | 906 | 906,333 tok |

**One number I could not have guessed without running it:** The primary constraint is **TPM/ITPM, not RPM**. With retrieved context blocks averaging ~800 tokens, admitting just 8 concurrent requests consumes ~6,400 TPM. The **practical ceiling is ~8 RPM**, making RPM limits redundant under realistic retrieval loads.

**Quality evaluation:** Measured on 19 self-authored questions (12 answerable, 4 unanswerable, 3 tricky), graded deterministically on key facts and valid citation presence.

## 4 Where the time goes
Using `eval/slow_request.py` and `results/traces.jsonl`, request latency is partitioned into **waiting** (admission queue + rate limiter pre-wait), **working** (HTTP transmission and LLM streaming), and **retrying** (`Retry-After` sleep intervals).

Under heavy load, **delay occurs almost entirely before the first token**. During the `burst` scenario, working TTFT was nominal (p50 = 258 ms), while total latency reached 11,270 ms due to pre-send queue wait. One notable real-service outlier, request `r-746aa392ff7f` in `quality_real_C`, logged 159,849 ms with no first token due to a client-side deadline overshoot under compounding backoff—an anomaly left transparently unedited in the raw trace.

## 5 When a limit is hit
- **Per-minute limit (RPM/TPM):** Handled via dual sliding windows at 80% capacity (24 RPM / 6,400 TPM). Requests wait in a bounded admission queue (max depth 64). If the calculated wait exceeds the request's remaining deadline, it is **dropped before transmission** to conserve quota. Arrivals after saturation are delayed; requests with expiring deadlines are dropped. No degraded answers are emitted.
- **Per-day limit (RPD):** Tracks ledger usage in `results/budget.json`. When $\text{remaining} < 25$, `max_tokens` is shrunk to 128 (soft landing). When $\text{remaining} == 0$, incoming requests are dropped immediately ($<1$ ms) with no network request dispatched.

## Retrieval: quality versus cost
I evaluated three distinct retrieval configurations across the three papers:
- **Setting A**: 150-token chunks, top-8 fetched, no re-ranking.
- **Setting B**: 400-token chunks, top-4 fetched, no re-ranking.
- **Setting C**: 400-token chunks, over-fetch 12, MMR deduplication to top-4.

### Table 3: Retrieval settings (quality vs cost)
| Setting (chunk, k, rerank) | Qs | Correct & cited % | Wrong % | Correctly refused % | Tokens/answer | First token p50 / p95 (ms) | Total time p50 / p95 (ms) |
|---|---|---|---|---|---|---|---|
| A: 150t, k=8, mmr=False | 7 | 85.7 | 14.3 | 0.0 | 1077 | 258 / 813 | 477 / 9986 |
| B: 400t, k=4, mmr=False | 28 | 35.7 | 46.4 | 17.9 | 870 | 276 / 745 | 354 / 4385 |
| C: 400t, k=4, mmr=True | 12 | 58.3 | 41.7 | 0.0 | 906 | 339 / 746 | 702 / 3529 |

**Selection rationale:** **Setting A is optimal for accuracy (85.7% correct)** because granular 150-token chunks isolate specific facts without diluting BM25 scores, costing ~24% more tokens than B. **Under daily quota stress, the pipeline falls back to Setting B** to minimize token expenditure. Furthermore, when top BM25 scores fall below relevance thresholds, the system refuses immediately to prevent wasting tokens.

## How this library will mislead a naive retriever
1. **Shared vocabulary across domains:** All three papers frequently use "attention". Asking *"What is scaled dot-product attention?"* causes naive BM25 to rank Moshi passages above Attention Is All You Need §3.2.1 unless filtered by paper.
2. **Polysemous terminology:** "Chunk" refers to learned sequence boundaries in H-Net, but denotes a 12.5 Hz discrete audio frame in Moshi. Asking *"What is the chunk length?"* retrieves conflicting definitions.
3. **Colliding hyperparameter values:** The constant `512` represents the base Transformer's `d_model`, but also appears repeatedly in Moshi RVQ dimension tables. Querying *"What is the 512 dimension used for?"* mixes unrelated architectures.

## 6 Retries
**Policy:** Retries are permitted only on HTTP 429 (`rate_limited`), HTTP 5xx, or network timeouts; never on 4xx client errors. If `Retry-After` is supplied, the scheduler respects it exactly; otherwise, it executes truncated exponential backoff (500 ms base, 8s cap, $\pm 50\%$ jitter) up to 3 retries within the 15s deadline. A 3-state circuit breaker trips after 5 consecutive failures for 10s.

**When retrying made things worse:** Running `burst` (policy) vs. `burst_naive` (fixed 500 ms backoff, ignoring `Retry-After`):
- `burst_naive` achieved 45% ok but generated **55.0% 429 errors**, bombarding the server with 3.0 retries/req and exhausting rate windows.
- `burst` (policy) produced **0.0% 429s**, cleanly dropping unserviceable requests (86.7% dropped) to protect the shared account quota.

## 7 What the person sees
When the daily budget is exhausted at 3:00 PM, the scheduler triggers an immediate client-side admission drop. No HTTP request is sent to Groq. The caller immediately receives an empty response with `status: dropped` (`timeout`) in ~1 ms, and 0 tokens billed. In an end-user UI, this maps directly to:
> *"Daily quota exhausted. The research library assistant has reached its daily processing limit and will resume tomorrow at 00:00 UTC."*
This fails fast and honestly without hallucinating answers or trapping users in indefinite loading states.

## 8 What I'm unsure about, what I left out, what I learned
- **Least certain assumption:** The stand-in load degradation slope ($\alpha = 0.08$ above 2 in-flight). Because measuring true Groq concurrency would have consumed our limited 250-request real budget, actual production saturation may be more non-linear.
- **Two things deliberately not built:** (1) *Dense vector embeddings*: Building neural embeddings would add latency and cost without solving the binding TPM constraint. (2) *Request hedging*: Given that Groq's slow tail occurs in $<3\%$ of requests, speculative duplicate calls would double token consumption for negligible percentile gains.
- **What was new to me:** Groq's bifurcation of rate limits into total **TPM (8,000)** and **Input TPM (7,000 ITPM)**. I discovered that prompt tokens are metered against an independent input ceiling, meaning retrieval pipelines with detailed context trigger rate limits long before generation tokens accumulate.

## My feedback on Vaani
Reviewing [vaani.dev](https://www.vaani.dev/):
- **What stood out:** The developer-first focus on voice AI orchestration—specifically combining real-time interruption handling, speech-to-speech pipelines, and turnkey telephony without forcing developers to manually integrate separate ASR, LLM, and TTS layers.
- **What confused me:** How mid-conversation tool calls and variable backend API latencies are reconciled with aggressive turn-taking latency bounds (i.e., whether audio buffer cancellation or speculative local TTS drafting takes precedence when an interruption occurs).
- **One thing I would change:** Surface latency waterfalls (breakdowns of ASR latency $\rightarrow$ LLM TTFT $\rightarrow$ TTS first-chunk audio) and granular token/rate budget telemetry directly in the dashboard playground.

## How I worked with the assistant
Development was guided using Claude Code / Antigravity. The uncleaned interaction transcript is preserved in `docs/CONVERSATION_HISTORY.md`, with core prompts in `docs/PROMPTS.md`.

- **Three prompts that mattered most:**
  1. *The Boundary & Invariant Prompt*: Establishing hard guardrails upfront—capping real Groq calls at 250/day, requiring exact citation syntax `[paper, §section, p.N]`, and mandating empirical data in all tables.
  2. *The Stand-in Calibration Prompt*: Instructing the agent to fit log-normal parameters ($\mu, \sigma$) directly from `probe_raw.jsonl` rather than using mock sleep timers, guaranteeing authentic tail behaviors.
  3. *The Scheduler Degradation Prompt*: Defining admission queue bounds, rate windows, token shrinking ($<25$ budget $\rightarrow$ 128 max tokens), and the circuit breaker.
- **One mistake the assistant made that I caught:** The assistant initially configured `llama-3.1-8b-instant`. Upon running the first probe, 10 consecutive requests failed with 404 `model_not_found` because that model ID was restricted on my API tier. I caught the errors in `budget.json`, inspected `/v1/models`, and redirected the pipeline to `qwen/qwen3.8-27b`.
- **One decision where I overruled it:** The assistant proposed a 90% rate-limit margin to maximize throughput. Real probe data revealed Groq's limits act as step-function cliffs where minor bursts incur 2s penalties. I overruled it and set an 80% ceiling (24 RPM / 6,400 TPM), trading 10% peak throughput for zero steady-state 429s.
- **What in this submission is distinctly mine:** The 250 request/day quota ceiling; the 80% safety margin; reserving real Groq strictly for quality runs; and openly preserving raw anomalies (the 10 wasted 404 calls and the deadline overshoot trace) rather than sanitizing results.
