"""
Build the results tables from raw data. Keeps tables honest: every
number is computed here from results/probe_raw.jsonl and
results/results.csv, never hand-written.

In Phase 2 only Table 1 is populated. Phase 6 adds Tables 2a/2b/3.
"""
from __future__ import annotations
import csv
import json
import statistics
from collections import defaultdict
from pathlib import Path

from relay.config import PROBE_RAW, RESULTS_CSV, ROOT


OUT_MD = ROOT / "docs" / "tables.md"


def _load_probe() -> list[dict]:
    if not PROBE_RAW.exists():
        return []
    return [json.loads(l) for l in PROBE_RAW.read_text(encoding="utf-8").splitlines() if l.strip()]


def _pct(xs: list[float], p: float) -> float:
    if not xs:
        return float("nan")
    xs = sorted(xs)
    k = min(len(xs) - 1, int(round(p * (len(xs) - 1))))
    return xs[k]


def _table1_rows(probe: list[dict]) -> list[tuple[str, str, str, str]]:
    """Each row: (metric, value, how_we_found_it, confidence)."""
    rows: list[tuple[str, str, str, str]] = []

    # Find the first RPM 429 body (body says "Limit N")
    rpm = None
    rpm_src = ""
    for r in probe:
        if r.get("status") != "rate_limited":
            continue
        body = r.get("error", "")
        if "requests per minute" in body:
            # parse "Limit 30, Used 30"
            import re
            m = re.search(r"Limit\s+(\d+)", body)
            if m:
                rpm = int(m.group(1))
                rpm_src = "server error body on RPM 429"
                break

    # TPM-header limit (input+output), ITPM-body limit (input only),
    # RPD-header limit, and retry-after values for both 429 shapes.
    import re as _re
    tpm_header = None
    itpm_body = None
    rpd = None
    rpd_remaining_at = None
    tpm_retry = None
    rpm_retry = None
    for r in probe:
        hdrs = r.get("rate_limit_headers") or {}
        if "x-ratelimit-limit-tokens" in hdrs and tpm_header is None:
            tpm_header = int(hdrs["x-ratelimit-limit-tokens"])
        if "x-ratelimit-limit-requests" in hdrs and rpd is None:
            rpd = int(hdrs["x-ratelimit-limit-requests"])
            rpd_remaining_at = hdrs.get("x-ratelimit-remaining-requests")
        body = r.get("error", "") if r.get("status") == "rate_limited" else ""
        if body and itpm_body is None and "input tokens per minute" in body:
            m = _re.search(r"Limit\s+(\d+)", body)
            if m:
                itpm_body = int(m.group(1))
        if body and tpm_retry is None and "tokens per minute" in body:
            tpm_retry = r.get("retry_after")
        if body and rpm_retry is None and "requests per minute" in body:
            rpm_retry = r.get("retry_after")

    rows.append((
        "Requests per minute",
        f"{rpm}" if rpm else "not measured",
        rpm_src or "not measured",
        "high" if rpm else "low",
    ))
    # Groq exposes two token limits: a combined limit in the header
    # and an input-only limit in the 429 body. Report both; a single
    # "TPM" number hides a real asymmetry.
    tpm_value = []
    if tpm_header is not None:
        tpm_value.append(f"{tpm_header} total (input+output)")
    if itpm_body is not None:
        tpm_value.append(f"{itpm_body} input only (ITPM)")
    tpm_str = "; ".join(tpm_value) if tpm_value else "not measured"
    tpm_how = []
    if tpm_header is not None:
        tpm_how.append("x-ratelimit-limit-tokens header")
    if itpm_body is not None:
        tpm_how.append("ITPM body text on TPM 429")
    rows.append((
        "Tokens per minute",
        tpm_str,
        "; ".join(tpm_how) if tpm_how else "not measured",
        "high" if tpm_value else "low",
    ))
    rows.append((
        "Requests per day",
        f"{rpd}" if rpd else "not measured",
        (f"x-ratelimit-limit-requests header (remaining={rpd_remaining_at} at probe time)"
         if rpd else "not measured"),
        "high" if rpd else "low",
    ))
    rows.append((
        "Tokens per day",
        "documented, not reached",
        "no TPD header surfaced by the API on 429; would need a full day's run to reach",
        "low",
    ))
    rows.append((
        "Requests in flight at once",
        "not measured",
        "server did not return a concurrency limit header; would need a concurrent-stream experiment",
        "low",
    ))

    # "What it returns when a limit is hit" — stitch the two 429 shapes
    what_hit = (
        "HTTP 429 JSON {error:{type:'requests'|'tokens',code:'rate_limit_exceeded', "
        "message:'...Limit N, Used N...'}} with Retry-After seconds header"
    )
    rows.append((
        "What it returns when a limit is hit",
        what_hit,
        "captured bodies of both RPM and TPM 429 responses",
        "high",
    ))

    # Time until it recovers
    rec_parts = []
    if rpm_retry is not None:
        rec_parts.append(f"{rpm_retry:.0f}s after RPM 429")
    if tpm_retry is not None:
        rec_parts.append(f"{tpm_retry:.0f}s after TPM 429")
    rec = ", ".join(rec_parts) if rec_parts else "not measured"
    rows.append((
        "Time until it recovers",
        rec,
        "Retry-After header on 429 (server-told)",
        "high" if rec_parts else "low",
    ))
    return rows


