# Decisions

This file lists every important choice I made while building The Relay.
Each entry has the same shape: what I decided, why, what it cost me,
what else I thought about, what would make me change it, and where to
find it in the code.

A few short words first, so the rest reads easily:

- **TTFT** = time to first token. How long until the model sends its
  first word back.
- **p50 / p95 / p99** = "half of my requests were faster than this /
  only 5 out of 100 were slower / only 1 out of 100 was slower".
- **RPM** = requests per minute. **TPM** = tokens per minute.
  **ITPM** = input tokens per minute. **RPD** = requests per day.
- **429** = the HTTP code the server sends when a limit is hit.
- **Backoff** = waiting before I try again after a failure.
- **Jitter** = adding a small random amount to a wait so many clients
  do not all retry at the same moment.
- **Chunk** = a short piece of a paper used for search.
- **BM25** = a classic word-overlap search method. Fast, no model
  needed.
- **MMR** = Maximal Marginal Relevance. A way to pick chunks that are
  relevant *and* different from the ones I already picked.

Who decided what:
- "**I decided**" = I made this choice.
- "**Claude Code proposed, I accepted**" = the assistant suggested it
  and I agreed, because the reason made sense and matched what I saw
  in the data.
- "**Claude Code proposed, I changed**" = the assistant suggested
  something and I overruled it.

---

## Decision 1: A four-step pipeline with one tool call, one branch, and one loop

- **What I decided:** Build the pipeline as four steps in this order:
  **Understand → Retrieve → Draft → Verify**. Add a tool call inside
  Understand, a branch in Retrieve, and a short loop between Verify
  and a single Redraft.
- **Why:** The brief asks for at least four steps, one tool call, and
  one branch or loop. I wanted all three in one honest pipeline, not
  bolted on. Understand asks "which paper is this about?" so Retrieve
  can focus on one paper when it is confident, which saves tokens and
  makes citations easier. Verify catches the two bad outputs we
  actually see: no citation at all, and a citation to a paper that was
  never retrieved. The loop only redrafts **once**, so a bad answer
  cannot burn the whole budget.
- **What I gave up:** A little speed. The second draft adds one extra
  LLM call per failure (bounded by the 15-second deadline).
- **Other options I thought about:**
  - A single big prompt with retrieval inside it. Cheaper, but then I
    cannot cite and I cannot refuse cleanly.
  - A chain-of-thought step before Draft. Too many tokens for the
    free tier's TPM budget.
- **What would make me change it:** If more than ~20% of correct
  drafts failed Verify on the first try, I would move the citation
  rules into the Draft prompt more strongly instead of redrafting.
- **How to find it in the code:** `relay/pipeline.py`, function
  `run_pipeline()`. The spans in `results/traces.jsonl` show the
  order: `understand → tool.list_sections → retrieve → draft →
  verify → redraft`.
- **Who decided:** Claude Code proposed the four-step shape; I
  accepted.

---

## Decision 2: The one choice that shaped the whole project — a hard daily budget of 250 real Groq requests

- **What I decided:** Cap real Groq use at **250 requests per day**,
  tracked in a JSON ledger that survives restarts. Do heavy load work
  against a local stand-in instead.
- **Why:** The real free tier allows 1,000 requests per day (`RPD`),
  but **this quota is shared with everything else on the account**,
  and running out is one of the things the brief tests for. I picked
  250 so I had room for mistakes without exhausting the day. The
  `results/budget.json` ledger shows I used 133 of the 250 so far.
- **What I gave up:** I could not measure Groq's behavior under real
  overload. All overload numbers in `results.csv` come from the
  stand-in.
- **Other options I thought about:**
  - No ceiling. Risky. One runaway loop and the day is gone.
  - 500. Would have let me run more real load, but I would still
    have had to use the stand-in for the overload scenario — the
    real service rate-limits at 30 RPM, which is not "overload".
- **What would make me change it:** If I had a paid key with a known
  monthly budget. Then the ceiling becomes "dollars per day", not
  "requests per day".
