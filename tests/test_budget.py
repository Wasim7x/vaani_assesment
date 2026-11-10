"""Tests for the daily budget tracker: new-day rollover, record math,
and file resilience. Uses a temp JSON path so a real budget is not
touched."""
from __future__ import annotations
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from relay import budget as bud


def test_load_fresh(tmp_path: Path):
    p = tmp_path / "b.json"
    b = bud.load(p)
    assert b.requests == 0
    assert b.date_utc == datetime.now(timezone.utc).strftime("%Y-%m-%d")


def test_record_counts(tmp_path: Path):
    p = tmp_path / "b.json"
    bud.record(tokens_in=10, tokens_out=5, path=p)
    bud.record(tokens_in=20, tokens_out=8, rate_limited=True, path=p)
    b = bud.load(p)
    assert b.requests == 2
    assert b.tokens_in == 30
    assert b.tokens_out == 13
    assert b.rate_limited_hits == 1


def test_rollover_discards_yesterday(tmp_path: Path):
    p = tmp_path / "b.json"
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1))\
        .strftime("%Y-%m-%d")
    p.write_text(json.dumps({
        "date_utc": yesterday,
        "requests": 99, "tokens_in": 1, "tokens_out": 2,
        "rate_limited_hits": 3,
    }))
    b = bud.load(p)
    assert b.requests == 0
    assert b.date_utc != yesterday


def test_corrupt_file_resets(tmp_path: Path):
    p = tmp_path / "b.json"
    p.write_text("{this is not json")
    b = bud.load(p)
    assert b.requests == 0
