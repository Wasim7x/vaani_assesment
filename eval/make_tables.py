"""
Build the results tables from the raw files. Every number comes
from results/probe_raw.jsonl (Table 1) or results/results.csv
(Tables 2a, 2b, 3). We never hand-write a number in a table, so
re-running this file is the way to check the write-up's numbers.

Early in the project (Phase 2) only Table 1 was filled. Tables 2a,
2b, and 3 were added in Phase 6, once the load and quality runs
had written their rows.
"""
from __future__ import annotations
import csv
import json
import statistics
from collections import defaultdict
from pathlib import Path

from relay.config import PROBE_RAW, RESULTS_CSV, ROOT, TRACES_JSONL


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


def _load_results() -> list[dict]:
    if not RESULTS_CSV.exists():
        return []
    with RESULTS_CSV.open("r", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _f(x: str | None) -> float | None:
    if x is None or x == "":
        return None
    try:
        return float(x)
    except ValueError:
        return None


def _group_by(rows: list[dict], key: str) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        out[r.get(key, "")].append(r)
    return out


def _scenario_stats(rows: list[dict]) -> dict:
    n = len(rows)
    oks = [r for r in rows if r["status"] == "ok"]
    rls = [r for r in rows if r["status"] == "rate_limited"]
    tos = [r for r in rows if r["status"] in ("timeout",)]
    errs = [r for r in rows if r["status"] == "error"]
    ttfts = [_f(r["first_token_ms"]) for r in oks]
    ttfts = [t for t in ttfts if t is not None]
    totals = [_f(r["total_ms"]) for r in rows]
    totals = [t for t in totals if t is not None]
    retries = [int(r["retries"] or 0) for r in rows]
    tok_in = [int(r["tokens_in"] or 0) for r in oks]
    tok_out = [int(r["tokens_out"] or 0) for r in oks]
    src = rows[0]["source"] if rows else ""
    # First + last timestamps (seconds) for duration.
    import datetime as dt
    def ts(r):
        try:
            return dt.datetime.fromisoformat(r["start_time"]).timestamp()
        except Exception:
            return 0.0
    tss = [ts(r) for r in rows]
    duration_s = max(1.0, (max(tss) - min(tss))) if len(tss) >= 2 else 0.0
    offered_per_min = (n / duration_s * 60) if duration_s > 0 else float("nan")
    tok_per_ans = (sum(tok_in) + sum(tok_out)) / len(oks) if oks else 0.0
    return {
        "n": n,
        "source": src,
        "duration_s": duration_s,
        "offered_per_min": offered_per_min,
        "ttft_p50": _pct(ttfts, .5) if ttfts else float("nan"),
        "ttft_p95": _pct(ttfts, .95) if ttfts else float("nan"),
        "ttft_p99": _pct(ttfts, .99) if ttfts else float("nan"),
        "total_p50": _pct(totals, .5) if totals else float("nan"),
        "total_p95": _pct(totals, .95) if totals else float("nan"),
        "total_p99": _pct(totals, .99) if totals else float("nan"),
        "pct_ok": 100.0 * len(oks) / n if n else 0.0,
        "pct_rl": 100.0 * len(rls) / n if n else 0.0,
        "pct_timeout": 100.0 * (len(tos) + len(errs)) / n if n else 0.0,
        "retries_per_req": sum(retries) / n if n else 0.0,
        "tokens_per_answer": tok_per_ans,
    }


def _peak_queue_for(scenario: str) -> int:
    """Pull peak queue depth from traces.jsonl attrs we stashed there."""
    if not TRACES_JSONL.exists():
        return 0
    peak = 0
    with TRACES_JSONL.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("scenario") == scenario:
                peak = max(peak, int(r.get("attrs", {}).get("peak_queue_depth", 0)))
    return peak


def _cost_per_1000(tokens_in: float, tokens_out: float) -> str:
    """No monetary cost on Groq's free tier. Report in tokens instead so
    the column is honest."""
    return f"{(tokens_in + tokens_out):.0f} tokens"


def render_table2a2b(rows: list[dict]) -> str:
    if not rows:
        return "\n## Table 2a / 2b — no load runs yet\n"
    by_scen = _group_by(rows, "scenario")
    # Keep a stable order: policy scenarios first, naive variants after.
    order = []
    for k in ("steady", "burst", "overload", "halved", "exhausted"):
        if k in by_scen:
            order.append(k)
        if f"{k}_naive" in by_scen:
            order.append(f"{k}_naive")
    for k in by_scen:
        if k not in order:
            order.append(k)

    out = ["", "## Table 2a — Latency by scenario", "",
           "| Scenario | Real/Stand-in | Requests | Duration (s) | Offered rate (/min) "
           "| TTFT p50/p95/p99 (ms) | Total p50/p95/p99 (ms) |",
           "|---|---|---|---|---|---|---|"]
    for k in order:
        s = _scenario_stats(by_scen[k])
        out.append(
            f"| {k} | {s['source']} | {s['n']} | {s['duration_s']:.1f} "
            f"| {s['offered_per_min']:.1f} "
            f"| {s['ttft_p50']:.0f} / {s['ttft_p95']:.0f} / {s['ttft_p99']:.0f} "
            f"| {s['total_p50']:.0f} / {s['total_p95']:.0f} / {s['total_p99']:.0f} |"
        )

    out += ["", "## Table 2b — Outcomes by scenario", "",
            "| Scenario | Succeeded % | Rate-limited % | Timed out/given up % "
            "| Retries/request | Peak queue | Tokens/answer | Cost/1000 answers |",
            "|---|---|---|---|---|---|---|---|"]
    for k in order:
        s = _scenario_stats(by_scen[k])
        peak = _peak_queue_for(k)
        # cost: all answers were on Groq's free tier, so cost column
        # reports total tokens per 1000 answers instead.
        cost = _cost_per_1000(s["tokens_per_answer"] * 1000, 0)
        out.append(
            f"| {k} | {s['pct_ok']:.1f} | {s['pct_rl']:.1f} | {s['pct_timeout']:.1f} "
            f"| {s['retries_per_req']:.2f} | {peak} "
            f"| {s['tokens_per_answer']:.0f} | {cost} |"
        )
    return "\n".join(out)


def render_table3(rows: list[dict]) -> str:
    """Retrieval settings A / B / C — grading breakdown + tokens + TTFT.

    Only real-service rows count: the stand-in echoes the input and
    cannot produce citations, so grading stand-in rows is meaningless
    for a retrieval-quality comparison."""
    if not rows:
        return "\n## Table 3 — no graded runs yet\n"
    graded = [r for r in rows
              if r.get("answer_grade") and r.get("source") == "real"]
    if not graded:
        return "\n## Table 3 — no real-service graded runs yet\n"
    by_set = _group_by(graded, "retrieval_setting")
    out = ["", "## Table 3 — Retrieval settings (quality vs cost)", "",
           "| Setting (chunk, k, rerank) | Qs | Correct & cited % | Wrong % "
           "| Correctly refused % | Tokens/answer | TTFT p50/p95 (ms) "
           "| Total p50/p95 (ms) |",
           "|---|---|---|---|---|---|---|---|"]
    from relay.config import RETRIEVAL_SETTINGS
    for k in sorted(by_set):
        rs = by_set[k]
        n = len(rs)
        c = sum(1 for r in rs if r["answer_grade"] == "correct")
        w = sum(1 for r in rs if r["answer_grade"] == "wrong")
        rc = sum(1 for r in rs if r["answer_grade"] == "refused_correctly")
        rw = sum(1 for r in rs if r["answer_grade"] == "refused_wrongly")
        tok = [int(r["tokens_in"] or 0) + int(r["tokens_out"] or 0) for r in rs]
        ttfts = [_f(r["first_token_ms"]) for r in rs]
        ttfts = [t for t in ttfts if t is not None]
        totals = [_f(r["total_ms"]) for r in rs]
        totals = [t for t in totals if t is not None]
        cfg = RETRIEVAL_SETTINGS.get(k, {})
        label = f"{k}: {cfg.get('chunk_tokens','?')}t, k={cfg.get('final_k','?')}, mmr={cfg.get('rerank',False)}"
        out.append(
            f"| {label} | {n} | {100*c/n:.1f} | {100*(w+rw)/n:.1f} "
            f"| {100*rc/n:.1f} | {sum(tok)/n:.0f} "
            f"| {_pct(ttfts,.5) if ttfts else float('nan'):.0f} / "
            f"{_pct(ttfts,.95) if ttfts else float('nan'):.0f} "
            f"| {_pct(totals,.5) if totals else float('nan'):.0f} / "
            f"{_pct(totals,.95) if totals else float('nan'):.0f} |"
        )
    return "\n".join(out)


def main() -> int:
    probe = _load_probe()
    rows = _load_results()
    parts = [
        "# Results tables",
        "",
        "Generated by `python -m eval.make_tables`. Do not hand-edit.",
        "",
        render_table1(probe),
        render_latency_note(probe),
        render_table2a2b(rows),
        render_table3(rows),
        "",
    ]
    OUT_MD.write_text("\n".join(parts), encoding="utf-8")
    print(f"wrote {OUT_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
