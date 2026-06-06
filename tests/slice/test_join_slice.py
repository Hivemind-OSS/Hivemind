"""P0.6 — the assembled OutcomeProducer round-trip: the §11 trace↔outcome join end
to end on a 2-memory store (real SQLite ledger + utility; faked source + clock).
Green here RETIRES R1. The 7-test contract + the one-tx atomicity guarantee [A7]."""
from __future__ import annotations

import random

from hive.adapters.sqlite_db import connect
from hive.adapters.store_sqlite import SqliteEpisodeStore
from hive.adapters.utility_store_sqlite import SqliteUtilityStore
from hive.domain.attribution import Attributor, CreditDelta
from hive.domain.join import OutcomeJoiner
from hive.domain.models import CommitFact, Scored, SourcePoll
from hive.domain.produce import OutcomeProducer
from hive.domain.surfacer import UtilitySurfacer
from tests.fakes import FakeClock, FakeOutcomeSource

DAY = 86_400
FAM = "r|python|general"
SETTLE_OFFSET = 7 * DAY


def _build(poll=None, raises=False):
    conn = connect(":memory:")
    ledger = SqliteEpisodeStore(conn)
    util = SqliteUtilityStore(conn)
    joiner = OutcomeJoiner(assoc_window_s=1800, settle_days=7, provisional_reward=0.2,
                           clawback_reward=-1.0, require_stamp=False, assoc_epsilon=0.0,
                           rng=random.Random(0))
    source = FakeOutcomeSource(poll or SourcePoll(repo_remote="r"), raises=raises)
    producer = OutcomeProducer(source=source, joiner=joiner, attributor=Attributor(),
                               store=ledger, utility_store=util, clock=FakeClock())
    return producer, ledger, util, source


def _commit(**kw):
    base = dict(sha="c1", ts=1100, repo_remote="r", files_touched=("a.py",))
    base.update(kw)
    return CommitFact(**base)


def _expose(ledger):
    ledger.record_exposure("trace1", [(1, 0.6), (2, 0.4)], ts=1000)


# 1 ───────────────────────────────────────────────────────────────────────────
def test_commit_links_window_traces():
    p, ledger, _, source = _build()
    _expose(ledger)
    source._poll = SourcePoll(repo_remote="r", commits=(_commit(),))
    p.step(now=1100)
    row = ledger.get_outcome("c1", "trace1")
    assert row is not None and row.state == "provisional" and row.family_scope == FAM
    assert ledger.task_ref_of("trace1") == "c1"


# 2 ───────────────────────────────────────────────────────────────────────────
def test_provisional_does_not_credit_before_settle():
    p, ledger, util, source = _build()
    _expose(ledger)
    source._poll = SourcePoll(repo_remote="r", commits=(_commit(),))
    p.step(now=1100)
    assert util.posterior(1, FAM).wins == 0 and util.posterior(2, FAM).wins == 0


# 3 ───────────────────────────────────────────────────────────────────────────
def test_settle_after_clean_days_moves_posterior():
    p, ledger, util, source = _build()
    _expose(ledger)
    source._poll = SourcePoll(repo_remote="r", commits=(_commit(),))
    p.step(now=1100)
    source._poll = SourcePoll(repo_remote="r", commits=())   # clean settle tick
    p.step(now=1100 + SETTLE_OFFSET + 1)
    assert abs(util.posterior(1, FAM).wins - 0.12) < 1e-9
    assert abs(util.posterior(2, FAM).wins - 0.08) < 1e-9


# 4 ───────────────────────────────────────────────────────────────────────────
def test_revert_claws_back_and_demotes():
    p, ledger, util, source = _build()
    _expose(ledger)
    source._poll = SourcePoll(repo_remote="r", commits=(_commit(),))
    p.step(now=1100)
    source._poll = SourcePoll(repo_remote="r",
                              commits=(CommitFact(sha="rev", ts=1200, kind="revert",
                                                  reverts="c1", repo_remote="r"),))
    p.step(now=1200)
    assert abs(util.posterior(1, FAM).losses - 0.6) < 1e-9
    assert ledger.get_outcome("c1", "trace1").state == "clawed_back"
    # demotion: a confident-negative memory ranks below an un-credited tie (catches the
    # `1+max(0,u)` floor that could only promote). Drive eid1 confident-negative:
    util.apply_credit([CreditDelta(1, FAM, 0.0, 8.0, "A")])
    umap = util.utility_map(FAM, confident_only=True)
    assert 1 in umap and umap[1] < 0.5
    surf = UtilitySurfacer(enabled=True, epsilon_explore=0.0, f_min=0.5, f_max=1.5,
                           rng=random.Random(0))
    out = surf.order([Scored(1, 1.0, 0.9), Scored(2, 1.0, 0.9)], umap, family_scope=FAM)
    assert out[0].episode_id == 2 and out[1].episode_id == 1   # eid1 demoted


