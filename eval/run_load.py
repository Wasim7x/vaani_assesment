"""
Load runner. Executes the pipeline under one of several scenarios,
against either the real Groq service or the local stand-in, and writes
one row per request to results/results.csv plus a span trace per
request to results/traces.jsonl.

Scenarios (brief §"What to run"):
  steady     — N requests spaced evenly at a sub-limit rate.
  burst      — N requests fired in one go (concurrent).
  overload   — M bursts of K requests each, sustained for a window.
  halved     — steady, but with eff_rpm / eff_tpm cut in half (per-min
               limit halved) — tests graceful degradation.
  exhausted  — exhausted daily budget: the budget ledger is pre-set to
               the ceiling, so the scheduler must refuse without calling
               the LLM.

Required A/B: `--naive` runs the same scenario with the naive scheduler
(no deadline, no retry-after, no rate-budget).

Usage:
  python -m eval.run_load --scenario steady --source standin --n 60
  python -m eval.run_load --scenario burst  --source standin --n 60
  python -m eval.run_load --scenario overload --source standin --duration 60 --rps 2
  python -m eval.run_load --scenario halved --source standin --n 60
  python -m eval.run_load --scenario exhausted --source real --n 10
  python -m eval.run_load --scenario steady --source real --n 10 --naive
"""
from __future__ import annotations
import argparse
import asyncio
import os
import random
import time
from dataclasses import replace
from pathlib import Path
from typing import Callable

import yaml

from relay.config import ROOT, SCHEDULER, RESULTS_CSV, TRACES_JSONL
from relay.metrics import (
    ResultsWriter, RequestRow, new_request_id, utcnow_iso, LOG, reset_results,
)
from relay.scheduler import Scheduler, SchedRequest
from relay.retrieval import build_index_for_setting
from relay.pipeline import run_pipeline
from relay.budget import load as load_budget, save as save_budget
from standin.params import StandinParams
from eval.grade import load_questions, grade as grade_answer, Question


# -------- scenarios --------

async def scenario_steady(runner, *, n: int, rpm_target: int,
                          questions: list[Question]):
    interval = max(0.0, 60.0 / rpm_target)
    for i in range(n):
        q = questions[i % len(questions)]
        asyncio.create_task(runner(q, i))
        await asyncio.sleep(interval)


async def scenario_burst(runner, *, n: int, questions: list[Question]):
    tasks = []
    for i in range(n):
        q = questions[i % len(questions)]
        tasks.append(asyncio.create_task(runner(q, i)))
    await asyncio.gather(*tasks, return_exceptions=True)


async def scenario_overload(runner, *, duration_s: float, rps: float,
                            questions: list[Question]):
    interval = max(0.0, 1.0 / rps)
    t0 = time.monotonic()
    i = 0
    tasks = []
    while time.monotonic() - t0 < duration_s:
        q = questions[i % len(questions)]
        tasks.append(asyncio.create_task(runner(q, i)))
        i += 1
        await asyncio.sleep(interval)
    await asyncio.gather(*tasks, return_exceptions=True)


# -------- harness --------

class LoadRun:
    def __init__(self, *, scenario: str, source: str, retrieval_setting: str,
                 scheduler: Scheduler, base_url: str | None,
                 writer: ResultsWriter):
        self.scenario = scenario
        self.source = source
        self.setting = retrieval_setting
        self.sched = scheduler
        self.base_url = base_url
        self.writer = writer

    async def one(self, q: Question, i: int) -> None:
        res = await run_pipeline(
            q.question,
            scheduler=self.sched,
            index=INDEX_CACHE[self.setting],
            retrieval_setting=self.setting,
            base_url=self.base_url,
            scenario=self.scenario,
            source=self.source,
            question_id=q.id,
            deadline_ms=self.sched.cfg.deadline_ms,
        )
        # Grade: only when status is ok/refused and the answer is a string.
        grade = ""
        if res.status in ("ok", "refused"):
            grade = grade_answer(res.answer or "", q)
        elif res.status == "timeout":
            grade = ""   # not graded — nobody saw it
        # Map pipeline status -> results.csv status
        csv_status = {
            "ok": "ok", "refused": "ok",
            "timeout": "timeout",
            "rate_limited": "rate_limited",
            "dropped": "timeout",
            "breaker_open": "error",
            "error": "error",
        }.get(res.status, "error")
        row = RequestRow(
            request_id=res.trace.request_id if res.trace else new_request_id(),
            scenario=self.scenario,
            source=self.source,
            start_time=utcnow_iso(),
            first_token_ms=res.first_token_ms,
            total_ms=res.total_ms,
            status=csv_status,
            retries=res.retries,
            tokens_in=res.tokens_in,
            tokens_out=res.tokens_out,
            retrieval_setting=self.setting,
            answer_grade=grade,
        )
        self.writer.append_row(row)
        if res.trace:
            # stash scheduler-visible queue depth at completion
            res.trace.attrs["peak_queue_depth"] = self.sched.peak_queue_depth
            res.trace.attrs["answer"] = (res.answer or "")[:400]
            res.trace.attrs["grade"] = grade
            res.trace.attrs["csv_status"] = csv_status
            self.writer.append_trace(res.trace)


