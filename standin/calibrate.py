"""
Fit the stand-in's knobs from real probe data.

Reads results/probe_raw.jsonl, keeps the successful rows of the
`latency` phase, fits TTFT and total time to a log-normal, and
pulls rate-limit values from the 429 rows. Writes
standin/params.json.

Where the stand-in differs from real Groq (also written up in
docs/writeup.md §2):
  - We use one log-normal for TTFT across all calls. The real
    service showed one 1,913 ms p99 spike in a 45-sample probe.
    A single log-normal cannot reproduce that tail.
  - The per-token delay is a jittered constant. Real streaming
    cadence changes with load in ways we did not model.
  - Our 429s are a strict sliding window. Groq looks more like a
    leaky bucket at the same effective limit.
"""
from __future__ import annotations
import argparse
import json
import math
import statistics
from pathlib import Path

from relay.config import PROBE_RAW
from standin.params import StandinParams, PARAMS_JSON


def _load_probe() -> list[dict]:
    if not PROBE_RAW.exists():
        return []
    return [json.loads(l) for l in PROBE_RAW.read_text(encoding="utf-8").splitlines() if l.strip()]


def _fit_lognormal(xs: list[float]) -> tuple[float, float]:
    logs = [math.log(x) for x in xs if x > 0]
    if len(logs) < 2:
        return 5.3, 0.45  # defaults
    mu = statistics.fmean(logs)
    sigma = statistics.pstdev(logs)
    return mu, sigma


def calibrate(probe: list[dict] | None = None) -> StandinParams:
    probe = probe if probe is not None else _load_probe()

    oks = [r for r in probe
           if r.get("phase") == "latency"
           and r.get("status") == "ok"
           and r.get("first_token_ms") is not None]
    ttfts = [float(r["first_token_ms"]) for r in oks]

    p = StandinParams.load()

    if ttfts:
        mu, sigma = _fit_lognormal(ttfts)
        p.ttft_mu = round(mu, 3)
        p.ttft_sigma = round(max(0.1, sigma), 3)

    # Slow-tail estimate: fraction of calls with ttft > p95.
    if len(ttfts) >= 20:
        srt = sorted(ttfts)
        p95 = srt[int(0.95 * (len(srt) - 1))]
        tail = sum(1 for x in ttfts if x > p95 * 1.5) / len(ttfts)
        p.slow_tail_rate = round(tail, 3)

    # Fail rate: fraction of non-429 errors across the probe.
    bad = [r for r in probe if r.get("status") == "error"]
    ok_or_bad = [r for r in probe if r.get("status") in ("ok", "error")]
    if ok_or_bad:
        # Exclude the 404 wasted requests from the first model (noise).
        bad_excl = [r for r in bad if "model_not_found" not in r.get("error", "")]
        ok_or_bad_excl = [r for r in ok_or_bad if "model_not_found" not in r.get("error", "")]
        if ok_or_bad_excl:
            p.fail_rate = round(len(bad_excl) / len(ok_or_bad_excl), 3)

    # Rate-limit values from 429 bodies/headers — use defaults from
    # Phase 2 unless the probe says otherwise. Keep this conservative.
    for r in probe:
        hdrs = r.get("rate_limit_headers") or {}
        if "x-ratelimit-limit-tokens" in hdrs:
            try:
                p.tpm_limit = int(hdrs["x-ratelimit-limit-tokens"])
            except ValueError:
                pass
        if "x-ratelimit-limit-requests" in hdrs:
            try:
                p.rpd_limit = int(hdrs["x-ratelimit-limit-requests"])
            except ValueError:
                pass
        body = r.get("error", "") if r.get("status") == "rate_limited" else ""
        if body and "requests per minute" in body:
            import re
            m = re.search(r"Limit\s+(\d+)", body)
            if m:
                p.rpm_limit = int(m.group(1))
        if body and r.get("retry_after"):
            if "tokens per minute" in body:
                p.retry_after_tpm = float(r["retry_after"])
            elif "requests per minute" in body:
                p.retry_after_rpm = float(r["retry_after"])

    return p


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="print params but do not write")
    args = ap.parse_args()
    p = calibrate()
    out = json.dumps(p.to_dict(), indent=2)
    print(out)
    if not args.dry_run:
        PARAMS_JSON.write_text(out)
        print(f"wrote {PARAMS_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
