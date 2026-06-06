"""P0.2 — pure Attributor.split (conserved margin-split) [A3][A5]."""
from __future__ import annotations

import random

from hive.domain.attribution import Attributor, CreditDelta
from hive.domain.models import SettledOutcome
from tests.fakes import FakeUtilityStore

FAM = "r|python|bugfix"


def _pos(mag=0.2):
    return SettledOutcome(family_scope=FAM, reward_sign=+1, magnitude=mag, source_agent="A")


def test_split_proportional_to_margin():
    deltas = Attributor().split(_pos(0.2), [(1, 0.6), (2, 0.4)], isolation=set())
    by = {d.episode_id: d for d in deltas}
    assert abs(by[1].d_wins - 0.12) < 1e-9 and abs(by[2].d_wins - 0.08) < 1e-9
    assert by[1].d_losses == 0 and by[2].d_losses == 0


def test_split_conserves_randomized():
    """Property (no hypothesis): random non-negative margins incl. ties/zeros,
    magnitude ∈ (0,1] ⟹ Σ(d_wins+d_losses) == magnitude; all-zero ⟹ uniform 1/n."""
    rng = random.Random(1234)
    a = Attributor()
    for _ in range(500):
        n = rng.randint(1, 8)
        margins = [rng.choice([0.0, 0.0, rng.random(), round(rng.random(), 2)]) for _ in range(n)]
        mag = rng.random() or 1.0
        sign = rng.choice([-1, 1])
        exposed = list(enumerate(margins, start=1))
        deltas = a.split(SettledOutcome(FAM, sign, mag, "A"), exposed, isolation=set())
        total = sum(d.d_wins + d.d_losses for d in deltas)
        assert abs(total - mag) < 1e-9
        if all(m == 0.0 for m in margins):           # all-zero ⇒ uniform
            shares = [d.d_wins + d.d_losses for d in deltas]
            assert all(abs(s - mag / n) < 1e-9 for s in shares)


def test_credit_writes_posterior_never_weight():
    store = FakeUtilityStore()
    store.apply_credit(Attributor().split(_pos(0.2), [(1, 0.6), (2, 0.4)], isolation=set()))
    assert abs(store.posterior(1, FAM).wins - 0.12) < 1e-9
    assert abs(store.posterior(2, FAM).wins - 0.08) < 1e-9
    assert store.posterior(1, FAM).losses == 0
    # the utility store exposes NO weight setter and NO episodes handle [A3]
    assert not any(hasattr(store, m) for m in ("bump_weight", "set_weight", "update_weight"))


def test_clawback_writes_losses():
    deltas = Attributor().split(
        SettledOutcome(FAM, -1, 1.0, "A"), [(1, 0.6), (2, 0.4)], isolation=set())
    by = {d.episode_id: d for d in deltas}
    assert abs(by[1].d_losses - 0.6) < 1e-9 and abs(by[2].d_losses - 0.4) < 1e-9
    assert by[1].d_wins == 0 and by[2].d_wins == 0


def test_isolation_ids_never_credited():
    deltas = Attributor().split(_pos(0.2), [(1, 0.6), (2, 0.4)], isolation={2})
    assert {d.episode_id for d in deltas} == {1}  # eid 2 held out, never reweighted


def test_reject_zero_sign():
    import pytest
    with pytest.raises(ValueError):
        SettledOutcome(FAM, 0, 0.0, "A")   # a CI-green ~0 event is unconstructable as credit
