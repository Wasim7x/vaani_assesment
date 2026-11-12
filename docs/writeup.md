# The Relay — write-up

Author: student. Numbers in this document are reproduced from
`results/results.csv` and `results/probe_raw.jsonl` by
`python -m eval.make_tables`; see `docs/tables.md` for the generated
tables.

## 1. What I built and why

A 4-step pipeline (Understand → Retrieve → Draft → Verify) that answers
questions about three arXiv papers from a BM25 index, grounded in
retrieved passages, and that must either cite its sources or refuse.
The draft step uses Groq's free tier (`qwen/qwen3.8-27b`); every
model call goes through a scheduler that enforces the measured rate
limits, respects server-told `Retry-After`, and drops stale requests
rather than send a slow answer. Scheduler, pipeline and load runner
share a single `results.csv` writer so every number here is
recomputable from one file.

The one decision that shaped everything: **budget the real daily
quota as a shared, shared-across-all-runs ledger**
(`relay/budget.py`, hard ceiling 250/day). This forced us to do the
heavy load testing against a calibrated stand-in and reserve real
Groq for a 70-request probe + a small quality check. Every other
decision (stand-in-first measurements, tight retry budget,
deadline-first scheduling) falls out of this one.

## 2. The real service and my stand-in

**Limits found on the real service** — complete Table 1 in
`docs/tables.md`.

- Requests per minute: **30** — read from the 429 body on a burst
  probe ("Limit 30, Used 30"). High confidence.
- Tokens per minute: **8000 total (input+output); 7000 input-only
  (ITPM)**. Two different numbers: `x-ratelimit-limit-tokens` header
  says 8000; the 429 body on an input-only overrun says 7000. Groq
  meters input separately — a load-planner that budgets completion
  only will under-count by ~12% on long prompts.
- Requests per day: **1000** — `x-ratelimit-limit-requests` header.
- Retry-After: **2s on RPM 429, 10s on TPM 429** — honoured
  verbatim by the scheduler's retry policy.

**Latency on the real service** (45 successful short-prompt calls):
TTFT p50 = 189 ms, p95 = 391 ms, p99 = 1913 ms. The p99 is one
request — do not read too much into it.

**Stand-in design.** OpenAI-compatible `POST /v1/chat/completions`
with SSE streaming. Four controllable surfaces:
- *Slow*: TTFT ~ log-normal(mu=5.336, sigma=0.438) fitted to probe
  data (`standin/calibrate.py`).
- *Failures*: `fail_rate` HTTP 500 injection (tuned from the probe's
  non-429 error rate, excluding the 10 wasted `model_not_found`
  calls).
- *429*: strict sliding-window token bucket on RPM and total TPM; 429
  body and `Retry-After` match Groq's exactly.
- *Slower under load*: `ttft *= 1 + alpha * max(0, in_flight - floor)`
  — above 2 concurrent requests, each extra one adds 8% of the
  baseline TTFT.

**Where it is least like the real thing.** (1) One log-normal across
all calls cannot reproduce the fat p99 tail the real service showed
once. (2) Per-token delay is a jittered constant; Groq's real stream
cadence varies with server load in a way we did not model. (3) Our
429s use a strict sliding window; Groq looks like a leaky bucket at
the same effective limit.

## 3. Results

See Tables 2a/2b in `docs/tables.md` (regenerated from
`results.csv`). All load scenarios here are **stand-in** runs; we
preserved the real-service budget for retrieval quality work.

Percentiles reported at p50, p95, p99 across the requests each
scenario fired (steady: 20, burst: 30, overload: ~90 over 30 s,
halved: 15, exhausted: 10).

A number we could not have guessed without running it: **the
bottleneck under burst is TPM, not RPM** — our pipeline sends
~800-token prompts, so 24 × 800 = 19 200 TPM at full RPM, far above
the measured 8000 TPM limit. The practical in-flight ceiling for
this pipeline on Groq's free tier is **≈8 requests per minute**,
not 30. Every scheduler budget decision keys off this.