# 5 ───────────────────────────────────────────────────────────────────────────
def test_blame_overlap_clawback_only():
    # overlap ⇒ clawback
    p, ledger, util, source = _build()
    _expose(ledger)
    source._poll = SourcePoll(repo_remote="r",
                              commits=(_commit(introduced_lines=frozenset({10, 11, 12})),))
    p.step(now=1100)
    source._poll = SourcePoll(repo_remote="r",
                              commits=(CommitFact(sha="bf", ts=1200, kind="bugfix",
                                                  files_touched=("a.py",),
                                                  touched_blame=frozenset({11, 99}), repo_remote="r"),))
    p.step(now=1200)
    assert ledger.get_outcome("c1", "trace1").state == "clawed_back"

    # coincidental same-file edit, NO blame overlap ⇒ NO clawback (the false-positive guard)
    p2, ledger2, util2, source2 = _build()
    _expose(ledger2)
    source2._poll = SourcePoll(repo_remote="r",
                               commits=(_commit(introduced_lines=frozenset({10, 11, 12})),))
    p2.step(now=1100)
    source2._poll = SourcePoll(repo_remote="r",
                               commits=(CommitFact(sha="bf2", ts=1200, kind="bugfix",
                                                   files_touched=("a.py",),
                                                   touched_blame=frozenset({50, 51}), repo_remote="r"),))
    p2.step(now=1200)
    assert ledger2.get_outcome("c1", "trace1").state == "provisional"   # untouched


# 6 ───────────────────────────────────────────────────────────────────────────
def test_drain_no_double_credit_on_retick():
    p, ledger, util, source = _build()
    _expose(ledger)
    source._poll = SourcePoll(repo_remote="r", commits=(_commit(),))
    p.step(now=1100)
    source._poll = SourcePoll(repo_remote="r", commits=())
    settle_now = 1100 + SETTLE_OFFSET + 1
    p.step(now=settle_now)
    p.step(now=settle_now)   # re-tick at the same time
    assert abs(util.posterior(1, FAM).wins - 0.12) < 1e-9   # moved exactly once


# 7 ───────────────────────────────────────────────────────────────────────────
def test_step_never_raises_and_returns_typed_tick():
    p, ledger, util, source = _build(raises=True)
    tick = p.step(now=1100)   # a bad repo must not raise
    assert tick.errors >= 1


# one-tx atomicity [A7] ─────────────────────────────────────────────────────────
class _FailingUtilityStore(SqliteUtilityStore):
    def apply_credit(self, deltas):
        raise RuntimeError("injected posterior-write failure")


def test_producer_tick_is_one_transaction_one_db():
    conn = connect(":memory:")
    ledger = SqliteEpisodeStore(conn)
    util = _FailingUtilityStore(conn)   # shares the one connection
    _expose(ledger)
    joiner = OutcomeJoiner(assoc_window_s=1800, settle_days=7, provisional_reward=0.2,
                           clawback_reward=-1.0, require_stamp=False, assoc_epsilon=0.0,
                           rng=random.Random(0))
    source = FakeOutcomeSource(SourcePoll(repo_remote="r", commits=(_commit(),)))
    p = OutcomeProducer(source=source, joiner=joiner, attributor=Attributor(),
                        store=ledger, utility_store=util, clock=FakeClock())
    p.step(now=1100)
    source._poll = SourcePoll(repo_remote="r", commits=())
    tick = p.step(now=1100 + SETTLE_OFFSET + 1)   # settle → drain raises → whole tick rolls back
    assert tick.errors >= 1
    # the settlement rolled back WITH the failed posterior write (no partial credit):
    assert ledger.get_outcome("c1", "trace1").state == "provisional"
    # no separate telemetry DB exists — everything is in the one connection [A7]:
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "exposure" in tables and "task_outcomes" in tables and "utility" in tables
