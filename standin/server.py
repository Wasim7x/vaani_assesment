"""
Stand-in for the real Groq service. Mimics OpenAI /chat/completions
closely enough that relay/llm_client.py hits it unchanged by pointing
STANDIN_URL at it.

What it does (and what the brief required):
  - slow: TTFT drawn from a log-normal calibrated to the probe.
  - fail: fail_rate% of requests return HTTP 500.
  - 429: token-bucket RPM + TPM limits; body + Retry-After match Groq.
  - slower under load: ttft is multiplied by (1 + alpha * excess_in_flight).
  - streams a piece at a time: real SSE with usage chunk at the end.
  - says where it differs from the real thing: documented in docs/writeup.md,
    the biggest differences are deterministic per-token delay (real Groq
    varies more) and no true tail of multi-second 200s.

Run:
  uvicorn standin.server:app --host 127.0.0.1 --port 8000
"""
from __future__ import annotations
import asyncio
import json
import logging
import math
import random
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from standin.params import StandinParams


# --- logging ---
LOG = logging.getLogger("standin")
if not LOG.handlers:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s standin: %(message)s"))
    LOG.addHandler(h)
LOG.setLevel(logging.INFO)


# --- token bucket ---
# One bucket per limit kind. We don't try to be strictly fair under
# contention — the brief cares about the *shape* of what the client
# sees, not about fairness between fake callers.
@dataclass
class SlidingWindowBucket:
    """Count events over the trailing `window_s` seconds."""
    window_s: float
    events: deque = field(default_factory=deque)

    def count_within(self) -> float:
        now = time.monotonic()
        cutoff = now - self.window_s
        while self.events and self.events[0][0] < cutoff:
            self.events.popleft()
        return sum(amount for _, amount in self.events)

    def admit(self, limit: float, amount: float = 1.0) -> tuple[bool, float]:
        """Return (ok, retry_after_hint_seconds)."""
        now = time.monotonic()
        current = self.count_within()
        if current + amount > limit:
            # Soonest-expiring event tells us when 1 unit frees up.
            if self.events:
                oldest_t = self.events[0][0]
                retry = max(0.05, self.window_s - (now - oldest_t))
            else:
                retry = self.window_s
            return False, retry
        self.events.append((now, amount))
        return True, 0.0


# --- app state ---
class ServerState:
    def __init__(self, params: StandinParams | None = None):
        self.params = params or StandinParams.load()
        self.rpm = SlidingWindowBucket(60.0)
        self.tpm = SlidingWindowBucket(60.0)
        self.day_requests = 0
        self.day_reset_utc = _utc_midnight_epoch()
        self.in_flight = 0
        self.total_requests = 0
        self._lock = asyncio.Lock()
        LOG.info("stand-in params: %s", self.params.to_dict())

    def _maybe_reset_day(self) -> None:
        now = time.time()
        if now >= self.day_reset_utc:
            self.day_requests = 0
            self.day_reset_utc = _utc_midnight_epoch()


def _utc_midnight_epoch() -> float:
    now = datetime.now(timezone.utc)
    nxt = now.replace(hour=0, minute=0, second=0, microsecond=0)
    # tomorrow 00:00 UTC
    return nxt.timestamp() + 86400.0


STATE = ServerState()
app = FastAPI(title="Relay stand-in")


# --- helpers ---
def _rate_headers(state: ServerState) -> dict[str, str]:
    p = state.params
    rpm_used = state.rpm.count_within()
    tpm_used = state.tpm.count_within()
    return {
        "x-ratelimit-limit-requests": str(p.rpd_limit),
        "x-ratelimit-remaining-requests": str(max(0, p.rpd_limit - state.day_requests)),
        "x-ratelimit-limit-tokens": str(p.tpm_limit),
        "x-ratelimit-remaining-tokens": str(max(0, int(p.tpm_limit - tpm_used))),
        "x-ratelimit-reset-requests": "60",
        "x-ratelimit-reset-tokens": "60",
        "x-standin-rpm-used": f"{rpm_used:.0f}",
        "x-standin-in-flight": str(state.in_flight),
    }


