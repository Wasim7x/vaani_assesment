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

## Run (filled in as phases land)

| Step | Command |
|---|---|
| Probe real service | `python -m eval.probe` (Phase 2) |
| Calibrate stand-in | `python -m standin.calibrate` (Phase 3) |
| Start stand-in | `uvicorn standin.server:app` (Phase 3) |
| Run a load scenario | `python -m eval.run_load --scenario steady --source standin` (Phase 6) |
| Make tables | `python -m eval.make_tables` (Phase 6) |

## Tests

```
pytest -q
```

## Layout

```
relay/        pipeline, llm client, scheduler, retrieval, metrics, config
standin/      FastAPI stand-in model + calibration script
data/         paper fetch + parser; manifest.json; PDFs and text live here
eval/         questions, grading, probe, load runner, table builder
results/      results.csv (raw) + traces.jsonl + probe_raw.jsonl + budget.json
docs/         LOG, DECISIONS, PROMPTS, writeup, tables, CHECKLIST
tests/        pytest tests for the easy-to-get-wrong bits
```
