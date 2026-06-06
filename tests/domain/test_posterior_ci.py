"""P0.3 — Beta-Bernoulli posterior CI gate [D2]: utility never moves ranking until
the posterior is confident (the 90% normal-approx CI excludes the no-signal point 0.5)."""
from __future__ import annotations

from hive.domain.models import UtilityPosterior

FAM = "r|python|bugfix"


def _p(wins, losses):
    return UtilityPosterior(episode_id=1, family_scope=FAM, wins=wins, losses=losses)


def test_mean_is_beta_mean_with_prior():
    assert _p(0, 0).mean() == 0.5            # fresh ⇒ neutral
    assert abs(_p(8, 1).mean() - 9 / 11) < 1e-9


def test_ci_includes_half_when_sparse():
    assert _p(1, 0).ci_excludes_half() is False   # too sparse ⇒ absent from confident map
    assert _p(0, 0).ci_excludes_half() is False


def test_ci_excludes_half_when_confident():
    assert _p(20, 2).ci_excludes_half() is True    # confident-positive
    assert _p(1, 8).ci_excludes_half() is True     # confident-negative (demotes)