- **How to find it in the code:** `relay/config.py` ->
  `DAILY_REAL_REQUEST_CEILING = 250`. Enforcement lives in
  `relay/budget.py` (`remaining()` + `record()`) and
  `relay/scheduler.py` (`_policy_call()` drops at 0).
- **Who decided:** I decided.

---

## Decision 3: Retry policy — honor `Retry-After`, bounded tries, exponential backoff with jitter

- **What I decided:**
  - Retry on `rate_limited`, `timeout`, and 5xx errors.
  - Do **not** retry on 4xx that is not 429 (those are client bugs).
  - If the server sends a `Retry-After` header, wait exactly that
    long. Do not use my own backoff.
  - Otherwise, exponential backoff starting at **500 ms**, doubling
    each try, capped at **8 s**, with jitter of 0.5–1.5× so retries
    spread out.
  - Per-request budget: **3 retries**. Hard stop: the request's
    15-second deadline. If the next wait would blow the deadline,
    drop the request instead of sleeping.
- **Why:** Groq tells you the right wait in `Retry-After` — 2 s on
  an RPM 429, 10 s on a TPM 429 (measured in `probe_raw.jsonl`).
  Ignoring that is how you cause cascades. The deadline hard stop
  stops the bill from piling up for someone who has already given up.
- **The case where retrying made things worse — with real numbers:**
  I ran the same burst scenario twice: once with the policy
  scheduler and once with `--naive`, which **ignores `Retry-After`**
  and uses fixed exponential backoff. The results in `results.csv`:
  - `burst` (policy): **13.3%** ok, **0%** rate-limited, **86.7%**
    timed out. Total p50 = 11,270 ms.
  - `burst_naive`: **45.0%** ok, **55.0%** rate-limited, **0%**
    timed out. Total p50 = 3,859 ms.
  Naive got more 200s and finished faster, but **more than half of
  its traffic was 429s**, which on a real shared account would eat
  into the day's quota and could get the account rate-shaped harder.
  The policy scheduler drops requests before they hit the server
  when the deadline cannot be met. **It trades success count for
  not punishing the account.** [WASIM TO WRITE: which of these two
  I would actually ship, and why — a decision I want to make, not
  the assistant.]
- **What I gave up:** Some requests that could have succeeded are
  dropped before they are even sent, because the wait would have
  made them late.
- **Other options I thought about:**
  - Fixed delay only. Simpler but worse: ignores server hints.
  - No cap on retries. Easy way to multiply load during an incident.
- **What would make me change it:** A different server that does
  **not** send `Retry-After`. Then I would have to guess with
  backoff only.
- **How to find it in the code:** `relay/scheduler.py`,
  `_policy_call()` lines around the attempt loop (search for
  `backoff_base_ms`). The naive version is `_naive_call()` in the
  same file.
- **Who decided:** Claude Code proposed; I accepted after seeing the
  two burst runs.

---

## Decision 4: 15-second deadline, drop before send if the wait is too long

- **What I decided:** Every request has a 15-second deadline from the
  moment the user clicks. At every gate (rate wait, in-flight queue,
  retry wait), if the time left is less than the time I would have
  to wait, I **drop the request without calling the model** and mark
  it `timeout`.
- **Why:** The brief says people give up sooner than you expect. An
  answer at 30 seconds is worse than a quick "sorry, try again",
  because by then the user has already left and the tokens are still
  spent. 15 s matches the p95 TTFT target I set.
- **What I gave up:** "Nobody turned away" — one of the four goals
  the brief said we cannot all have. I turn people away rather than
  send a slow answer.
- **Other options I thought about:**
  - No deadline. Would push p99 into tens of seconds under load.
  - 30 s deadline. Would include more requests but miss the "feels
    broken" threshold most users have.
- **What would make me change it:** If the app's use case changed to
  batch work (reports, overnight), the deadline would go up.
- **How to find it in the code:** `relay/scheduler.py`, every
  `deadline_left_ms()` check in `_policy_call()`.
- **Who decided:** Claude Code proposed; I accepted.

---

## Decision 5: Bounded queue and in-flight limit

