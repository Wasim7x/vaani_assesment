"""
Metrics: one place that writes the brief's required results.csv and
the per-request traces.jsonl.

results.csv columns (exact order required by the brief):
    request_id, scenario, source, start_time, first_token_ms, total_ms,
    status, retries, tokens_in, tokens_out, retrieval_setting,
    answer_grade

Status values: ok | rate_limited | timeout | error
Answer grade values: correct | wrong | refused_correctly | refused_wrongly
(or empty when we did not grade this row — the load scenarios only
grade questions from eval/questions.yaml).

Every number used in docs/tables.md must be computable from this file.
"""
from __future__ import annotations
import csv
import io
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path

from relay.config import RESULTS_CSV, TRACES_JSONL, ROOT


LOGS_DIR = ROOT / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

RESULTS_FIELDS = [
    "request_id", "scenario", "source", "start_time",
    "first_token_ms", "total_ms", "status", "retries",
    "tokens_in", "tokens_out", "retrieval_setting", "answer_grade",
]


def setup_logging(name: str = "relay") -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    # Console
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    # File — one file per run-day for easy archival.
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    fh = logging.FileHandler(LOGS_DIR / f"relay-{day}.log", encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    return logger


LOG = setup_logging()


@dataclass
class RequestRow:
    """One row in results.csv. Matches the brief exactly."""
    request_id: str
    scenario: str
    source: str                 # "real" or "stand-in"
    start_time: str             # ISO8601 UTC
    first_token_ms: float | None
    total_ms: float
    status: str                 # ok | rate_limited | timeout | error
    retries: int
    tokens_in: int
    tokens_out: int
    retrieval_setting: str
    answer_grade: str = ""      # correct | wrong | refused_correctly | refused_wrongly | ""


@dataclass
class Span:
    name: str
    t_start: float = 0.0
    t_end: float = 0.0
    attrs: dict = field(default_factory=dict)

    @property
    def dur_ms(self) -> float:
        return (self.t_end - self.t_start) * 1000.0


@dataclass
class Trace:
    """One pipeline execution's worth of spans. Written as one JSON
    line to traces.jsonl. The load runner uses this to break down a
    slow request into waiting/working/retrying."""
    request_id: str
    scenario: str
    source: str
    question_id: str = ""
    spans: list[Span] = field(default_factory=list)
    attrs: dict = field(default_factory=dict)

    def span(self, name: str, **attrs) -> "SpanCtx":
        return SpanCtx(self, name, attrs)

    def to_jsonable(self) -> dict:
        return {
            "request_id": self.request_id,
            "scenario": self.scenario,
            "source": self.source,
            "question_id": self.question_id,
            "attrs": self.attrs,
            "spans": [
                {"name": s.name, "dur_ms": s.dur_ms, "attrs": s.attrs}
                for s in self.spans
            ],
        }


class SpanCtx:
    def __init__(self, trace: Trace, name: str, attrs: dict):
        self.trace = trace
        self.span = Span(name=name, attrs=attrs)

    def __enter__(self) -> "SpanCtx":
        self.span.t_start = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.span.t_end = time.perf_counter()
        self.trace.spans.append(self.span)

    def set(self, **attrs) -> None:
        self.span.attrs.update(attrs)


# --- writer (thread-safe for the single-process load runner) ---
class ResultsWriter:
    """Append-only writer. The file is created with the header on first
    use. The load runner opens one writer per run; parallel pipelines
    share it. A lock keeps lines whole."""

    def __init__(self, csv_path: Path = RESULTS_CSV,
                 traces_path: Path = TRACES_JSONL):
        self.csv_path = csv_path
        self.traces_path = traces_path
        self._lock = threading.Lock()
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        self.traces_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.csv_path.exists():
            with self.csv_path.open("w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(RESULTS_FIELDS)

    def append_row(self, row: RequestRow) -> None:
        with self._lock:
            with self.csv_path.open("a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow([
                    row.request_id,
                    row.scenario,
                    row.source,
                    row.start_time,
                    "" if row.first_token_ms is None else f"{row.first_token_ms:.2f}",
                    f"{row.total_ms:.2f}",
                    row.status,
                    row.retries,
                    row.tokens_in,
                    row.tokens_out,
                    row.retrieval_setting,
                    row.answer_grade,
                ])

    def append_trace(self, trace: Trace) -> None:
        with self._lock:
            with self.traces_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(trace.to_jsonable(), ensure_ascii=False) + "\n")


def new_request_id(prefix: str = "r") -> str:
    # 64 bits of randomness, formatted short.
    import secrets
    return f"{prefix}-{secrets.token_hex(6)}"


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# Convenience for tests that want to introspect written rows.
def read_csv(path: Path = RESULTS_CSV) -> list[dict]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def reset_results(csv_path: Path = RESULTS_CSV,
                  traces_path: Path = TRACES_JSONL) -> None:
    """Truncate both files and re-write the CSV header. Used by load
    runs with --fresh."""
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    traces_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow(RESULTS_FIELDS)
    traces_path.write_text("", encoding="utf-8")
