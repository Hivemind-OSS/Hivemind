"""P0.0 — every fake satisfies its runtime_checkable Protocol."""
from __future__ import annotations

from hive.domain.ports import (
    Clock, ConflictFlagStore, EmbeddingProvider, MetaStore, MutableVectorIndex,
    SecretScanner, VectorIndex,
)
from tests.fakes import (
    FakeClock, FakeConflictFlagStore, FakeIndex, FakeProvider,
    FakeScanner, FakeStore,
)


def test_fakes_satisfy_protocols() -> None:
    assert isinstance(FakeClock(), Clock)
    assert isinstance(FakeProvider(), EmbeddingProvider)
    assert isinstance(FakeIndex(), VectorIndex)
    assert isinstance(FakeIndex(), MutableVectorIndex)
    assert isinstance(FakeStore(), MetaStore)
    assert isinstance(FakeScanner(), SecretScanner)
    assert isinstance(FakeConflictFlagStore(), ConflictFlagStore)
