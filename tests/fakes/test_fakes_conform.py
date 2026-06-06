"""P0.0 — every fake satisfies its runtime_checkable Protocol."""
from __future__ import annotations

from hive.domain.ports import (
    Clock, EmbeddingProvider, EpisodeStore, MutableVectorIndex,
    SecretScanner, UtilityStore, VectorIndex,
)
from tests.fakes import (
    FakeClock, FakeIndex, FakeProvider,
    FakeScanner, FakeStore, FakeUtilityStore,
)


def test_fakes_satisfy_protocols() -> None:
    assert isinstance(FakeClock(), Clock)
    assert isinstance(FakeProvider(), EmbeddingProvider)
    assert isinstance(FakeIndex(), VectorIndex)
    assert isinstance(FakeIndex(), MutableVectorIndex)
    assert isinstance(FakeStore(), EpisodeStore)
    assert isinstance(FakeUtilityStore(), UtilityStore)
    assert isinstance(FakeScanner(), SecretScanner)
