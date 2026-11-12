# vaani_assesment
# The Relay

Pipeline that answers questions about 3 arXiv papers using Groq, with a
local stand-in model service and a scheduler that works inside the real
free-tier limits.

See [docs/writeup.md](docs/writeup.md) for the 2-page write-up and
[docs/tables.md](docs/tables.md) for the results tables.

## Setup

```
python -m venv .venv
.venv\Scripts\activate       # Windows
pip install -r requirements.txt
copy .env.example .env       # then edit, paste your GROQ_API_KEY
```

## Fetch papers (once)

```
python -m data.fetch_papers
python -m data.parser
```

Downloads the three PDFs at the exact versions required by the brief,
then extracts text with (section, page) tags. Writes
`data/manifest.json` with SHA256 and date.

## Run

| Step | Command |
|---|---|
| Probe real service | `python -m eval.probe --phase latency` |
| Calibrate stand-in from probe | `python -m standin.calibrate` |
| Start stand-in | `python -m uvicorn standin.server:app --host 127.0.0.1 --port 8000` |
| Adhoc one question | `python -m relay.pipeline "What is d_model in the base Transformer?"` |
| Same question via stand-in | `STANDIN_URL=http://127.0.0.1:8000/v1 python -m relay.pipeline --standin "..."` |
| Run a load scenario | `python -m eval.run_load --scenario steady --source standin --n 20 --standin-url http://127.0.0.1:8000/v1` |
| Naive A/B | `python -m eval.run_load --scenario burst --source standin --n 20 --naive --standin-url http://127.0.0.1:8000/v1` |
| Real-Groq quality pass | `python -m eval.quality_run --settings A B C --pause 12 --source real` |
| Slow-request breakdown | `python -m eval.slow_request --scenario burst` |
| Make tables | `python -m eval.make_tables` |

Scenarios: `steady`, `burst`, `overload`, `halved` (per-min limit cut in half),
`exhausted` (daily budget pre-set to ceiling to exercise the drop path).

## Tests

```
pytest -q
```

## Layout

```
relay/        pipeline, llm client, scheduler, retrieval, metrics, tools, config, budget
standin/      FastAPI stand-in model + calibration script + params.json
data/         paper fetch + parser; manifest.json; PDFs and text live here
eval/         questions, grading, probe, load runner, quality runner, slow-request, table builder
results/      results.csv (raw) + traces.jsonl + probe_raw.jsonl + budget.json
docs/         LOG, DECISIONS, PROMPTS, writeup, tables, CHECKLIST
logs/         per-day log files written by the pipeline and scheduler
tests/        pytest tests (38 total)
```

## Scheduler knobs (in `relay/config.py`)

- `rpm_limit=30`, `tpm_limit=8000` — measured on Groq's free tier, `qwen/qwen3.8-27b`.
- `safety_margin=0.80` — client budgets 80% of the measured limit (D-004).
- `max_in_flight=4`, `deadline_ms=15000`, `retry_budget_per_req=3`.
- `DAILY_REAL_REQUEST_CEILING=250` — our self-imposed cap on real Groq per day.
