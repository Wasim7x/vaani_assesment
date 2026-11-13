"""
The scheduler sits between the pipeline and the LLM client. It
owns:

  - Client-side RPM + TPM budgeting at 80% of the measured limit
    (see DECISIONS.md Decision 6).
  - A cap on how many requests talk to the server at once.
  - A queue with a wall-clock deadline (the brief says people give
    up sooner than you expect). Requests that would miss the
    deadline are dropped before any HTTP call.
  - A retry policy that honors the server's `Retry-After`.
  - A circuit breaker: after N 5xx/timeouts in a row, we stop
    calling for a short cool-down.
  - Daily-budget modes: when the ceiling is close, shrink
    max_tokens; when it is reached, drop with no HTTP call.
  - A naive mode (for the required A/B): fire-and-retry with no
    budget check, no deadline, no cap.

Every step emits structured events that the pipeline folds into
Trace, which `make_tables.py` and the write-up can read later.

Design note: the brief says "nobody turned away" is one of the four
goals we cannot all meet. I chose to turn people away (status =
timeout) instead of sending a wrong or slow answer.
"""
from __future__ import annotations
import asyncio
import logging
import random
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from relay.config import SCHEDULER, SchedulerConfig
from relay.llm_client import LLMResult, chat_stream
from relay.budget import record as budget_record, remaining as budget_remaining
from relay.metrics import LOG


# -----------------------------------------------------------------
# Token-bucket style sliding-window limiter. One event = 1 request.
# One event (weight t) = t tokens. 60-second window matches Groq.
# -----------------------------------------------------------------
class SlidingWindow:
    def __init__(self, window_s: float = 60.0):
        self.window_s = window_s
        self._events: deque = deque()

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_s
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()

    def count(self) -> float:
        self._prune(time.monotonic())
        return sum(w for _, w in self._events)

    def earliest_free_in(self, needed: float, limit: float) -> float:
        """Seconds until there is room for `needed`."""
        now = time.monotonic()
        self._prune(now)
        used = sum(w for _, w in self._events)
        headroom = limit - used
        if headroom >= needed:
            return 0.0
        if not self._events:
            return 0.0
        # free capacity as the oldest events expire, oldest first
        running = 0.0
        for t, w in self._events:
            running += w
            if headroom + running >= needed:
                age = now - t
                return max(0.0, self.window_s - age)
        # Not enough capacity will ever fit (needed > limit): return
        # window so the caller sees "no" cleanly.
        return self.window_s

    def record(self, weight: float = 1.0) -> None:
        self._events.append((time.monotonic(), weight))


# -----------------------------------------------------------------
# Circuit breaker — three states (closed, open, half_open). We open
# after `fail_threshold` consecutive hard failures (5xx or timeout).
# -----------------------------------------------------------------
@dataclass
class Breaker:
    fail_threshold: int = 5
    cooldown_s: float = 10.0
    state: str = "closed"
    consecutive_fails: int = 0
    opened_at: float = 0.0

    def allow(self) -> bool:
        if self.state == "closed":
            return True
        if self.state == "open":
            if time.monotonic() - self.opened_at >= self.cooldown_s:
                self.state = "half_open"
                return True
            return False
        return True  # half_open: let one probe through

    def on_success(self) -> None:
        self.consecutive_fails = 0
        self.state = "closed"

    def on_failure(self) -> None:
        self.consecutive_fails += 1
        if self.consecutive_fails >= self.fail_threshold:
            self.state = "open"
            self.opened_at = time.monotonic()


# -----------------------------------------------------------------
# A submitted request. `deadline_ms` is from the caller's perspective
# (when the person gives up). `projected_in` is our best guess at
# input tokens for TPM pre-check; refined by prompt length.
# -----------------------------------------------------------------
@dataclass
class SchedRequest:
    messages: list[dict]
    max_tokens: int
    projected_in: int
    deadline_ms: int
    base_url: str | None = None
    model: str | None = None
    priority: int = 1          # smaller = more important
    submitted_at: float = field(default_factory=time.monotonic)
    attrs: dict = field(default_factory=dict)


@dataclass
class SchedResult:
    """What the scheduler returns to the pipeline."""
    llm: LLMResult
    retries: int = 0
    waited_ms: float = 0.0           # queue + rate-wait before first call
    working_ms: float = 0.0          # sum of LLMResult.total_ms over all attempts
    retry_wait_ms: float = 0.0       # sum of sleeps between attempts
    outcome: str = "ok"              # ok | rate_limited | timeout | error | dropped | breaker_open
    attempts: list[dict] = field(default_factory=list)