- **What I decided:**
  - `max_in_flight = 4`: at most 4 requests talk to the server at
    once.
  - `max_queue = 64`: at most 64 requests wait to talk to the
    server. Over that, drop immediately.
- **Why:** The real service has `RPM = 30` and `TPM = 8,000`. At my
  typical prompt of ~800 tokens, 10 in-flight would blow the TPM
  budget. 4 is safe room. The queue cap is there so a traffic spike
  does not quietly build a 10,000-deep backlog.
- **What I gave up:** Some throughput headroom during very short
  bursts.
- **Other options I thought about:**
  - Unbounded queue. Fails loudly later instead of early.
  - `max_in_flight = 8`. Fine for short prompts; dangerous for mine.
- **What would make me change it:** Shorter prompts (say under 300
  tokens) would let me raise `max_in_flight` to 8.
- **How to find it in the code:** `relay/scheduler.py`,
  `asyncio.Semaphore(cfg.max_in_flight)` and the queue check in
  `_policy_call()`.
- **Who decided:** Claude Code proposed; I accepted.

---

## Decision 6: What happens at the per-minute limit

- **What I decided:** Use a sliding-window token bucket on **both**
  RPM and TPM, at **80%** of the measured limit (24 RPM / 6,400 TPM
  effective). When a request would push either over its budget, wait
  until there is room. If the wait is longer than the deadline, drop
  instead of waiting.
- **Why:** Measured in Phase 2: real RPM = 30 (hard cliff —
  `probe_raw.jsonl` shows 5 of 40 requests returning 429 at 50 RPM).
  80% leaves room for small overshoots that the server tolerates,
  and keeps me from losing requests to 429 under normal load. Who
  gets delayed: whoever would push us over budget. Who gets dropped:
  whoever's deadline is shorter than the needed wait. No one gets a
  weaker answer — degradation only kicks in at the daily limit.
- **What I gave up:** About 6 RPM of possible throughput at peak.
- **Other options I thought about:**
  - 90% margin (more throughput, more 429s). Too close to the cliff.
  - 50% margin (never 429). Wastes free-tier capacity.
- **What would make me change it:** If I saw repeated 429s while
  still inside the 80% budget, that would mean Groq's own counting
  disagrees with mine — I would drop to 70%.
- **How to find it in the code:** `relay/scheduler.py`,
  `SlidingWindow` class and the `rate pre-wait` block in
  `_policy_call()`. Effective limits set from
  `relay/config.py:SchedulerConfig.safety_margin = 0.80`.
- **Who decided:** Claude Code proposed 90%; I changed it to 80%
  after reading the probe.

---

## Decision 7: What happens at the per-day limit

- **What I decided:**
  - When `remaining < 25` real requests: **shrink** `max_tokens` to
    128, so the last few answers still get through, but shorter.
  - When `remaining == 0`: **drop the request immediately**. No HTTP
    call. Return `daily_budget_exhausted`.
- **Why:** The brief asks what the system does when the daily budget
  is gone. Dropping the HTTP call at the client saves tokens *and*
  the person's time. Shrinking the output first is a soft landing
  for the last 25 requests, so the service does not go from "fine"
  to "nothing" in one step.
- **What I gave up:** The last few answers are shorter than normal.
  The reader may not notice; a careful reader might.
- **The exact message the user sees:** [WASIM TO SEE SECTION 5 OF
  WRITEUP AND DECIDE.] **Right now the code returns an empty answer
  string with `status = timeout`, not the friendly message in
  `writeup.md §7`.** The friendly message is written in the writeup
  but no code emits it. I need to either (a) add the message to
  `relay/pipeline.py` where the scheduler returns
  `daily_budget_exhausted`, or (b) change the writeup to describe
  what actually happens (empty answer, 1 ms, no HTTP call).
