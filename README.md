# The Relay

A small pipeline that answers questions about three arXiv papers.
It uses Groq's free tier for the model, a local stand-in service for
load testing, and a scheduler that works inside the measured rate
limits.

For the full write-up, see [docs/writeup.md](docs/writeup.md).
For the choices I made and why, see [docs/DECISIONS.md](docs/DECISIONS.md).
For the tables of results, see [docs/tables.md](docs/tables.md).

## The three papers

The pipeline only answers from these three papers, at these exact
versions:

- "Attention Is All You Need" — arXiv 1706.03762v7
- "Moshi: a speech-text foundation model for real-time dialogue" —
  arXiv 2410.00037v1
- "Dynamic Chunking for End-to-End Hierarchical Sequence Modeling" —
  arXiv 2507.07955v2

The fetch script downloads them and writes a manifest with SHA256
hashes.

## Setup

```
python -m venv .venv
.venv\Scripts\activate       # Windows
pip install -r requirements.txt
copy .env.example .env       # then paste your GROQ_API_KEY
```

You need a free Groq account and a key in `.env`. The key is never
sent to any other service. `.env` is in `.gitignore`.

## Fetch the papers (once)

```
python -m data.fetch_papers     # downloads the 3 PDFs
python -m data.parser           # extracts text into data/text/*.jsonl
```

## Run

| What you want | Command |
|---|---|
| Measure the real service | `python -m eval.probe --phase latency` |
| Fit the stand-in to the probe | `python -m standin.calibrate` |
| Start the stand-in | `python -m uvicorn standin.server:app --host 127.0.0.1 --port 8000` |
| Ask one question (real Groq) | `python -m relay.pipeline "What is d_model in the base Transformer?"` |
| Same question via the stand-in | `STANDIN_URL=http://127.0.0.1:8000/v1 python -m relay.pipeline --standin "..."` |
| Run a load scenario | `python -m eval.run_load --scenario steady --source standin --n 20 --standin-url http://127.0.0.1:8000/v1` |
| Run the naive A/B version | `python -m eval.run_load --scenario burst --source standin --n 20 --naive --standin-url http://127.0.0.1:8000/v1` |
| Grade quality on real Groq | `python -m eval.quality_run --settings A B C --pause 12 --source real` |
| Break down one slow request | `python -m eval.slow_request --scenario burst` |
| Rebuild the tables | `python -m eval.make_tables` |

The five load scenarios are:

- `steady` — N requests at a steady rate, under the limit.
- `burst` — N requests all at once.
- `overload` — sustained traffic above the limit for a window.
- `halved` — same as steady but with the per-minute limit cut in
  half, to show graceful degradation.
- `exhausted` — the daily budget is pre-set to the ceiling, so the
  scheduler must refuse without calling the real API.

## Tests

```
pytest -q
```

## Folder layout

```
relay/        pipeline, LLM client, scheduler, retrieval, metrics, tools
standin/      the fake service + calibration + params.json
data/         fetch + parse + manifest + PDFs and text
eval/         questions, grading, probe, load runner, quality runner, tables
results/      results.csv, traces.jsonl, probe_raw.jsonl, budget.json
docs/         write-up, decisions, log, prompts, tables, submission checklist
logs/         one log file per UTC day
tests/        pytest suite
```

## Scheduler settings (see `relay/config.py`)

- `rpm_limit = 30` — requests per minute. Measured on Groq's free
  tier for `qwen/qwen3.8-27b`.
- `tpm_limit = 8000` — tokens per minute, combined input + output.
- `itpm_limit = 7000` — input-only tokens per minute. Groq meters
  this separately.
- `safety_margin = 0.80` — I budget 80% of the measured limit, so a
  short overshoot does not cause 429s. See Decision 6.
- `max_in_flight = 4` — at most 4 requests to the real service at
  the same time.
- `deadline_ms = 15000` — every request has 15 seconds to finish,
  or it is dropped.
- `retry_budget_per_req = 3` — up to 3 retries per request.
- `DAILY_REAL_REQUEST_CEILING = 250` — my self-imposed cap on real
  Groq per day. The real free-tier limit is 1,000.

## Where the numbers come from

Every number in `docs/tables.md` is computed from
`results/results.csv` (for load and quality) and
`results/probe_raw.jsonl` (for the real-service limits). I do not
hand-write any number in a table. Run `python -m eval.make_tables`
to rebuild them.