INDEX_CACHE: dict = {}


def _preload_indexes(setting: str) -> None:
    if setting not in INDEX_CACHE:
        INDEX_CACHE[setting] = build_index_for_setting(setting)


def _only_answerable_subset(qs: list[Question], source: str) -> list[Question]:
    """For stand-in loads we don't need the grader signal, but cycling
    through the full 15-set gives the right token distribution. Keep it
    as-is."""
    return qs


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario",
                    choices=["steady", "burst", "overload", "halved", "exhausted"],
                    required=True)
    ap.add_argument("--source", choices=["real", "standin"], required=True)
    ap.add_argument("--n", type=int, default=30, help="number of requests")
    ap.add_argument("--rpm-target", type=int, default=20, help="steady target")
    ap.add_argument("--rps", type=float, default=2.0, help="overload rps")
    ap.add_argument("--duration", type=float, default=30.0, help="overload seconds")
    ap.add_argument("--setting", default="B")
    ap.add_argument("--naive", action="store_true",
                    help="use the naive (no-deadline, no retry-after) scheduler")
    ap.add_argument("--fresh", action="store_true",
                    help="truncate results.csv and traces.jsonl before running")
    ap.add_argument("--standin-url", default=os.environ.get("STANDIN_URL",
                                                             "http://127.0.0.1:8000/v1"))
    ap.add_argument("--in-flight", type=int, default=None,
                    help="override scheduler max_in_flight")
    return ap.parse_args()


async def _main_async(args: argparse.Namespace) -> int:
    _preload_indexes(args.setting)
    questions = load_questions()
    _only_answerable_subset(questions, args.source)

    # Scheduler config for the scenario
    cfg = replace(SCHEDULER)
    if args.in_flight is not None:
        cfg.max_in_flight = args.in_flight
    if args.scenario == "halved":
        cfg.rpm_limit = cfg.rpm_limit // 2
        cfg.tpm_limit = cfg.tpm_limit // 2
        cfg.itpm_limit = cfg.itpm_limit // 2
    sched = Scheduler(cfg=cfg, naive=args.naive)

    base_url = args.standin_url if args.source == "standin" else None
    # The "exhausted" scenario is about the real-service daily ceiling.
    # Force the scheduler onto the real-budget path so the drop fires
    # without ever leaving the process (the ledger is pre-set so no HTTP
    # call will reach Groq).
    if args.scenario == "exhausted":
        base_url = None

    writer = ResultsWriter()
    if args.fresh:
        reset_results()
        writer = ResultsWriter()

    # Exhausted-budget scenario: pre-set the budget ledger to the ceiling.
    if args.scenario == "exhausted":
        from relay.config import DAILY_REAL_REQUEST_CEILING
        b = load_budget()
        b.requests = DAILY_REAL_REQUEST_CEILING
        save_budget(b)
        LOG.warning("budget ledger pre-set to ceiling %d for exhausted scenario",
                    DAILY_REAL_REQUEST_CEILING)

    run = LoadRun(
        scenario=args.scenario + ("_naive" if args.naive else ""),
        source=args.source,
        retrieval_setting=args.setting,
        scheduler=sched,
        base_url=base_url,
        writer=writer,
    )

    LOG.info("starting scenario=%s source=%s setting=%s n=%d naive=%s",
             args.scenario, args.source, args.setting, args.n, args.naive)
    t0 = time.monotonic()
    if args.scenario in ("steady", "halved"):
        # Fire-and-forget loop + wait until queue empty
        pending = set()

        async def runner(q, i):
            await run.one(q, i)
        for i in range(args.n):
            q = questions[i % len(questions)]
            pending.add(asyncio.create_task(runner(q, i)))
            await asyncio.sleep(max(0.0, 60.0 / args.rpm_target))
        await asyncio.gather(*pending, return_exceptions=True)
    elif args.scenario == "burst":
        await scenario_burst(run.one, n=args.n, questions=questions)
    elif args.scenario == "overload":
        await scenario_overload(run.one, duration_s=args.duration,
                                rps=args.rps, questions=questions)
    elif args.scenario == "exhausted":
        # All N should be dropped by the scheduler; we fire them so the
        # refusal path is exercised and recorded.
        await scenario_burst(run.one, n=args.n, questions=questions)
    elapsed = time.monotonic() - t0
    LOG.info("done elapsed=%.1fs peak_queue=%d", elapsed, sched.peak_queue_depth)
    return 0


def main() -> int:
    args = _parse_args()
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
