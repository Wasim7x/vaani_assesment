# The Relay — write-up

Author: Mohammad Wasim. All numbers come from `results/results.csv`
and `results/probe_raw.jsonl`; `python -m eval.make_tables`
rebuilds `docs/tables.md`. Reasoning for each choice is in
`docs/DECISIONS.md`.

Short words: **TTFT** = time to first token. **p50/p95/p99** =
median / slow / very-slow. **RPM / TPM** = requests / tokens per
minute. **429** = HTTP "rate limited".

## 1. What I built and why

A four-step pipeline: **Understand → Retrieve → Draft → Verify**.
Understand picks a paper and calls a local `list_sections` tool.
Retrieve pulls top passages from a BM25 index of the three papers.
Draft is one Groq call that must cite or refuse. Verify checks the
citation; if bad, one Redraft (the required loop). A scheduler
knows the measured limits, honors `Retry-After`, and drops stale
requests.

The choice that shaped everything: **I capped real Groq at 250
requests per day** (free tier allows 1,000, but the quota is
shared and running out is part of the test). Heavy load ran against
a stand-in. See DECISIONS.md D-2.

## 2. The real service and my stand-in

**Limits (full Table 1 in `docs/tables.md`):**
RPM = **30** (429 body). TPM = **8,000 total; 7,000 input-only
(ITPM)** — two numbers; output-only budgeting under-counts ~12% on
long prompts. RPD = **1,000**. Recovery: **2 s after RPM 429, 10 s
after TPM 429**.

**Real latency** (45 OK probes): TTFT **p50 = 189 / p95 = 391 / p99 =
1,913 ms** (p99 is one spike).

**Stand-in.** Same URL as Groq. It can (1) be slow — log-normal TTFT,
`mu = 5.336, sigma = 0.438`, fitted from the probe; (2) fail with
HTTP 500; (3) refuse with a 429 matching Groq's body and
`Retry-After`; (4) slow under load, TTFT × `(1 + 0.08 × max(0,
in_flight − 2))`; (5) stream in ~4-char chunks. Fit:
`standin/calibrate.py`.

**Biggest gaps vs real:** one log-normal cannot reproduce Groq's
1,913 ms p99; per-token delay is a jittered constant; my 429s are
a strict sliding window while Groq looks like a leaky bucket.

## 3. Results

Tables 2a and 2b in `docs/tables.md`. All load scenarios are
**stand-in**; real-Groq rows are the quality pass. Percentiles are
**p50, p95, p99** across each scenario's full request count (steady
35, burst 30, overload 89 over ~20 s, halved 15, exhausted 10).

**One number I could not have guessed:** the binding constraint is
**TPM, not RPM**. At ~800-token prompts, 10 in-flight needs 8,000
TPM — the measured limit. **Practical ceiling: ~8 RPM**, not 30.

**Quality.** 19 self-written questions (12 answerable, 4
unanswerable, 3 tricky), graded deterministically. Per-setting
results in Table 3.

## 4. Where the time goes

`eval/slow_request.py` splits a trace into **waiting** (queue +
rate pre-wait), **working** (sum of attempt HTTP time), **retrying**
(sum of `Retry-After`). On overload the delay is **before first
token**: once started, `first_token_ms` is in the normal 200 ms
range, so wall-clock minus TTFT equals queue wait. One real case:
`r-746aa392ff7f` in `quality_real_C` ran **159,849 ms with no first
token** — a deadline-overshoot bug I left visible (DECISIONS.md).

## 5. When a limit is hit

**Per-minute.** Requests queue (depth 64), then leave as the
sliding window frees capacity at 80% of the measured limit. If the
wait exceeds the deadline, the request is **dropped before send**.
Who waits: arrivals after the window filled. Who is dropped:
whoever's deadline is shorter than the wait. No one is given a
weaker answer at this limit.

**Per-day.** At `remaining < 25`, `max_tokens` is capped at 128. At
`remaining == 0`, the scheduler drops without any HTTP call
(`relay/scheduler.py: submit()`).

## Retrieval: quality versus cost