def _rate_limited_body(kind: str, used: float, limit: float) -> dict:
    # Groq's 429 body shape — we observed both these keywords in the probe.
    if kind == "rpm":
        msg = f"Rate limit reached for requests per minute. Limit {int(limit)}, Used {int(used)}."
    else:
        msg = f"Rate limit reached for input tokens per minute. Limit {int(limit)}, Used {int(used)}."
    return {
        "error": {
            "message": msg,
            "type": "requests" if kind == "rpm" else "tokens",
            "code": "rate_limit_exceeded",
        }
    }


def _estimate_tokens_in(messages: list[dict], chars_per_token: float) -> int:
    chars = 0
    for m in messages:
        content = m.get("content", "")
        if isinstance(content, str):
            chars += len(content)
    return max(1, int(chars / chars_per_token))


def _lognormal_ms(mu: float, sigma: float) -> float:
    return math.exp(mu + sigma * random.gauss(0.0, 1.0))


def _derive_reply(messages: list[dict], max_tokens: int) -> str:
    """Make the body deterministic-ish from the last user message — long
    enough to drive several SSE chunks but never longer than max_tokens
    *chars_per_token*. Content of the reply is irrelevant for load
    testing; the shape (length, chunk count) is what matters."""
    last = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            last = m.get("content", "") or ""
            break
    seed = (last[:40] or "ok").strip() or "ok"
    canned = (
        f"[standin] echo: {seed}. "
        "This is a synthetic answer used only for load testing. "
        "It mimics streamed token output from a chat model."
    )
    # Cap by token budget.
    max_chars = max(16, max_tokens * 4)
    return canned[:max_chars]


# --- streaming body ---
async def _stream_sse(body_text: str, tokens_in: int, model: str,
                      state: ServerState):
    """Yield SSE lines. First send role, then content chunks with inter-
    token delay, then a usage chunk, then DONE. Chunks are ~4 chars each
    so the client sees a real stream."""
    chat_id = f"chatcmpl-{uuid.uuid4().hex[:10]}"
    created = int(time.time())
    base = {"id": chat_id, "object": "chat.completion.chunk",
            "created": created, "model": model}

    # First chunk: role (OpenAI pattern). Content starts after TTFT wait.
    first = dict(base)
    first["choices"] = [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]
    yield f"data: {json.dumps(first)}\n\n"

    chunk_chars = 4
    p = state.params
    tokens_out = 0
    for i in range(0, len(body_text), chunk_chars):
        piece = body_text[i: i + chunk_chars]
        tokens_out += 1
        ev = dict(base)
        ev["choices"] = [{"index": 0, "delta": {"content": piece}, "finish_reason": None}]
        yield f"data: {json.dumps(ev)}\n\n"
        # per-token delay with jitter
        delay_ms = max(0.0, p.per_token_ms_mean + random.uniform(-p.per_token_ms_jitter,
                                                                 p.per_token_ms_jitter))
        await asyncio.sleep(delay_ms / 1000.0)

    # finish
    fin = dict(base)
    fin["choices"] = [{"index": 0, "delta": {}, "finish_reason": "stop"}]
    yield f"data: {json.dumps(fin)}\n\n"

    # usage chunk (Groq-style: separate chunk with no choices)
    usage = dict(base)
    usage["choices"] = []
    usage["usage"] = {
        "prompt_tokens": tokens_in,
        "completion_tokens": tokens_out,
        "total_tokens": tokens_in + tokens_out,
    }
    yield f"data: {json.dumps(usage)}\n\n"
    yield "data: [DONE]\n\n"