- **Other options I thought about:**
  - Serve a cached answer. I did not build a cache (see "Things I
    did not build").
  - Serve a longer apology with a wait time. Costs tokens we do not
    have.
- **What would make me change it:** A cache of previous answers.
  Then the "gone" case can still serve repeat questions.
- **How to find it in the code:** `relay/scheduler.py`, the
  `remaining <= 0` branch in `submit()` and the `remaining < 25`
  branch right above it. Budget ledger: `relay/budget.py`.
- **Who decided:** Claude Code proposed the "soft landing"; I
  accepted. The friendly message is mine, but I did not wire it to
  code yet.

---

## Decision 8: Three retrieval settings, and which one I chose

- **What I decided:** Try three settings, measure quality, pick one:
  - **A** — 150-token chunks, top-8, no re-rank.
  - **B** — 400-token chunks, top-4, no re-rank.
  - **C** — 400-token chunks, over-fetch 12 then MMR-dedupe down to 4.
  I measured quality on **real Groq** across all three. Table 3 of
  `docs/tables.md` has the numbers.
- **Why:**
  - A trades tokens for coverage.
  - B is cheap and focused, but misses answers that straddle a
    chunk.
  - C costs the same as B at draft time but should pick more
    complementary passages.
- **What the numbers actually said (from `results.csv`, not the
  stale `tables.md`):**
  - A: n=7, **85.7% correct**, 1,077 tokens/answer, TTFT p50 258 ms.
  - B: n=28, 35.7% correct (17.9% refused correctly), 870 tokens.
  - C: n=12, 58.3% correct, 906 tokens.
  **A wins on correctness. It costs ~24% more tokens per answer.**
- **My choice:** **A by default when the daily budget is
  comfortable. Fall back to B when the budget is close**, because B
  uses fewer tokens per answer so more questions fit in what is
  left. Near the limit I also refuse when the top BM25 score is low
  rather than pay tokens on weak passages.
- **What I gave up:** B's lower token cost in the common case.
- **Other options I thought about:**
  - A cross-encoder re-ranker. Would cost more TPM for probably
    small gains.
  - Semantic embeddings. Also more TPM; see "Things I did not
    build".
- **What would make me change it:** If A's win narrowed to <5
  percentage points at the same token cost, I would default to B.
- **How to find it in the code:** `relay/config.py:
  RETRIEVAL_SETTINGS` and `relay/retrieval.py: retrieve()`.
- **Who decided:** Claude Code proposed the three settings; I ran
  all three and picked A from the numbers.

---

## Decision 9: The stand-in service — what it does and how I calibrated it

- **What I decided:** A FastAPI service at `POST
  /v1/chat/completions` that acts like Groq's OpenAI-compatible
  endpoint. It can do five things:
  1. **Be slow:** TTFT drawn from a log-normal with `mu = 5.336,
     sigma = 0.438`, plus a 3% chance of adding 1,500 ms.
  2. **Fail:** inject HTTP 500 at `fail_rate`.
  3. **Refuse with a 429:** sliding-window buckets on RPM (30) and
     TPM (8,000); body shape matches Groq.
  4. **Slow down under load:** TTFT multiplier `1 + alpha * (in_flight
     - floor)`. So above 2 concurrent requests, each extra one adds
     8% of the baseline TTFT.
  5. **Stream in pieces:** real SSE, ~4 chars per chunk, per-token
     delay 18 ms (±6 ms jitter).
- **How I calibrated it from real data:** `standin/calibrate.py`
  reads `results/probe_raw.jsonl`, takes the 45 successful "latency"
  phase rows, fits a log-normal to their TTFTs. That is where `mu =
  5.336` and `sigma = 0.438` come from. You can check: open the
  probe file, filter for `phase == "latency"` and `status == "ok"`,
  take `log(first_token_ms)`, compute the mean and standard
  deviation — the numbers match `standin/params.json` to 3 decimal
  places.
- **Where the stand-in differs most from the real thing:**
  - One log-normal cannot reproduce the one 1,913 ms p99 spike the
    real service showed.
  - Per-token delay is a jittered constant; real Groq's stream
    cadence changes with load in ways I did not model.
  - The 429s use a strict sliding window; Groq looks more like a
    leaky bucket.
- **What I gave up:** Tail-shape realism. If I run a very long
  overload against the stand-in, the slowest 1% will look cleaner
  than it would on real Groq.
- **Other options I thought about:**
  - Record-and-replay real responses. More faithful but cannot
    change load shape.
  - A fuller distribution (log-normal + occasional heavy tail).
    Worth doing next; see "Known weak points".
- **What would make me change it:** More real-service samples. With
  200+ latency samples I would switch to a two-mode model.
- **How to find it in the code:** `standin/server.py` (the behavior),
  `standin/params.py` (the dataclass), `standin/calibrate.py` (the
  fitter), `standin/params.json` (the result).
- **Who decided:** Claude Code proposed; I accepted and reviewed the
  fit by hand.

---

## Decision 10: How I handle questions the papers cannot answer

- **What I decided:** Two layers.
  1. **Prompt:** the Draft system message tells the model: "If the
     passages do not contain the answer, reply with exactly: *I
     cannot answer from the provided papers.*" No guesses. No
     invented citations.
  2. **Verify:** a regex checks the draft contains a well-formed
     citation `[paper, §section, p.N]` where `paper` is one of the
     papers actually retrieved. If not, redraft once with stricter
     instructions. If still not, keep the first draft but flag.
- **Why:** The brief says the assistant must say so instead of
  guessing, and every answer must cite. A deterministic verifier is
  easier to defend than an LLM-based judge.
- **Evidence it works:** Real-service traces show `unans_groq`,
  `unans_hnet_cost`, `unans_moshi_iphone`, and `unans_att_date` each
  returned exactly "I cannot answer from the provided papers." Grade:
  `refused_correctly`.
- **What I gave up:** Some answerable questions get refused when the
  retrieved context does not contain the answer. In `results.csv`
  this shows up as `refused_wrongly` (18 cases on real Groq).
- **Other options I thought about:**
  - An LLM-as-judge refusal check. More expressive, but adds a
    second API call per answer.
  - Lower confidence threshold. Would refuse less but also catch
    fewer hallucinations.
- **What would make me change it:** If `refused_wrongly` kept rising
  as I added more questions, I would widen retrieval (more `k`)
  before touching the refusal rule.
- **How to find it in the code:** `relay/pipeline.py`:
  `DRAFT_SYSTEM`, `verify()`, `REFUSAL_RE`, `CITE_RE`.
- **Who decided:** Claude Code proposed the exact refusal phrase and
  citation regex; I accepted.

---

## Decision 11: Cost and token choices — short answers, hard max, local tools

- **What I decided:**
  - `max_tokens = 320` for drafts by default.
  - Context trimmed to **3,500 characters** before it reaches the
    model.
  - Each retrieved chunk clipped to **600 characters** if longer.
  - Tools (`list_sections`, `page_text`) are **local** lookups over
    pre-parsed JSONL, so they cost no tokens.
- **Why:** On the free tier the binding constraint is tokens per
  minute, not requests per minute. **A prompt I measured at ~800
  tokens means I cannot run more than about 8 requests per minute
  before TPM gates me — well under the 30 RPM limit.** Short
  answers, trimmed context, local tools all reduce TPM pressure.
- **What I gave up:** Long multi-paragraph answers. If a question
  needs a full sub-section, the model clips.
- **Other options I thought about:**
  - Dynamic `max_tokens` based on question length. Complexity for
    small gain.
  - Hosted embeddings for retrieval. Would spend TPM on every
    question; local BM25 is free.
- **What would make me change it:** A paid tier with more TPM, or a
  smaller model with the same quality.
- **How to find it in the code:** `relay/pipeline.py`:
  `build_context_block()` and `build_draft_messages()`. `max_tokens`
  default in `run_pipeline()`.
- **Who decided:** I decided on the token budgets after reading the
  probe output.

---

## Decision 12: The model — `qwen/qwen3.8-27b`, not `llama-3.1-8b-instant`

- **What I decided:** Use `qwen/qwen3.8-27b` on Groq for every model
  call.
- **Why:** My first 10 real requests failed with `model_not_found`:
  `llama-3.1-8b-instant` is **not available on my key**. I called
  `/v1/models`, saw only `openai/gpt-oss-*` and `qwen/qwen3.8-27b`
  for general chat, and picked qwen. **Those 10 wasted requests are
  logged in `results/budget.json` — I count them against my budget
  so the record is honest.**
- **What I gave up:** 10 real requests of my self-imposed 250.
- **Other options I thought about:**
  - `openai/gpt-oss-20b`: probably faster, I did not measure.
  - `openai/gpt-oss-120b`: probably higher quality, but TTFT and TPM
    behavior unknown.
- **What would make me change it:** If quality on real Qs stayed
  under 60% correct even with setting A, I would re-try the
  gpt-oss-20b option.
- **How to find it in the code:** `relay/config.py: GROQ_MODEL =
  "qwen/qwen3.8-27b"`.
- **Who decided:** Claude Code proposed llama originally; I replaced
  it with qwen after seeing the 404s.

---

## Decision 13: Deterministic keyword-based grader, no LLM judge

- **What I decided:** `eval/grade.py` returns one of `correct`,
  `wrong`, `refused_correctly`, `refused_wrongly`, using (a) a
  substring match for required key facts, (b) a regex check for the
  refusal phrase, and (c) a check that the citation names the
  expected paper. No LLM in the loop.
- **Why:** A rule-based grader is cheap, repeatable, and I can
  defend each verdict with a `grep`. An LLM judge adds variance I
  did not want to debug.
- **What I gave up:** Natural-language tolerance. If the model says
  "d = 512" instead of "d_model = 512", the grader still catches
  "512" as the key fact, so the risk is small in practice.
- **Other options I thought about:**
  - LLM-as-judge with a different model. Variable, costs tokens.
  - Multiple human raters. Not possible for a solo project.
- **What would make me change it:** Many `wrong` verdicts that a
  reader would score `correct`. Would add a soft second pass by
  hand.
- **How to find it in the code:** `eval/grade.py: grade()`.
- **Who decided:** Claude Code proposed; I accepted.

---

## Known weak points

- **Where the stand-in differs most from the real service:** it uses
  one log-normal for TTFT, so its p99 tail is thinner than the real
  one. Real Groq showed a 1,913 ms p99 spike in a 45-sample probe;
  my stand-in will not reproduce that without extra tuning. If
  Section 3 or 4 of the writeup leans on p99 numbers from the
  stand-in, keep this in mind.
- **Least certain assumption:** the stand-in's load-slowdown formula
  (`alpha = 0.08` above 2 in-flight). I did not measure Groq's TTFT
  under concurrency — doing so would have burned too much of my 250
  real-request budget. If this formula is wrong, the shape of the
  overload scenario is wrong. Everything else in the project is
  built on either (a) a measured number or (b) a stand-in behaviour
  I can turn off.
- **Deadline overshoot bug.** The slowest row in `results.csv`
  (`r-746aa392ff7f`, scenario `quality_real_C`) took **159,849 ms**
  under a 15,000 ms deadline. The per-attempt `request_timeout` is
  capped at 30 s, but a slow-streaming SSE response can re-arm
  httpx's read timeout per chunk. I have not fixed this yet. See
  "Things I did not build".

---

## Things I did not build

1. **A semantic embedding retriever** (or a cross-encoder re-ranker).
   BM25 covered the questions in the current set well. A dense
   retriever would cost TPM on every query, and TPM is already the
   binding constraint on the free tier. I would add one if quality
   dropped below 60% correct on setting A.
2. **Hedging (racing two identical requests)**. Groq's TTFT tail is
   one request in 45 above p95. Hedging would double the TPM load
   for maybe 2% of cases, and TPM is already the binding constraint.
   On a faster tier with headroom I would reconsider.

---

## Open items I need to decide myself

[WASIM TO WRITE: the exact text of the reply a user gets when the
daily budget is gone (see Decision 7). Either wire the friendly
message into `relay/pipeline.py` on `daily_budget_exhausted`, or
reword writeup §7 to describe the current behaviour (empty answer,
status `timeout`, 1 ms, no HTTP call).]

[WASIM TO WRITE: whether I ship the policy scheduler or the naive
one for burst traffic (see Decision 3). The policy drops 86.7% of a
burst but stays off the account's rate-limit radar. The naive one
gets 45% of the burst through at a cost of 55% 429s.]
