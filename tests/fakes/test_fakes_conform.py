"""P0.0 — every fake satisfies its runtime_checkable Protocol."""
from __future__ import annotations

from hive.domain.ports import (
    Clock, EmbeddingProvider, EpisodeStore, ExposureLedger, MutableVectorIndex,
    OutcomeSource, SecretScanner, UtilityStore, VectorIndex,
)
from tests.fakes import (
    FakeClock, FakeExposureLedger, FakeIndex, FakeOutcomeSource, FakeProvider,
    FakeScanner, FakeStore, FakeUtilityStore,
)


def test_fakes_satisfy_protocols() -> None:
    assert isinstance(FakeClock(), Clock)
    assert isinstance(FakeProvider(), EmbeddingProvider)
    assert isinstance(FakeIndex(), VectorIndex)
    assert isinstance(FakeIndex(), MutableVectorIndex)
    assert isinstance(FakeStore(), EpisodeStore)
    assert isinstance(FakeStore(), ExposureLedger)
    assert isinstance(FakeExposureLedger(), ExposureLedger)
    assert isinstance(FakeUtilityStore(), UtilityStore)
    assert isinstance(FakeOutcomeSource(), OutcomeSource)
    assert isinstance(FakeScanner(), SecretScanner)
