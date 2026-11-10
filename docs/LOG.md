# Log

Dated notes of what we did, what we measured, what surprised us, what
broke. Decisions live in `DECISIONS.md`; this file is the timeline.

## 2026-11-10 — Phase 0 (plan)

- Read the brief (4 pages). Confirmed the fixed arXiv versions and the
  required headings in the write-up.
- Agreed a Groq real-request ceiling of ~250 for the whole project;
  stop and ask before any batch above 50.
- Chose `llama-3.1-8b-instant` as the primary model. Rationale: fastest
  TTFT on the Groq free tier, cheapest tokens; a retrieval-grounded
  pipeline does not need a 70B judge.
- Stack: Python 3.11+, asyncio, httpx, FastAPI, rank_bm25, pymupdf.
  Actual Python on this box is 3.14 — fine, but flagged in case any
  wheel does not yet have 3.14 support.

## 2026-11-10 — Phase 1 (setup)

- Skeleton written: `relay/`, `standin/`, `data/`, `eval/`, `results/`,
  `docs/`, `tests/`. `.env.example`, `.gitignore`, `requirements.txt`
  pinned, `README.md` stub, `relay/config.py` with retrieval settings
  and placeholder scheduler numbers (filled after Phase 2).
- Pinned deps updated to what actually installs on Python 3.14 on
  Windows: pymupdf 1.24 had no wheel for 3.14 and fails to build from
  source without MSVC; 1.28.2 works. Captured actual versions.
- Downloaded 3 arXiv papers at exact versions. Manifest with SHA256:
    attention (1706.03762v7): 2.1 MB, sha bdfa...
    moshi (2410.00037v1):     4.5 MB, sha a687...
    hnet (2507.07955v2):      1.3 MB, sha 04a2...
- Parser: first version used PyMuPDF `get_text("blocks")` and found
  only 3 sections for Attention (one block per page). Rewrote to be
  line-based with two-line heading merge — PyMuPDF puts "3.1" and
  "Encoder and Decoder Stacks" on separate lines. Section count jumped
  to 36 for Attention. Tightened heading regex: first digit must be
  1–9, sub-levels single digits only, min title length 5, reject
  measurement-unit titles like "12.5 Hz". Final counts: Attention 46
  blocks / 33 sections, Moshi 143 / 67, H-Net 89 / 49.
- Some false-positive sections remain (table rows like "14.8 Spectron
  (1B)"). Accepted them — they only mislabel a few chunks; BM25 still
  matches the content.
- Retrieval: BM25 over three settings — A (150-token, top-8, no
  rerank), B (400-token, top-4, no rerank), C (400-token, fetch-12,
  MMR-dedupe to 4). First smoke test retrieved chunks with weirdly
  missing numbers. Cause: my tokenizer only matched letter-starting
  tokens, dropping "512", "8", etc. from both BM25 vectors AND the
  chunk text. Fixed to keep numeric tokens. Numbers queries now hit
  the right sections (e.g. "frame rate of Mimi in Hz" → §3.3.1
  Architecture).
- Known-limitation of BM25 that bit the smoke test: "Which optimizer
  did the Transformer use and what was beta2?" retrieved Moshi
  references instead of Attention §5.3. The Understand step in the
  pipeline (Phase 4) will rewrite queries to narrow the paper first.
- Drafted eval/questions.yaml with 18 questions (12 answerable, 4
  unanswerable, 3 tricky). All marked DRAFT — student reviews before
  Phase 6. Three questions have `notes` asking the student to verify
  the exact value against the paper (e.g. Mimi RVQ depth).

### Phase 1 handoff: what the student should check

- Open eval/questions.yaml and review each DRAFT. Fix any key_facts
  that don't match the actual paper, then delete the `status: DRAFT`
  line to signal "approved by student".
- Skim data/manifest.json to confirm the 3 SHA256 hashes are what
  arxiv served today. (They'll change if arXiv replaces a PDF, which
  is rare but possible.)
- Pull the first page of each paper from data/pdfs/ and spot-check
  that the parsed sections in data/text/*.jsonl line up with reality.

## 2026-11-10 — Phase 2 (llm client + probe + Table 1)

- `relay/llm_client.py` — async httpx streaming client, measures
  TTFT + total, reads `x-ratelimit-*` + `retry-after` on any response.
  No retries here; scheduler owns that.
- `relay/budget.py` — persisted JSON ledger with UTC day rollover and
  corrupt-file resilience; hard-stops when the 250/day ceiling would
  be exceeded.
- `eval/probe.py` — four phase-gated batches, each with --dry-run.
- 15 tests pass.
- Dropped the full 30-parallel burst and the poll-based `recover`
  phase after the data they would have produced came out of the other
  two phases for free (RPM from body, retry-after from header).

### What the probe found
- First 10 real requests failed with `model_not_found` — the planned
  `llama-3.1-8b-instant` is not on this key. Called `/v1/models`,
  swapped to `qwen/qwen3.8-27b`. 10 requests wasted; budget ledger
  still counts them (honest).
- Latency on the new model (45 OK, 2-token output): TTFT
  p50=189ms / p95=391ms / p99=1913ms. The p99 spike is one request —
  don't over-interpret it. Total ≈ TTFT because outputs are 2 tokens.
- RPM limit = 30 (server body, high confidence). At 1.2 s spacing we
  hit 5/40 429s — the limit is a cliff.
- TPM (total) = 8000; ITPM (input) = 7000. Two different numbers —
  Groq meters input separately. The `x-ratelimit-limit-tokens` header
  reports the combined 8000; the 429 body on an ITPM breach reports
  7000. Important later: our scheduler budgets must include the
  prompt tokens, not just completion.
- RPD = 1000 (header, high confidence). Our 250 self-ceiling is well
  below.
- TPD = documented, not reached. No TPD header surfaced.
- In-flight concurrency limit = not measured. Groq does not surface
  one; we'd need a separate experiment, and it would cost more than
  it's worth given that RPM already caps us.
- Retry-After: 2 s on RPM 429, ~10 s on TPM 429. Server-told, so use
  this instead of blind backoff.
- Scheduler config now reflects measured numbers; safety margin
  chosen at 80% (see D-004).
- Budget after Phase 2: 70 real requests used (10 wasted + 60
  useful), 180 remain of the 250 ceiling.
