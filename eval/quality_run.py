"""
Run the question set against real Groq (or the stand-in) once per
retrieval setting. Appends rows to results.csv so Table 3 can be
rebuilt.

Runs serially with a configurable pause between requests. The
prompt is about 800 tokens of context per question, so we need a
pause of ~11 s to stay inside the measured 8,000 TPM on the free
tier.
"""
from __future__ import annotations
import argparse
import asyncio
import os
import sys

from relay.scheduler import Scheduler
from relay.retrieval import build_index_for_setting
from relay.metrics import ResultsWriter, LOG
from eval.grade import load_questions
from eval.run_load import LoadRun, INDEX_CACHE
from relay.config import RETRIEVAL_SETTINGS


async def main_async(settings: list[str], pause_s: float,
                     source: str, standin_url: str,
                     limit: int | None) -> int:
    qs = load_questions()
    if limit:
        qs = qs[:limit]
    writer = ResultsWriter()
    base_url = standin_url if source == "standin" else None
    for setting in settings:
        INDEX_CACHE[setting] = build_index_for_setting(setting)
        sched = Scheduler()
        run = LoadRun(
            scenario=f"quality_{source}_{setting}",
            source=source,
            retrieval_setting=setting,
            scheduler=sched,
            base_url=base_url,
            writer=writer,
        )
        LOG.info("quality pass setting=%s n=%d source=%s", setting, len(qs), source)
        for q in qs:
            await run.one(q, 0)
            await asyncio.sleep(pause_s)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--settings", nargs="+", default=["A", "B", "C"],
                    choices=list(RETRIEVAL_SETTINGS))
    ap.add_argument("--pause", type=float, default=11.0,
                    help="seconds between requests (keep under TPM on real)")
    ap.add_argument("--source", choices=["real", "standin"], default="real")
    ap.add_argument("--standin-url", default=os.environ.get(
        "STANDIN_URL", "http://127.0.0.1:8765/v1"))
    ap.add_argument("--limit", type=int, default=None,
                    help="cap questions per setting (e.g. 10 answerable+unans)")
    args = ap.parse_args()
    return asyncio.run(main_async(
        args.settings, args.pause, args.source, args.standin_url, args.limit,
    ))


if __name__ == "__main__":
    raise SystemExit(main())