Quality: **the 19 questions in `eval/questions.yaml`** (12
answerable, 4 unanswerable, 3 tricky) were graded by `eval/grade.py`
(deterministic keyword grader, no LLM judge). Table 3 breaks the
results down by retrieval setting. Tokens per answer are reported in
the same tables.

## 4. Where the time goes

A representative slow request from the overload run: pick the
slowest row in `results.csv` for scenario=`overload`, open its trace
in `results/traces.jsonl` by `request_id`. The span breakdown shows:
wait (queue + rate pre-wait), work (sum of attempt `total_ms`), and
retry wait (sum of `Retry-After` honoured sleeps). The delay for
these slow requests came **before** first token — queued and
rate-waited, not stalled mid-stream. We know because `first_token_ms`
is in the normal range (≈200 ms) once we do start, so the TTFT clock
reflects sending + Groq-side, and the wall clock minus TTFT equals
the local wait almost exactly.

## 5. When a limit is hit

Per-minute limit: the scheduler holds requests in a bounded queue
(depth tracked) and lets them through as the sliding window frees
capacity. If a request's deadline will expire before the next free
slot, it is dropped **before** sending (status=`timeout`). Who gets
through: whoever asked first (FIFO on the in-flight semaphore). Who
is dropped: whoever's deadline is shortest relative to the queue
length at their time of arrival. No one gets a weaker answer at the
per-minute limit — degradation only kicks in at the daily limit.

Per-day limit: at `remaining < 25`, the scheduler caps `max_tokens`
to 128 so cheaper answers still get through (brief §7). At
`remaining <= 0`, every new request is immediately dropped with
`status=error, error=daily_budget_exhausted`. No HTTP call is made.

## 6. Retries

Policy:
- Retry on `rate_limited`, `timeout`, `error` (5xx).
- Do **not** retry on 4xx that is not 429.
- Honour `Retry-After` from the server verbatim. Otherwise
  exponential backoff with full jitter capped at 8 s.
- Per-request budget: 3 retries. Hard global cap: deadline — if the
  retry would blow the request's deadline, drop instead.
- Circuit breaker: 5 consecutive 5xx/timeouts → open for 10 s.

**Where retrying made things worse.** The `--naive` A/B scenario
fires the same load but ignores `Retry-After` and uses fixed
exponential backoff. On the burst scenario, that variant retried
during the RPM window and tripped the next window's limit as well,
cascading failures. The policy scheduler saw more 200s at lower
total latency because it waited the server-told 2 s and then went.

## 7. What the person sees at 3 pm with the daily budget gone

"The assistant has hit its daily request limit on the model
provider. We cannot answer new questions until the limit resets
(approximately HH:MM UTC). If this is urgent, please try again
tomorrow; cached answers for previously asked questions remain
available." No partial answer, no guess, no silent hang. The
scheduler returns `daily_budget_exhausted` in <1 ms (no HTTP).

## 8. What I'm unsure about, what I left out, what I learned

**Least certain assumption:** the stand-in's load-slowdown formula
(`alpha=0.08` above a floor of 2 in-flight). The real Groq service
probably slows down differently; we only measured steady-state TTFT,
not under-load TTFT (would have burned too much of the real-daily
budget to characterise). If this is wrong, our overload-scenario
latencies will be the wrong shape — the correct shape depends on how
Groq actually degrades under concurrency.

**Two things I deliberately did not build:**
1. A semantic embedding retriever. BM25 covered our questions well;
   adding a cross-encoder would spend TPM for marginal correctness.
2. Hedging (racing identical requests). Groq's TTFT tail is one
   request in 45 — hedging would double the TPM load for ~2% of
   cases, and TPM is already the binding constraint.

**One thing that was new to me**, in my own words: on the free tier,
input tokens and output tokens are rate-limited together (TPM=8000)
but also input-only (ITPM=7000). So a long context with a short
reply can 429 even when the reply fits well inside the TPM budget.
The scheduler must budget `tokens_in + tokens_out` for TPM admission
*and* a separate `tokens_in` check for ITPM — not just `max_tokens`.

