"""
A small probe of the real Groq service. Four phases, each behind a
--phase flag, so I can run one, read the data, and stop early if I
have learned enough:

  latency : 50 small requests at steady pacing. Measures
            ttft / total / tokens.
  burst   : 30 requests fired at once. Triggers an RPM 429 so we can
            read the x-ratelimit-* headers and Retry-After.
  tpm     : 10 requests with a very long prompt. Triggers a TPM 429.
  recover : after a 429, send one request every 2 s until we see a
            200. Measures how long recovery takes.

Each request writes a JSON line to results/probe_raw.jsonl and bumps
results/budget.json. `--dry-run` prints what it would do without
calling Groq, so I can check the plan safely.

Usage:
  python -m eval.probe --phase latency --dry-run      # safe, no calls
  python -m eval.probe --phase latency                # 50 real calls
  python -m eval.probe --phase burst
  python -m eval.probe --phase tpm
  python -m eval.probe --phase recover
"""
from __future__ import annotations
import argparse
import asyncio
import json
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from relay.llm_client import chat_stream, LLMResult
from relay.budget import record, snapshot, load as load_budget
from relay.config import PROBE_RAW, DAILY_REAL_REQUEST_CEILING


SHORT_PROMPT = [
    {"role": "user", "content": "Say the single word: hello"},
]

MEDIUM_PROMPT = [
    {"role": "user",
     "content": "In two sentences, what is attention in a Transformer?"},
]


def _ping(longer: bool = False) -> list[dict]:
    return MEDIUM_PROMPT if longer else SHORT_PROMPT


def _dump(r: LLMResult, extra: dict) -> None:
    PROBE_RAW.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "t_utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        **extra,
        **asdict(r),
    }
    with PROBE_RAW.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _budget_guard(need: int) -> None:
    b = load_budget()
    if b.requests + need > DAILY_REAL_REQUEST_CEILING:
        raise SystemExit(
            f"BUDGET STOP: would use {b.requests + need} / "
            f"{DAILY_REAL_REQUEST_CEILING} today. Edit the ceiling "
            f"in relay/config.py only after talking to a human."
        )


async def _call_once(phase: str, i: int, *, longer: bool = False) -> LLMResult:
    r = await chat_stream(_ping(longer), max_tokens=64)
    record(
        tokens_in=r.tokens_in, tokens_out=r.tokens_out,
        rate_limited=(r.status == "rate_limited"),
    )
    _dump(r, {"phase": phase, "i": i, "longer": longer})
    return r


async def phase_latency(dry: bool, n: int = 50, spacing_s: float = 1.2) -> None:
    print(f"[latency] {n} requests, {spacing_s}s apart. dry={dry}")
    if dry:
        return
    _budget_guard(n)
    results = []
    for i in range(n):
        r = await _call_once("latency", i)
        print(f"  {i+1:>3}/{n}  status={r.status:>12}  "
              f"ttft={r.first_token_ms}  total={r.total_ms:.0f}ms  "
              f"out_tokens={r.tokens_out}")
        results.append(r)
        if i < n - 1:
            await asyncio.sleep(spacing_s)
    oks = [x for x in results if x.status == "ok" and x.first_token_ms]
    if oks:
        ttfts = sorted(x.first_token_ms for x in oks)
        totals = sorted(x.total_ms for x in oks)
        def pct(xs, p): return xs[min(len(xs) - 1, int(len(xs) * p))]
        print(f"[latency] ok={len(oks)}/{n} "
              f"ttft p50={pct(ttfts,.5):.0f}ms p95={pct(ttfts,.95):.0f}ms  "
              f"total p50={pct(totals,.5):.0f}ms p95={pct(totals,.95):.0f}ms")


async def phase_burst(dry: bool, n: int = 30) -> None:
    print(f"[burst] {n} requests in parallel. dry={dry}")
    if dry:
        return
    _budget_guard(n)
    tasks = [asyncio.create_task(_call_once("burst", i)) for i in range(n)]
    results = await asyncio.gather(*tasks)
    ok = sum(1 for r in results if r.status == "ok")
    rl = sum(1 for r in results if r.status == "rate_limited")
    print(f"[burst] ok={ok} rate_limited={rl} error={n-ok-rl}")
    for r in results:
        if r.status == "rate_limited":
            print(f"  429 headers: {r.rate_limit_headers}")
            print(f"  retry_after: {r.retry_after}")
            print(f"  body:        {r.error[:200]}")
            break


async def phase_tpm(dry: bool, n: int = 10) -> None:
    # Pad the prompt to be large enough to push TPM but still inside
    # per-request context on 8b.
    padding = " ".join(["context"] * 2000)
    msgs = [{"role": "user",
             "content": f"Here is some context to pad token count: {padding}\n\n"
                        f"Now, in one short sentence, define attention."}]
    print(f"[tpm] {n} large-context requests in parallel. dry={dry}")
    if dry:
        return
    _budget_guard(n)
    async def one(i):
        r = await chat_stream(msgs, max_tokens=64)
        record(tokens_in=r.tokens_in, tokens_out=r.tokens_out,
               rate_limited=(r.status == "rate_limited"))
        _dump(r, {"phase": "tpm", "i": i, "longer": True})
        return r
    rs = await asyncio.gather(*[one(i) for i in range(n)])
    ok = sum(1 for r in rs if r.status == "ok")
    rl = sum(1 for r in rs if r.status == "rate_limited")
    print(f"[tpm] ok={ok} rate_limited={rl}")
    for r in rs:
        if r.status == "rate_limited":
            print(f"  429 headers: {r.rate_limit_headers}")
            print(f"  retry_after: {r.retry_after}")
            break


async def phase_recover(dry: bool, max_wait_s: float = 120.0,
                        poll_s: float = 2.0) -> None:
    print(f"[recover] polling until a 200. dry={dry}")
    if dry:
        return
    _budget_guard(int(max_wait_s / poll_s) + 1)
    t0 = time.perf_counter()
    i = 0
    while True:
        r = await _call_once("recover", i)
        elapsed = time.perf_counter() - t0
        print(f"  t+{elapsed:5.1f}s  status={r.status}")
        if r.status == "ok":
            print(f"[recover] recovered after {elapsed:.1f}s")
            return
        if elapsed > max_wait_s:
            print(f"[recover] gave up after {elapsed:.1f}s")
            return
        await asyncio.sleep(poll_s)
        i += 1


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", choices=["latency", "burst", "tpm", "recover"],
                    required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--n", type=int, default=None, help="override batch size")
    args = ap.parse_args()

    print(f"budget before: {snapshot()}")
    runner = {
        "latency": lambda: phase_latency(args.dry_run, n=args.n or 50),
        "burst":   lambda: phase_burst(args.dry_run, n=args.n or 30),
        "tpm":     lambda: phase_tpm(args.dry_run, n=args.n or 10),
        "recover": lambda: phase_recover(args.dry_run),
    }[args.phase]
    asyncio.run(runner())
    print(f"budget after:  {snapshot()}")


if __name__ == "__main__":
    main()
