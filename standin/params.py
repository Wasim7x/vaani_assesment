"""
Stand-in knobs. One dataclass plus a loader. The loader merges the
defaults with standin/params.json (written by calibrate.py) and
with STANDIN_<FIELD> env-var overrides, so the server can be
re-tuned without changing code.

All latencies are in milliseconds. All rates are per minute. All
probabilities are in [0, 1]. TTFT = time to first token, modelled
as a log-normal with mu/sigma fitted from the probe (see
standin/calibrate.py). The per-token delay sets the streaming
cadence.
"""
from __future__ import annotations
import json
import os
from dataclasses import dataclass, asdict, fields
from pathlib import Path

from relay.config import ROOT

PARAMS_JSON = ROOT / "standin" / "params.json"


@dataclass
class StandinParams:
    # TTFT log-normal: X = exp(mu + sigma * N(0,1))  (ms)
    ttft_mu: float = 5.30            # ~exp(5.3) = 200ms
    ttft_sigma: float = 0.45         # heavier right tail
    # Per-output-token delay (ms). Keeps streaming visible but quick.
    per_token_ms_mean: float = 18.0
    per_token_ms_jitter: float = 6.0

    # Reliability
    fail_rate: float = 0.02          # 2% random 500s
    slow_tail_rate: float = 0.03     # 3% requests get +1500ms extra wait

    # Rate limits (per minute, token-bucket) — calibrated to probe
    rpm_limit: int = 30
    tpm_limit: int = 8000            # total tokens/min
    retry_after_rpm: float = 2.0     # seconds (Groq-measured)
    retry_after_tpm: float = 10.0    # seconds (Groq-measured)
    rpd_limit: int = 1000            # requests per day

    # Load-based slowdown. Effective ttft is multiplied by
    #   1 + load_slowdown_alpha * max(0, in_flight - load_slowdown_floor)
    # So the first `floor` concurrent requests feel normal; after that,
    # every extra concurrent adds `alpha * baseline` more waiting.
    load_slowdown_alpha: float = 0.08
    load_slowdown_floor: int = 2

    # Default response "token" budget when the request doesn't cap it.
    default_tokens_out: int = 48
    # Scale factor from char count -> token count for billing.
    chars_per_token: float = 4.0

    @classmethod
    def load(cls) -> "StandinParams":
        """Merge defaults with params.json (if any) and env overrides."""
        kwargs: dict = {}
        if PARAMS_JSON.exists():
            try:
                kwargs = json.loads(PARAMS_JSON.read_text())
            except json.JSONDecodeError:
                pass
        # Keep only known fields.
        known = {f.name for f in fields(cls)}
        kwargs = {k: v for k, v in kwargs.items() if k in known}
        obj = cls(**kwargs)
        # Env overrides: STANDIN_<FIELD>
        for f in fields(cls):
            env = f"STANDIN_{f.name.upper()}"
            if env in os.environ:
                raw = os.environ[env]
                try:
                    if f.type is int or f.type == "int":
                        setattr(obj, f.name, int(raw))
                    else:
                        setattr(obj, f.name, float(raw))
                except ValueError:
                    pass
        return obj

    def to_dict(self) -> dict:
        return asdict(self)
