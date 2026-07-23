"""SystemClock — the real wall-clock adapter behind the Clock port."""

from __future__ import annotations

from hive.adapters.clock_system import SystemClock
from hive.domain.ports import Clock


def test_now_is_int_epoch_seconds():
    t = SystemClock().now()
    assert isinstance(t, int)
    assert t > 1_600_000_000  # after 2020 — proves it is real epoch seconds, not 0


def test_now_is_non_decreasing():
    c = SystemClock()
    a = c.now()
    b = c.now()
    assert b >= a


def test_conforms_to_clock_protocol():
    assert isinstance(SystemClock(), Clock)