class Scheduler:
    def __init__(self,
                 cfg: SchedulerConfig = SCHEDULER,
                 breaker: Breaker | None = None,
                 use_budget: bool = True,
                 naive: bool = False,
                 max_queue: int = 64):
        self.cfg = cfg
        self.rpm = SlidingWindow(60.0)
        self.tpm = SlidingWindow(60.0)
        self.in_flight_sem = asyncio.Semaphore(cfg.max_in_flight)
        self._queue_depth = 0
        self.peak_queue_depth = 0
        self.breaker = breaker or Breaker()
        self.use_budget = use_budget
        self.naive = naive
        self.max_queue = max_queue
        self._queue_lock = asyncio.Lock()
        # effective limits (safety margin)
        self.eff_rpm = int(cfg.rpm_limit * cfg.safety_margin)
        self.eff_tpm = int(cfg.tpm_limit * cfg.safety_margin)
        self.eff_itpm = int(cfg.itpm_limit * cfg.safety_margin)
        LOG.info("scheduler eff_rpm=%d eff_tpm=%d eff_itpm=%d in_flight=%d naive=%s",
                 self.eff_rpm, self.eff_tpm, self.eff_itpm, cfg.max_in_flight, naive)

    # ---------- public ----------
    async def submit(self, req: SchedRequest) -> SchedResult:
        # Daily degrade: if close to the daily ceiling, shrink max_tokens
        # so cheaper requests still get through. See brief §7.
        # Only the real Groq service is bounded by this ledger; stand-in
        # runs (base_url set) are free and should not read/write it.
        if self.use_budget and (not self.naive) and (req.base_url is None):
            remaining = budget_remaining()
            if remaining <= 0:
                LOG.warning("daily budget exhausted; dropping request")
                return SchedResult(
                    llm=LLMResult(status="error", error="daily_budget_exhausted"),
                    outcome="dropped",
                )
            if remaining < 25:
                req.max_tokens = min(req.max_tokens, 128)
                req.attrs["degraded_tokens"] = True

        if not self.breaker.allow():
            LOG.warning("breaker open; refusing request")
            return SchedResult(
                llm=LLMResult(status="error", error="breaker_open"),
                outcome="breaker_open",
            )

        # Admission: bounded queue depth. The naive variant skips the
        # queue entirely and goes straight to the client.
        if self.naive:
            return await self._naive_call(req)
        return await self._policy_call(req)

    # ---------- policy mode ----------
    async def _policy_call(self, req: SchedRequest) -> SchedResult:
        result = SchedResult(llm=LLMResult(status="error", error="unknown"))

        # Admission queue depth check
        async with self._queue_lock:
            if self._queue_depth >= self.max_queue:
                LOG.warning("queue full depth=%d", self._queue_depth)
                result.llm = LLMResult(status="error", error="queue_full")
                result.outcome = "dropped"
                return result
            self._queue_depth += 1
            self.peak_queue_depth = max(self.peak_queue_depth, self._queue_depth)

        try:
            wait_start = time.monotonic()
            # Serialise entry into in_flight after our rate pre-wait.
            # Order: (1) wait on RPM, (2) wait on TPM, (3) acquire in-flight, (4) call.
            # Deadline is checked at each gate.
            def deadline_left_ms() -> float:
                elapsed_ms = (time.monotonic() - req.submitted_at) * 1000
                return req.deadline_ms - elapsed_ms

            # --- rate pre-wait ---
            while True:
                wait_rpm = self.rpm.earliest_free_in(1.0, self.eff_rpm)
                wait_tpm_total = self.tpm.earliest_free_in(
                    float(req.projected_in + req.max_tokens), self.eff_tpm)
                wait_s = max(wait_rpm, wait_tpm_total)
                if wait_s <= 0:
                    break
                left_ms = deadline_left_ms()
                if wait_s * 1000 > left_ms:
                    LOG.info("deadline-drop before send: wait=%.2fs left=%.0fms",
                             wait_s, left_ms)
                    result.llm = LLMResult(status="timeout",
                                           error="deadline_before_send")
                    result.outcome = "timeout"
                    result.waited_ms = (time.monotonic() - wait_start) * 1000
                    return result
                await asyncio.sleep(wait_s)

            # --- in-flight gate ---
            # We may be queued here if max_in_flight < demand. The
            # semaphore will respect FIFO (asyncio's default). Deadline
            # check again right after we acquire, in case we waited long.
            acquire_wait_start = time.monotonic()
            try:
                await asyncio.wait_for(self.in_flight_sem.acquire(),
                                       timeout=max(0.001, deadline_left_ms() / 1000))
            except asyncio.TimeoutError:
                LOG.info("deadline-drop at in-flight gate")
                result.llm = LLMResult(status="timeout",
                                       error="deadline_in_flight_gate")
                result.outcome = "timeout"
                result.waited_ms = (time.monotonic() - wait_start) * 1000
                return result
            acquire_wait_ms = (time.monotonic() - acquire_wait_start) * 1000

            result.waited_ms = (time.monotonic() - wait_start) * 1000

            try:
                # Record intent (so other concurrent submits see this one
                # consumes capacity).
                self.rpm.record(1.0)
                self.tpm.record(float(req.projected_in + req.max_tokens))

                # --- attempt loop with retries ---
                attempt = 0
                while True:
                    attempt_t0 = time.monotonic()
                    left_ms = deadline_left_ms()
                    if left_ms <= 0:
                        LOG.info("deadline-drop before attempt %d", attempt)
                        result.llm = LLMResult(status="timeout",
                                               error="deadline_before_attempt")
                        result.outcome = "timeout"
                        break

                    request_timeout = max(0.5, min(30.0, left_ms / 1000))
                    llm = await chat_stream(
                        req.messages,
                        base_url=req.base_url,
                        model=req.model,
                        max_tokens=req.max_tokens,
                        request_timeout=request_timeout,
                    )
                    result.llm = llm
                    result.working_ms += llm.total_ms
                    result.attempts.append({
                        "attempt": attempt,
                        "status": llm.status,
                        "total_ms": llm.total_ms,
                        "retry_after": llm.retry_after,
                    })
                    # Only the real Groq service draws from the daily
                    # budget. Stand-in calls (base_url set) are free.
                    if self.use_budget and (req.base_url is None):
                        try:
                            budget_record(
                                tokens_in=llm.tokens_in,
                                tokens_out=llm.tokens_out,
                                rate_limited=(llm.status == "rate_limited"),
                            )
                        except Exception:
                            pass

                    if llm.status == "ok":
                        self.breaker.on_success()
                        result.outcome = "ok"
                        break

                    # Decide: retry?
                    retriable = llm.status in ("rate_limited", "timeout", "error")
                    if llm.http_status and 400 <= llm.http_status < 500 and llm.status != "rate_limited":
                        # client-side 4xx isn't worth retrying.
                        retriable = False

                    if llm.status == "rate_limited":
                        pass  # don't trip breaker for 429
                    elif retriable:
                        self.breaker.on_failure()

                    if not retriable or attempt >= self.cfg.retry_budget_per_req:
                        result.outcome = llm.status
                        break

                    # Backoff: prefer server-told retry-after; else
                    # exponential backoff with jitter capped.
                    if llm.retry_after and llm.retry_after > 0:
                        wait_s = llm.retry_after
                    else:
                        base = self.cfg.backoff_base_ms * (2 ** attempt)
                        cap = self.cfg.backoff_cap_ms
                        wait_s = min(cap, base) / 1000
                        wait_s *= 0.5 + random.random()  # jitter

                    left_ms = deadline_left_ms()
                    if wait_s * 1000 > left_ms:
                        LOG.info("deadline-drop in retry wait: wait=%.2fs left=%.0fms",
                                 wait_s, left_ms)
                        result.llm = LLMResult(status="timeout",
                                               error="deadline_in_retry")
                        result.outcome = "timeout"
                        break

                    await asyncio.sleep(wait_s)
                    result.retry_wait_ms += wait_s * 1000
                    attempt += 1
                    result.retries = attempt
            finally:
                self.in_flight_sem.release()
        finally:
            async with self._queue_lock:
                self._queue_depth -= 1

        return result

    # ---------- naive mode (A/B comparison) ----------
    async def _naive_call(self, req: SchedRequest) -> SchedResult:
        """No rate budgeting, no deadline, naive exponential retry on any
        error or 429. Used only in the required naive-vs-policy A/B."""
        result = SchedResult(llm=LLMResult(status="error", error="unknown"))
        attempt = 0
        while True:
            llm = await chat_stream(
                req.messages,
                base_url=req.base_url,
                model=req.model,
                max_tokens=req.max_tokens,
                request_timeout=30.0,
            )
            result.llm = llm
            result.working_ms += llm.total_ms
            result.attempts.append({
                "attempt": attempt, "status": llm.status,
                "total_ms": llm.total_ms, "retry_after": llm.retry_after,
            })
            if llm.status == "ok":
                result.outcome = "ok"
                break
            if attempt >= self.cfg.retry_budget_per_req:
                result.outcome = llm.status
                break
            # Naive: fixed backoff, do NOT honour retry-after even when
            # the server tells us one. This is on purpose — it's the
            # "one case where retrying made things worse" the brief asks
            # us to call out.
            wait_s = (self.cfg.backoff_base_ms * (2 ** attempt)) / 1000.0
            await asyncio.sleep(wait_s)
            result.retry_wait_ms += wait_s * 1000
            attempt += 1
            result.retries = attempt
        return result
