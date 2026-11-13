"""
Pick the slowest row in results.csv (optionally in one scenario),
find its trace in traces.jsonl, and print a waiting / working /
retrying split. This is the evidence for write-up §4.

Usage:
  python -m eval.slow_request                      # slowest overall
  python -m eval.slow_request --scenario overload  # slowest in one scenario
"""
from __future__ import annotations
import argparse
import csv
import json
from pathlib import Path

from relay.config import RESULTS_CSV, TRACES_JSONL


def _csv_rows(scenario: str | None) -> list[dict]:
    with RESULTS_CSV.open("r", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if scenario:
        rows = [r for r in rows if r["scenario"] == scenario]
    return rows


def _slowest(rows: list[dict]) -> dict | None:
    def key(r):
        try:
            return float(r["total_ms"])
        except ValueError:
            return 0.0
    rows = sorted(rows, key=key, reverse=True)
    return rows[0] if rows else None


def _trace_for(rid: str) -> dict | None:
    if not TRACES_JSONL.exists():
        return None
    with TRACES_JSONL.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("request_id") == rid:
                return r
    return None


def _split(trace: dict) -> dict:
    """Return waiting/working/retrying (ms) + before/after first token."""
    waiting_ms = 0.0
    working_ms = 0.0
    retry_wait_ms = 0.0
    for sp in trace["spans"]:
        if sp["name"] == "understand":
            waiting_ms += sp["dur_ms"]
        elif sp["name"] == "retrieve":
            waiting_ms += sp["dur_ms"]
        elif sp["name"].startswith("tool."):
            waiting_ms += sp["dur_ms"]
        elif sp["name"] in ("draft", "redraft"):
            attrs = sp.get("attrs", {})
            waiting_ms += float(attrs.get("waited_ms", 0.0))
            working_ms += float(attrs.get("working_ms", 0.0))
            retry_wait_ms += float(attrs.get("retry_wait_ms", 0.0))
        elif sp["name"] == "verify":
            waiting_ms += sp["dur_ms"]
    total = waiting_ms + working_ms + retry_wait_ms
    return {
        "waiting_ms": round(waiting_ms, 1),
        "working_ms": round(working_ms, 1),
        "retrying_ms": round(retry_wait_ms, 1),
        "total_ms_from_spans": round(total, 1),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default=None)
    args = ap.parse_args()
    rows = _csv_rows(args.scenario)
    if not rows:
        print(f"no rows for scenario={args.scenario}")
        return 1
    slowest = _slowest(rows)
    print(f"slowest row: {slowest}")
    trace = _trace_for(slowest["request_id"])
    if not trace:
        print(f"no trace for request_id={slowest['request_id']}")
        return 2
    print("trace attrs:", json.dumps(trace["attrs"], indent=2))
    print("spans:")
    for sp in trace["spans"]:
        print(f"  {sp['name']:28} dur={sp['dur_ms']:>8.1f} ms  attrs={sp['attrs']}")
    split = _split(trace)
    print("split:", json.dumps(split, indent=2))
    # Before/after first token
    first_token_ms = float(slowest["first_token_ms"] or 0)
    total_ms = float(slowest["total_ms"])
    print(f"first_token_ms={first_token_ms:.1f}  total_ms={total_ms:.1f}  "
          f"before_first_token_ms={first_token_ms:.1f}  after_first_token_ms="
          f"{max(0.0, total_ms - first_token_ms):.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
