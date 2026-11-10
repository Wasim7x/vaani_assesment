"""
Thin async client for Groq's OpenAI-compatible chat/completions, plus
the same shape pointed at our stand-in. Always streams, so we can
measure time-to-first-token separately from total time.

What this returns is deliberately flat: a dataclass with everything
the scheduler and metrics code need (status, timings, tokens, headers,
error). No retries here — the scheduler layer owns that in Phase 5.
"""
from __future__ import annotations
import asyncio
import json
import os
import time
from dataclasses import dataclass, field

import httpx
from dotenv import load_dotenv

from relay.config import GROQ_BASE_URL, GROQ_MODEL


load_dotenv()


@dataclass
class LLMResult:
    status: str                       # ok | rate_limited | timeout | error
    text: str = ""
    first_token_ms: float | None = None
    total_ms: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    http_status: int | None = None
    error: str = ""
    retry_after: float | None = None  # seconds, from Retry-After header
    rate_limit_headers: dict[str, str] = field(default_factory=dict)


RATE_HEADERS = (
    "x-ratelimit-limit-requests",
    "x-ratelimit-limit-tokens",
    "x-ratelimit-remaining-requests",
    "x-ratelimit-remaining-tokens",
    "x-ratelimit-reset-requests",
    "x-ratelimit-reset-tokens",
    "retry-after",
)


def _collect_rate_headers(h: httpx.Headers) -> dict[str, str]:
    return {k: h[k] for k in RATE_HEADERS if k in h}


def _parse_retry_after(raw: str | None) -> float | None:
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _parse_sse_chunk(line: str) -> dict | None:
    """SSE line like `data: {...}` -> dict; `data: [DONE]` -> None."""
    if not line.startswith("data:"):
        return None
    payload = line[5:].strip()
    if not payload or payload == "[DONE]":
        return None
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return None


async def chat_stream(
    messages: list[dict],
    *,
    base_url: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    max_tokens: int = 512,
    temperature: float = 0.0,
    request_timeout: float = 30.0,
) -> LLMResult:
    base = base_url or os.environ.get("GROQ_BASE_URL", GROQ_BASE_URL)
    model = model or os.environ.get("GROQ_MODEL", GROQ_MODEL)
    key = api_key or os.environ.get("GROQ_API_KEY", "")

    # Point at the stand-in? no auth header needed, but Groq refuses
    # without one. Only send the auth header when the URL looks like
    # Groq.
    headers = {"content-type": "application/json"}
    if "groq.com" in base:
        if not key:
            return LLMResult(status="error", error="GROQ_API_KEY not set")
        headers["authorization"] = f"Bearer {key}"

    body = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},   # Groq echoes usage on the last chunk
    }
    url = f"{base.rstrip('/')}/chat/completions"
    t0 = time.perf_counter()
    first_tok_t: float | None = None
    text_parts: list[str] = []
    tokens_in = 0
    tokens_out = 0

    try:
        timeout = httpx.Timeout(request_timeout, connect=10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", url, json=body, headers=headers) as r:
                rate_hdrs = _collect_rate_headers(r.headers)
                if r.status_code == 429:
                    raw_body = (await r.aread()).decode("utf-8", errors="replace")
                    return LLMResult(
                        status="rate_limited",
                        http_status=429,
                        error=raw_body[:500],
                        retry_after=_parse_retry_after(r.headers.get("retry-after")),
                        rate_limit_headers=rate_hdrs,
                        total_ms=(time.perf_counter() - t0) * 1000,
                    )
                if r.status_code >= 400:
                    raw_body = (await r.aread()).decode("utf-8", errors="replace")
                    return LLMResult(
                        status="error",
                        http_status=r.status_code,
                        error=raw_body[:500],
                        rate_limit_headers=rate_hdrs,
                        total_ms=(time.perf_counter() - t0) * 1000,
                    )
                async for line in r.aiter_lines():
                    if not line:
                        continue
                    chunk = _parse_sse_chunk(line)
                    if chunk is None:
                        continue
                    # usage chunk comes last; has no choices on Groq
                    usage = chunk.get("usage")
                    if usage:
                        tokens_in = usage.get("prompt_tokens", tokens_in)
                        tokens_out = usage.get("completion_tokens", tokens_out)
                    for ch in chunk.get("choices", []):
                        delta = ch.get("delta", {}).get("content")
                        if delta:
                            if first_tok_t is None:
                                first_tok_t = time.perf_counter()
                            text_parts.append(delta)
        return LLMResult(
            status="ok",
            text="".join(text_parts),
            first_token_ms=((first_tok_t - t0) * 1000) if first_tok_t else None,
            total_ms=(time.perf_counter() - t0) * 1000,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            http_status=200,
            rate_limit_headers=rate_hdrs,
        )
    except httpx.TimeoutException as e:
        return LLMResult(status="timeout", error=str(e),
                         total_ms=(time.perf_counter() - t0) * 1000)
    except httpx.HTTPError as e:
        return LLMResult(status="error", error=str(e),
                         total_ms=(time.perf_counter() - t0) * 1000)


def chat_stream_sync(messages: list[dict], **kwargs) -> LLMResult:
    """Blocking wrapper so scripts that don't need concurrency stay simple."""
    return asyncio.run(chat_stream(messages, **kwargs))