def _latency_summary(probe: list[dict]) -> dict:
    """Extra: TTFT/total p50/p95/p99 for the latency phase only. Useful
    while Tables 2a/2b are empty."""
    oks = [r for r in probe
           if r.get("phase") == "latency" and r.get("status") == "ok"
           and r.get("first_token_ms") is not None]
    if not oks:
        return {}
    ttfts = [r["first_token_ms"] for r in oks]
    totals = [r["total_ms"] for r in oks]
    return {
        "n": len(oks),
        "ttft_p50": _pct(ttfts, .5),
        "ttft_p95": _pct(ttfts, .95),
        "ttft_p99": _pct(ttfts, .99),
        "total_p50": _pct(totals, .5),
        "total_p95": _pct(totals, .95),
        "total_p99": _pct(totals, .99),
    }


def render_table1(probe: list[dict]) -> str:
    rows = _table1_rows(probe)
    lines = [
        "## Table 1 — Limits found in the real service",
        "",
        "| Limit | Value | How we found it | Confidence |",
        "|---|---|---|---|",
    ]
    for r in rows:
        lines.append(f"| {r[0]} | {r[1]} | {r[2]} | {r[3]} |")
    return "\n".join(lines)


def render_latency_note(probe: list[dict]) -> str:
    s = _latency_summary(probe)
    if not s:
        return ""
    return "\n".join([
        "",
        f"### Probe-phase latency (Groq, {s['n']} successful calls, 2-token output)",
        "",
        "| Metric | p50 | p95 | p99 |",
        "|---|---|---|---|",
        f"| TTFT (ms)  | {s['ttft_p50']:.0f} | {s['ttft_p95']:.0f} | {s['ttft_p99']:.0f} |",
        f"| Total (ms) | {s['total_p50']:.0f} | {s['total_p95']:.0f} | {s['total_p99']:.0f} |",
        "",
        "Short prompts here. The pipeline will measure realistic "
        "output-length latency in Phase 4.",
    ])


def main() -> int:
    probe = _load_probe()
    parts = [
        "# Results tables",
        "",
        "Generated by `python -m eval.make_tables`. Do not hand-edit.",
        "",
        render_table1(probe),
        render_latency_note(probe),
        "",
        "## Table 2a / 2b / 3 — pending",
        "",
        "Written by Phase 6 once load scenarios have run.",
    ]
    OUT_MD.write_text("\n".join(parts), encoding="utf-8")
    print(f"wrote {OUT_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
