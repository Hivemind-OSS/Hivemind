"""Conforming in-memory fakes for the pure-domain suite."""
from tests.fakes._fakes import (
    FakeClock,
    FakeIndex,
    FakeProvider,
    FakeScanner,
    FakeStore,
    FakeUtilityStore,
)

__all__ = [
    "FakeClock", "FakeIndex", "FakeProvider", "FakeScanner",
    "FakeStore", "FakeUtilityStore",
]
