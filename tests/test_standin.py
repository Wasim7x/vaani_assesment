"""Stand-in server tests. We exercise the token bucket and the
calibration loader without binding a real TCP port.

The full HTTP path is covered by the integration test in test_e2e_standin.py
(skipped by default). These keep CI fast and don't need network."""
from __future__ import annotations
import time

from standin.server import SlidingWindowBucket
from standin.params import StandinParams
from standin.calibrate import calibrate, _fit_lognormal


def test_bucket_admits_then_blocks():
    b = SlidingWindowBucket(60.0)
    ok, _ = b.admit(3.0, 1.0)
    assert ok
    ok, _ = b.admit(3.0, 1.0)
    assert ok
    ok, _ = b.admit(3.0, 1.0)
    assert ok
    ok, wait = b.admit(3.0, 1.0)
    assert not ok and wait > 0


def test_bucket_recovers_after_window():
    b = SlidingWindowBucket(0.1)
    for _ in range(2):
        b.admit(2.0, 1.0)
    ok, _ = b.admit(2.0, 1.0)
    assert not ok
    time.sleep(0.11)
    ok, _ = b.admit(2.0, 1.0)
    assert ok


def test_params_defaults_sensible():
    p = StandinParams()
    assert p.rpm_limit > 0
    assert p.tpm_limit > 0
    assert 0 < p.ttft_sigma < 10
    assert 0 <= p.fail_rate <= 1


def test_calibrate_empty_probe_uses_defaults():
    p = calibrate(probe=[])
    assert p.rpm_limit > 0


def test_fit_lognormal_sensible():
    xs = [100, 150, 200, 300, 500, 800, 1500]
    mu, sigma = _fit_lognormal(xs)
    assert 3 < mu < 10
    assert sigma > 0
