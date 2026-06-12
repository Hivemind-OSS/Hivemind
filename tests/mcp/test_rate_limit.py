"""Part S — TokenBucketLimiter (the per-token throttle contract).

Pure-unit contract for the one genuinely new module: continuous-refill math (N allowed,
N+1 denied with a positive ceil'd Retry-After, a full window re-allows, a partial window
accrues exactly its floor of tokens), the `limit<=0` DISABLED sentinel (the AC4
`HIVE_HTTP_RATE_LIMIT=0` path), per-key independence (AC4 — one flooding device never
throttles another), and the BOUNDED LRU map (attacker-varied keys cannot grow memory).
Deterministic via an injected clock — no sleeps, no wall time. The mutations
(always-allow; `tokens > 0` → `>= 0`; LRU→MRU eviction flip) live in `check`/`_touch`.
"""
from __future__ import annotations

from hive.app.rate_limit import RateLimitResult, TokenBucketLimiter


class _Clock:
    """Injected deterministic clock (the `now` seam)."""
    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def advance(self, dt: float) -> None:
        self.t += dt

    def __call__(self) -> float:
        return self.t


def _limiter(limit: int = 3, window_s: float = 60.0, **kw) -> tuple[TokenBucketLimiter, _Clock]:
    clock = _Clock()
    return TokenBucketLimiter(limit=limit, window_s=window_s, now=clock, **kw), clock


# ── refill math ────────────────────────────────────────────────────────────────
def test_first_n_allowed_then_n_plus_1_denied():
    lim, _ = _limiter(limit=3)
    results = [lim.check("alice") for _ in range(3)]
    assert all(r.allowed and r.retry_after_s == 0 for r in results)
    r4 = lim.check("alice")                       # the N+1th in the same instant
    assert not r4.allowed
    assert r4.retry_after_s > 0                   # a denied check always names a wait


def test_full_window_refills_to_burst_capacity():
    lim, clock = _limiter(limit=3, window_s=60.0)
    for _ in range(3):
        lim.check("k")
    assert not lim.check("k").allowed
    clock.advance(60.0)                           # one full window ⇒ bucket is full again
    assert all(lim.check("k").allowed for _ in range(3))
    assert not lim.check("k").allowed             # …and only full, never over (cap holds)


def test_partial_window_accrues_exactly_floor_tokens():
    # 3 per 60s ⇒ one token per 20s: at +20s exactly ONE more check is allowed.
    lim, clock = _limiter(limit=3, window_s=60.0)
    for _ in range(3):
        lim.check("k")
    clock.advance(20.0)
    assert lim.check("k").allowed
    assert not lim.check("k").allowed             # the fraction past one token is NOT a token


def test_fractional_progress_is_preserved_across_checks():
    # two denied probes at +10s and +19s must not reset the accrual clock: the token that
    # completes at +20s still arrives (a naive `last=now` on every check would starve it).
    lim, clock = _limiter(limit=3, window_s=60.0)
    for _ in range(3):
        lim.check("k")
    clock.advance(10.0)
    assert not lim.check("k").allowed
    clock.advance(9.0)
    assert not lim.check("k").allowed
    clock.advance(1.0)                            # t=+20s since exhaustion
    assert lim.check("k").allowed


def test_retry_after_is_ceil_of_seconds_to_next_token():
    lim, clock = _limiter(limit=3, window_s=60.0)  # one token per 20s
    for _ in range(3):
        lim.check("k")
    assert lim.check("k").retry_after_s == 20     # denied at +0: next token lands at +20
    clock.advance(0.5)
    assert lim.check("k").retry_after_s == 20     # ceil(19.5) — whole seconds, rounded UP
    clock.advance(19.0)
    assert lim.check("k").retry_after_s == 1      # ceil(0.5) ≥ 1: never advertise a 0 wait


# ── per-key independence (AC4) ─────────────────────────────────────────────────
def test_keys_have_independent_buckets():
    lim, _ = _limiter(limit=2)
    lim.check("flooder")
    lim.check("flooder")
    assert not lim.check("flooder").allowed       # flooder throttled…
    assert lim.check("innocent").allowed          # …the other device is untouched


# ── the disabled sentinel (AC4 `=0` path) ──────────────────────────────────────
def test_limit_zero_disables_check_always_allows():
    lim, _ = _limiter(limit=0)
    results = [lim.check("k") for _ in range(1000)]
    assert all(r == RateLimitResult(True, 0) for r in results)


def test_negative_limit_also_disables():
    lim, _ = _limiter(limit=-5)
    assert all(lim.check("k").allowed for _ in range(100))


# ── bounded map (DoS resistance) ───────────────────────────────────────────────
def test_eviction_drops_least_recently_touched_key():
    lim, _ = _limiter(limit=1, max_keys=3)
    for key in ("a", "b", "c"):
        assert lim.check(key).allowed             # all three buckets now exhausted
    assert not lim.check("a").allowed             # touches a ⇒ LRU order is now b, c, a
    lim.check("d")                                # 4th key ⇒ evicts b (least recently touched)
    assert lim.check("b").allowed                 # b is FRESH again — proof it was evicted
    assert not lim.check("a").allowed             # a survived ⇒ still exhausted (NOT evicted)


def test_map_stays_bounded_under_key_flood():
    lim, _ = _limiter(limit=1, max_keys=10)
    for i in range(1000):                         # attacker-varied keys
        lim.check(f"key-{i}")
    assert len(lim._buckets) <= 10                # the DoS bound itself