# --- route ---
@app.post("/v1/chat/completions")
async def chat_completions(req: Request):
    state = STATE
    p = state.params

    try:
        body = await req.json()
    except Exception:
        return JSONResponse(
            {"error": {"message": "invalid json body", "type": "invalid_request_error"}},
            status_code=400,
        )
    messages = body.get("messages") or []
    if not isinstance(messages, list) or not messages:
        return JSONResponse(
            {"error": {"message": "messages required", "type": "invalid_request_error"}},
            status_code=400,
        )
    stream = bool(body.get("stream", False))
    model = body.get("model", "standin-qwen")
    max_tokens = int(body.get("max_tokens") or p.default_tokens_out)

    tokens_in = _estimate_tokens_in(messages, p.chars_per_token)
    reply = _derive_reply(messages, max_tokens)
    projected_tokens_out = max(1, int(len(reply) / p.chars_per_token))
    projected_total = tokens_in + projected_tokens_out

    async with state._lock:
        state._maybe_reset_day()
        # 1) daily cap
        if state.day_requests >= p.rpd_limit:
            headers = _rate_headers(state)
            headers["retry-after"] = str(int(max(1.0, _seconds_to_midnight(state))))
            return JSONResponse(
                _rate_limited_body("rpm", state.day_requests, p.rpd_limit),
                status_code=429,
                headers=headers,
            )
        # 2) RPM
        ok_rpm, retry_rpm = state.rpm.admit(p.rpm_limit, 1.0)
        if not ok_rpm:
            headers = _rate_headers(state)
            headers["retry-after"] = str(max(1, int(p.retry_after_rpm)))
            return JSONResponse(
                _rate_limited_body("rpm", state.rpm.count_within(), p.rpm_limit),
                status_code=429,
                headers=headers,
            )
        # 3) TPM — bill the projected total up front; refund of overshoot not modelled.
        ok_tpm, retry_tpm = state.tpm.admit(p.tpm_limit, float(projected_total))
        if not ok_tpm:
            # Undo RPM credit so the caller isn't double-penalised.
            try:
                state.rpm.events.pop()
            except IndexError:
                pass
            headers = _rate_headers(state)
            headers["retry-after"] = str(max(1, int(p.retry_after_tpm)))
            return JSONResponse(
                _rate_limited_body("tpm", state.tpm.count_within(), p.tpm_limit),
                status_code=429,
                headers=headers,
            )
        state.day_requests += 1
        state.total_requests += 1
        state.in_flight += 1
        in_flight_at_entry = state.in_flight

    # 4) random failure injection (before TTFT so it looks like a real
    #    server-side 500 hitting during the handshake)
    if random.random() < p.fail_rate:
        state.in_flight -= 1
        LOG.info("req=%s inject 500", body.get("request_id", "-"))
        return JSONResponse(
            {"error": {"message": "stand-in injected failure",
                       "type": "server_error", "code": "injected_500"}},
            status_code=500,
            headers=_rate_headers(state),
        )

    # 5) TTFT wait, with load-based multiplier + occasional slow tail.
    ttft_ms = _lognormal_ms(p.ttft_mu, p.ttft_sigma)
    excess = max(0, in_flight_at_entry - p.load_slowdown_floor)
    ttft_ms *= (1.0 + p.load_slowdown_alpha * excess)
    if random.random() < p.slow_tail_rate:
        ttft_ms += 1500.0

    await asyncio.sleep(ttft_ms / 1000.0)

    LOG.info(
        "req ok model=%s tokens_in=%d in_flight=%d ttft_ms=%.0f stream=%s",
        model, tokens_in, in_flight_at_entry, ttft_ms, stream,
    )

    if not stream:
        # Non-streaming reply — still after the ttft delay.
        resp = {
            "id": f"chatcmpl-{uuid.uuid4().hex[:10]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": reply},
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens": tokens_in,
                "completion_tokens": projected_tokens_out,
                "total_tokens": projected_total,
            },
        }
        state.in_flight -= 1
        return JSONResponse(resp, headers=_rate_headers(state))

    async def _gen():
        try:
            async for line in _stream_sse(reply, tokens_in, model, state):
                yield line
        finally:
            state.in_flight -= 1

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers=_rate_headers(state),
    )


@app.get("/healthz")
async def healthz():
    return {
        "ok": True,
        "in_flight": STATE.in_flight,
        "total_requests": STATE.total_requests,
        "day_requests": STATE.day_requests,
        "params": STATE.params.to_dict(),
    }


def _seconds_to_midnight(state: ServerState) -> float:
    return max(1.0, state.day_reset_utc - time.time())
