"""TokenBucketLimiter — the per-token throttle for the HTTP transport (Part S).

Under a public tunnel every request shares the tunnel's egress IP, so the load-bearing
limiter is POST-AUTH, keyed on the VERIFIED device label (D3) — never
a source/forwarded IP, which sidesteps the X-Forwarded-For trust hazard entirely. One
continuous-refill bucket per key, held in a BOUNDED LRU map so attacker-varied keys can
never grow memory past ``max_keys``.

Pure and deterministic: the clock is injected (``now``), and there is NO internal lock —
the transport calls ``check()`` under its existing global handler lock, so adding one
here would only add a second concurrency surface. ``limit <= 0`` constructs the DISABLED
sentinel (``check()`` always allows) — a caller-level capability; the operator HTTP belt
is always on (no env knob disables it). Stdlib only (AC6 — zero new runtime dependency).
"""
from __future__ import annotations

import math
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class RateLimitResult:
    """A ``check()`` verdict. ``retry_after_s`` is 0 when allowed; when denied it is
    ceil(seconds until the bucket accrues its next token) — whole seconds ≥ 1, ready to
    serve verbatim as the HTTP ``Retry-After`` header."""
    allowed: bool
    retry_after_s: int


class TokenBucketLimiter:
    """A continuous-refill token bucket per key, in an LRU-bounded map.

    A new key starts FULL (``limit`` tokens — burst capacity equals the per-window
    budget); each allowed check consumes one; tokens accrue at ``limit / window_s`` per
    second, credited in WHOLE tokens (floor) with the fractional remainder preserved
    (``last`` advances only by the credited intervals, so steady probing never starves
    the refill). Inserting past ``max_keys`` evicts the least-recently-TOUCHED key —
    memory is O(max_keys) no matter how many distinct keys are presented. // check(): O(1).
    """

    def __init__(self, *, limit: int, window_s: float, max_keys: int = 10_000,
                 now: Callable[[], float] = time.monotonic) -> None:
        self._limit = int(limit)                  # <=0 ⇒ DISABLED sentinel (always allow)
        self._window_s = float(window_s)
        self._max_keys = int(max_keys)
        self._now = now
        self._buckets: OrderedDict[str, tuple[int, float]] = OrderedDict()  # key → (tokens, last)

    def check(self, key: str) -> RateLimitResult:
        """Consume one token for ``key`` if available; deny with the seconds-to-next-token
        otherwise. Disabled (``limit<=0``) ⇒ always allowed, map untouched. // O(1)."""
        if self._limit <= 0:
            return RateLimitResult(True, 0)
        now = self._now()
        interval = self._window_s / self._limit            # seconds per token
        entry = self._buckets.get(key)
        if entry is None:
            tokens, last = self._limit, now                # new key ⇒ full bucket
        else:
            tokens, last = entry
            refill = int((now - last) / interval)          # floor: whole tokens accrued
            if refill > 0:
                tokens += refill
                if tokens >= self._limit:                  # capped ⇒ re-anchor (no backlog)
                    tokens, last = self._limit, now
                else:
                    last += refill * interval              # keep the fractional remainder
        if tokens > 0:                 # ← fault: `>= 0` is the off-by-one mutation
            self._touch(key, tokens - 1, last)
            return RateLimitResult(True, 0)
        # denied: the next token completes at last+interval (floor above guarantees
        # now - last < interval, so the wait is strictly positive; max() is the belt).
        retry = max(1, math.ceil(last + interval - now))
        self._touch(key, tokens, last)
        return RateLimitResult(False, retry)

    def _touch(self, key: str, tokens: int, last: float) -> None:
        """Upsert + mark ``key`` most-recently-used; evict the LRU key past ``max_keys``."""
        self._buckets[key] = (tokens, last)
        self._buckets.move_to_end(key)
        while len(self._buckets) > self._max_keys:
            self._buckets.popitem(last=False)              # least-recently-touched dies first