**A** = 150-token chunks, top-8, no re-rank. **B** = 400-token
chunks, top-4, no re-rank. **C** = 400-token chunks, over-fetch 12
then MMR-dedupe to 4. On real Groq (Table 3): **A = 85.7% correct
(7 Qs, 1,077 tok/ans); B = 35.7% (28 Qs, 870 tok/ans); C = 58.3%
(12 Qs, 906 tok/ans)**. A wins at ~24% more tokens. **Choice: A by
default; fall back to B when the daily budget is close** (fewer
tokens per answer leaves room for more). Near the limit I also
refuse when the top BM25 score is low rather than pay tokens on
weak passages.

## How this library will mislead a naive retriever

1. **Shared vocabulary.** "Attention" is in all three. Example:
   *What is scaled dot-product attention?* — BM25 ranks Moshi above
   Attention §3.2.1 unless the paper is narrowed.
2. **Overloaded terms.** "Chunk" = learned segment in H-Net, a 12.5
   Hz audio frame in Moshi. Example: *What does chunk mean in
   dynamic chunking?* mixes both.
3. **Recurring numbers.** `512` = Attention's `d_model`, also in
   Moshi RVQ figures. Example: *What is d_model in the base
   Transformer?* can retrieve Moshi tables.

## 6. Retries

Retry on `rate_limited`, `timeout`, 5xx; never non-429 4xx. Honor
`Retry-After`; else exponential backoff 500 ms → cap 8 s, jitter
0.5–1.5×. **3 retries/request**, hard stop at the 15-s deadline.
Breaker opens after 5 consecutive 5xx/timeout for 10 s.

**A case where retrying made things worse.** `--naive` ignores
`Retry-After` and uses fixed backoff. Same burst:
- `burst` (policy): **13.3% ok, 0% 429, 86.7% dropped**, total p50
  11,270 ms.
- `burst_naive`: **45.0% ok, 55.0% 429, 0% dropped**, total p50
  3,859 ms.

Naive gets more 200s and finishes faster, but half its traffic is
429s — on a shared account that eats the day's quota and can
trigger harder rate shaping. Policy protects the account at the
cost of raw throughput.

## 7. What the person sees at 3 pm with the daily budget gone

[WASIM TO WRITE: the exact sentence the user sees when
`daily_budget_exhausted` fires. **Right now the code returns an
empty answer with `status = timeout` in ~1 ms (no HTTP call).**
Either wire a friendly message into `relay/pipeline.py` or
describe the current behavior truthfully. See DECISIONS.md D-7.]

## 8. What I'm unsure about, what I left out, what I learned

**Least certain assumption.** The stand-in's load-slowdown formula
(`alpha = 0.08` above 2 in-flight). I did not measure Groq's TTFT
under concurrency — too much of my 250-request budget. If this is
wrong, the overload scenario's shape is wrong.

**Two things I did not build.** (1) A dense retriever — costs TPM,
and TPM is the binding constraint. (2) Hedging — Groq's slow tail
is 1 in 45; hedging doubles TPM for ~2% of cases.

**One thing that was new to me, in my own words:**
[WASIM TO WRITE in my own voice. Hint: Groq meters input (ITPM
7,000) separately from total TPM 8,000, so long prompts 429 before
TPM is spent.]

## Your feedback on Vaani

[WASIM TO WRITE a few honest lines on https://www.vaani.dev/ —
what stood out, what confused me, one thing I would change.]

## How I worked with the assistant

I used Claude Code. The uncleaned conversation is in
`docs/CONVERSATION_HISTORY.md` [WASIM TO EXPORT — paste verbatim].
Three key prompts are in `docs/PROMPTS.md` [WASIM TO ADD THE THIRD].

**One mistake the assistant made that I caught:**
[WASIM TO WRITE in my own voice. Hint: it picked
`llama-3.1-8b-instant`; 10 requests 404'd. See DECISIONS.md D-12.]

**One decision I overruled:**
[WASIM TO WRITE in my own voice. Hint: it proposed 90% safety
margin; I used 80% after the probe. See DECISIONS.md D-6.]

**What is distinctly mine:**
[WASIM TO WRITE in my own voice. Candidates: the 80% margin, the
250/day ceiling, keeping the stand-in honest about its gaps,
reserving real Groq for quality not load.]
