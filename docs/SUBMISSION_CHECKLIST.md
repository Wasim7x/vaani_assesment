# Submission checklist

Every requirement in the brief mapped to the file/section that satisfies it. Status: `done`, `partial`, `needs me`, `todo`.

| Area | Requirement | Where | Status |
|---|---|---|---|
| Papers | 3 papers, exact arXiv versions | `data/fetch_papers.py`, `data/manifest.json` | done |
| Papers | every answer cites paper + section + page | `relay/retrieval.py`, `relay/pipeline.py` | done (Verify step enforces [paper, §section, p.N]) |
| Pipeline | ≥4 steps, ≥1 tool call, ≥1 branch/loop, ≥1 retrieval | `relay/pipeline.py` | done (4 steps, `list_sections` tool, paper-filter branch + redraft loop) |
| Pipeline | refuses when evidence weak | `relay/pipeline.py` Draft prompt | done (exact refusal phrase enforced in prompt + verify) |
| Model | Groq free tier, real key, no key in submission | `.env`, `.env.example`, `.gitignore` | done |
| Stand-in | slow, fail, 429, slower under load, streaming | `standin/server.py` | done |
| Stand-in | calibrated from real measurements | `standin/calibrate.py`, `standin/params.json` | done (fitted log-normal from probe calls) |
| Stand-in | documents where it is least like real | `docs/writeup.md` §2 | done |
| Probe | modest real requests, limits + latency + recovery | `eval/probe.py`, `results/probe_raw.jsonl` | done (real requests; all 7 Table-1 rows filled) |
| Scheduler | admission, bounded queue, deadline | `relay/scheduler.py` | done (max_queue=64, deadline-drop at each gate) |
| Scheduler | rate limiter (RPM+TPM) with safety margin | `relay/scheduler.py` | done (0.80 of measured; SlidingWindow) |
| Scheduler | in-flight cap | `relay/scheduler.py` | done (`asyncio.Semaphore(max_in_flight)`) |
| Scheduler | priorities with stated rule | `relay/scheduler.py`, `DECISIONS.md` | done (`priority` field present, FIFO used in runs; priorities documented in writeup §5) |
| Scheduler | retry policy + naive-mode A/B | `relay/scheduler.py` | done (`--naive` A/B wired; see §6 and Table 2a/2b `_naive` rows) |
| Scheduler | per-call timeouts, hedging decision | `relay/scheduler.py`, `DECISIONS.md` | done (per-attempt timeout; no hedging — writeup §8) |
| Scheduler | daily budget + degrade modes | `relay/scheduler.py` | done (max_tokens cap at remaining<25, drop at 0) |
| Scheduler | circuit breaker | `relay/scheduler.py` | done (`Breaker` 3-state) |
| Scheduler | everything logged at the limit | `relay/metrics.py`, `results/traces.jsonl` | done (span tree per request; logs/ dir) |
| Scenarios | steady, burst, overload, per-min halved, daily exhausted | `eval/run_load.py` | done |
| Scenarios | naive-retry vs policy same scenario twice | `eval/run_load.py --naive` | done |
| Retrieval | ≥3 settings, chosen with reasons | `relay/retrieval.py`, `docs/writeup.md` | done (A/B/C; recommendation documented) |
| Retrieval | three misleading cases with real questions | `eval/questions.yaml`, `docs/writeup.md` | done (writeup §"How this library will mislead") |
| Questions | ≥15 questions, ≥4 unanswerable, reviewed | `eval/questions.yaml` | done (19 reviewed: 12 answerable, 4 unanswerable, 3 tricky) |
| Grading | rule-based + manual override, no LLM grader | `eval/grade.py` | done (deterministic keyword grader + citation paper check) |
| results.csv | exact columns in exact order | `relay/metrics.py` | done (`RESULTS_FIELDS`) |
| Tables | Table 1, 2a, 2b, 3 computed from results.csv | `eval/make_tables.py`, `docs/tables.md` | done |
| Traces | per-request spans, slow-request split | `relay/metrics.py`, `results/traces.jsonl` | done (wait / work / retry_wait spans per attempt) |
| Write-up | ≤2 pages, fixed headings in fixed order | `docs/writeup.md` | done (contains all required sections and tables embedded) |
| Write-up | number "we could not have guessed without running" | `docs/writeup.md` §3 | done (TPM/ITPM, not RPM, is the binding constraint at ~800-token prompts) |
| Write-up | least-certain assumption + 2 things not built | `docs/writeup.md` §8 | done |
| Write-up | Vaani feedback | `docs/writeup.md` | done |
| Write-up | distinctly mine, 3 prompts, what it got wrong, override | `docs/writeup.md` §How I worked with the assistant | done |
| Write-up | one thing new, in my own words | `docs/writeup.md` §8 (ITPM vs TPM) | done |
| Transcript | real conversation history, uncleaned | `docs/CONVERSATION_HISTORY.md`, `docs/PROMPTS.md` | done |
