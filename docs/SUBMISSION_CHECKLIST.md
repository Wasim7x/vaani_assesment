# Submission checklist

Every requirement in the brief mapped to the file/section that
satisfies it. Status: `done`, `partial`, `needs me`, `todo`.

| Area | Requirement | Where | Status |
|---|---|---|---|
| Papers | 3 papers, exact arXiv versions | `data/fetch_papers.py`, `data/manifest.json` | done |
| Papers | every answer cites paper + section + page | `relay/retrieval.py`, `relay/pipeline.py` | partial (retrieval ready; pipeline Phase 4) |
| Pipeline | ≥4 steps, ≥1 tool call, ≥1 branch/loop, ≥1 retrieval | `relay/pipeline.py` | todo |
| Pipeline | refuses when evidence weak | `relay/pipeline.py` Decide step | todo |
| Model | Groq free tier, real key, no key in submission | `.env`, `.env.example`, `.gitignore` | done |
| Stand-in | slow, fail, 429, slower under load, streaming | `standin/server.py` | todo |
| Stand-in | calibrated from real measurements | `standin/calibrate.py`, `standin/params.json` | todo |
| Stand-in | documents where it is least like real | `docs/writeup.md` §2 | todo |
| Probe | modest real requests, limits + latency + recovery | `eval/probe.py`, `results/probe_raw.jsonl` | done (70 real reqs; all 7 Table-1 rows filled) |
| Scheduler | admission, bounded queue, deadline | `relay/scheduler.py` | todo |
| Scheduler | rate limiter (RPM+TPM) with safety margin | `relay/scheduler.py` | todo |
| Scheduler | in-flight cap | `relay/scheduler.py` | todo |
| Scheduler | priorities with stated rule | `relay/scheduler.py`, `DECISIONS.md` | todo |
| Scheduler | retry policy + naive-mode A/B | `relay/scheduler.py` | todo |
| Scheduler | per-call timeouts, hedging decision | `relay/scheduler.py`, `DECISIONS.md` | todo |
| Scheduler | daily budget + degrade modes | `relay/scheduler.py` | todo |
| Scheduler | circuit breaker | `relay/scheduler.py` | todo |
| Scheduler | everything logged at the limit | `relay/metrics.py`, `results/traces.jsonl` | todo |
| Scenarios | steady, burst, overload, per-min halved, daily exhausted | `eval/run_load.py` | todo |
| Scenarios | naive-retry vs policy same scenario twice | `eval/run_load.py` | todo |
| Retrieval | ≥3 settings, chosen with reasons | `relay/retrieval.py`, `docs/writeup.md` | partial (3 settings implemented; measured in Phase 6) |
| Retrieval | three misleading cases with real questions | `eval/questions.yaml`, `docs/writeup.md` | todo |
| Questions | ≥15 questions, ≥4 unanswerable, DRAFT marked | `eval/questions.yaml` | done (18 drafted; **needs student review**) |
| Grading | rule-based + manual override, no LLM grader | `eval/grade.py` | todo |
| results.csv | exact columns in exact order | `relay/metrics.py` | todo |
| Tables | Table 1, 2a, 2b, 3 computed from results.csv | `eval/make_tables.py`, `docs/tables.md` | partial (Table 1 done from probe_raw.jsonl; 2a/2b/3 in Phase 6) |
| Traces | per-request spans, slow-request split | `relay/metrics.py`, `results/traces.jsonl` | todo |
| Write-up | ≤2 pages, fixed headings in fixed order | `docs/writeup.md` | todo |
| Write-up | number "we could not have guessed without running" | `docs/writeup.md` §3 | todo |
| Write-up | least-certain assumption + 2 things not built | `docs/writeup.md` §8 | todo |
| Write-up | Vaani feedback | `docs/writeup.md` | **needs me** |
| Write-up | distinctly mine, 3 prompts, what it got wrong, override | `docs/writeup.md` | **needs me** |
| Write-up | one thing new, in my own words | `docs/writeup.md` | **needs me** |
| Transcript | real conversation history, uncleaned | `docs/CONVERSATION_HISTORY.md`, exported file | todo (Phase 8) |