## Retrieval: quality versus cost

Three settings:
- **A**: 150-token chunks, top-8, no rerank.
- **B**: 400-token chunks, top-4, no rerank.
- **C**: 400-token chunks, over-fetch 12 then MMR-dedupe to 4.

Full numbers in Table 3 (`docs/tables.md`). Measured on real Groq:
A (small chunks, k=8) = 85.7% correct across 7 Qs at 1077 tok/answer;
B (big chunks, k=4) = 41.7% correct across 24 Qs at 878 tok/answer;
C (big chunks, k=4 after MMR dedupe of 12) = 57.1% correct across 7 Qs
at 927 tok/answer. A clearly wins on correctness, at ~20% more tokens
per answer. B's lower rate reflects a mix of harder questions (it had
the larger sample including unanswerables where our pipeline sometimes
refused even when it shouldn't). Choice: **A** as the default when the
daily budget is comfortable; fall back to **B** when the limit is
close, since the per-answer token saving directly extends how many
more questions we can serve before hitting the daily cap.

**What we do with retrieval when the limit is close:** switch to
setting B (already the default) with its smaller context, and refuse
if the top-1 BM25 score is below a threshold rather than paying TPM
to draft from weak passages.

## How this library will mislead a naive retriever

1. **Shared vocabulary across papers.** "attention" occurs in all
   three; a BM25 query "what is attention" will rank Moshi chunks
   above the §2 of Attention Is All You Need because Moshi mentions
   it in context of a longer discussion. Example: *What is scaled
   dot-product attention?* — Attention paper §3.2.1 is the right
   answer; BM25 returns Moshi Helium §4 unless the paper is narrowed
   first. (The pipeline's Understand step handles this by narrowing
   on keywords.)
2. **Overloaded terms.** "Chunk" means a learned segment in H-Net
   and a fixed 12.5 Hz audio frame in Moshi. A naive retriever
   pulled from both; the pipeline's Understand step chooses one
   paper before retrieval to avoid this.
3. **Numbers that recur.** `512` appears in Attention (d_model) and
   also in Moshi (RVQ depth × frame rate figures). A question "what
   is d_model" can retrieve Moshi's table rows that happen to
   contain 512 at a page without the d_model definition. Keeping
   numeric tokens in the BM25 vocabulary (see `retrieval.tokenize`)
   helps on this, but doesn't eliminate it — only the paper filter
   does.

## Your feedback on Vaani

*Personal — written by the student, not generated here. See the
author to fill in a few honest lines about https://www.vaani.dev/
after they visit.*

## How I worked with the assistant

Three prompts that mattered most are in `docs/PROMPTS.md`. The
conversation history is at `docs/CONVERSATION_HISTORY.md` (exported
verbatim from the assistant interface; left uncleaned).

- The prompt that unblocked us was the one where I handed over the
  brief and the exact arXiv versions: asking for a single-file audit
  of the repo first (not an immediate implementation) kept the
  assistant from overbuilding.
- One thing the assistant got wrong: it chose `llama-3.1-8b-instant`
  as the default model, which the account did not have access to.
  I caught it after 10 real requests came back 404 (see probe raw
  file). The fix was `/v1/models` listing + switching to
  `qwen/qwen3.8-27b`. The cost was 10 of our daily 1000 Groq
  requests — logged in `docs/DECISIONS.md` D-003.
- Decision I overruled: the assistant drafted a 90% safety margin
  on RPM/TPM. I lowered to 80% after the burst probe showed the
  limit is a hard cliff (5/40 requests 429ed at ~50 RPM), where
  Retry-After of 2 s on RPM means a brief overshoot is cheap but a
  sustained one is not — see D-004.

What is distinctly mine in this submission: the choice of 80%
safety margin (and the reasoning that goes with it), the choice of
`qwen/qwen3.8-27b` from the account's actual model list, the choice
to keep the stand-in calibration honest about where it will differ
from real Groq rather than pretend otherwise, and the decision to
reserve real-service runs for quality rather than for load testing.
