"""Tests for the scheduler's window limiter and circuit breaker. The
full submit() loop is covered by an integration test against the
stand-in; these are the pieces where a logic bug is likely."""
from __future__ import annotations
import time

from relay.scheduler import SlidingWindow, Breaker


def test_window_admits_under_limit():
    w = SlidingWindow(60.0)
    assert w.earliest_free_in(1.0, 10) == 0.0
    w.record(1.0)
    w.record(1.0)
    assert w.count() == 2.0
    assert w.earliest_free_in(1.0, 10) == 0.0


def test_window_blocks_at_limit_and_recovers():
    w = SlidingWindow(0.1)  # 100ms window for fast test
    for _ in range(3):
        w.record(1.0)
    wait = w.earliest_free_in(1.0, 3)
    assert wait > 0.0
    time.sleep(0.11)
    assert w.earliest_free_in(1.0, 3) == 0.0
    assert w.count() == 0


def test_window_weighted_events():
    w = SlidingWindow(60.0)
    w.record(100.0)
    w.record(50.0)
    assert w.count() == 150.0
    # 100 cap, 50 used -> 1 doesn't fit
    assert w.earliest_free_in(1.0, 100) > 0


def test_breaker_opens_after_failures():
    b = Breaker(fail_threshold=3, cooldown_s=0.05)
    assert b.allow()
    b.on_failure()
    b.on_failure()
    b.on_failure()
    assert not b.allow()
    time.sleep(0.06)
    # half_open: one probe allowed
    assert b.allow()
    b.on_success()
    assert b.allow()
    assert b.state == "closed"
