"""
Daily request/token budget tracker, persisted to results/budget.json.

Everything that touches the real Groq service must call `record()`
after the call and may call `remaining()` to decide whether to issue
another. The scheduler enforces the ceiling and degrades when close;
this module just owns the counter.

A day rolls over at UTC midnight because Groq's own quota does.
"""
from __future__ import annotations
import json
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

from relay.config import BUDGET_JSON, DAILY_REAL_REQUEST_CEILING


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


@dataclass
class Budget:
    date_utc: str
    requests: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    rate_limited_hits: int = 0


def load(path: Path = BUDGET_JSON) -> Budget:
    today = _today()
    if not path.exists():
        return Budget(date_utc=today)
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return Budget(date_utc=today)
    b = Budget(**data)
    # auto-rollover: if the stored date is older, start fresh for today
    if b.date_utc != today:
        return Budget(date_utc=today)
    return b


def save(b: Budget, path: Path = BUDGET_JSON) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(b), indent=2))


def record(*, tokens_in: int = 0, tokens_out: int = 0,
           rate_limited: bool = False, path: Path = BUDGET_JSON) -> Budget:
    b = load(path)
    b.requests += 1
    b.tokens_in += tokens_in
    b.tokens_out += tokens_out
    if rate_limited:
        b.rate_limited_hits += 1
    save(b, path)
    return b


def remaining(path: Path = BUDGET_JSON) -> int:
    b = load(path)
    return max(0, DAILY_REAL_REQUEST_CEILING - b.requests)


def snapshot(path: Path = BUDGET_JSON) -> dict:
    return asdict(load(path))
