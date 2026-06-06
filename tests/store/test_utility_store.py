"""P0.4 — SqliteUtilityStore (real Beta-Bernoulli persistence) [A3][A5][A7]."""
from __future__ import annotations

import pytest

from hive.adapters.sqlite_db import connect, tx
from hive.adapters.utility_store_sqlite import SqliteUtilityStore
from hive.domain.attribution import CreditDelta

FAM = "r|python|bugfix"


@pytest.fixture()
def store():
    return SqliteUtilityStore(connect(":memory:"))


def _d(eid, dw=0.0, dl=0.0, agent="A"):
    return CreditDelta(episode_id=eid, family_scope=FAM, d_wins=dw, d_losses=dl, source_agent=agent)


def test_apply_credit_accumulates(store):
    store.apply_credit([_d(1, dw=0.12)])
    store.apply_credit([_d(1, dw=0.08)])
    assert abs(store.posterior(1, FAM).wins - 0.20) < 1e-9


def test_apply_credit_distinct_n_sources(store):
    store.apply_credit([_d(1, dw=0.1, agent="A")])
    store.apply_credit([_d(1, dw=0.1, agent="B")])
    assert store.posterior(1, FAM).n_sources == 2
    store.apply_credit([_d(1, dw=0.1, agent="A")])      # repeat agent
    assert store.posterior(1, FAM).n_sources == 2       # DISTINCT


def test_update_bumps_version_never_weight(store):
    store.apply_credit([_d(1, dw=0.1)])
    v1 = store.posterior(1, FAM).version
    store.apply_credit([_d(1, dw=0.1)])
    assert store.posterior(1, FAM).version > v1
    assert not any(hasattr(store, m) for m in ("bump_weight", "set_weight", "update_weight"))
    tables = {r[0] for r in store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "episodes" not in tables       # no episodes handle reachable from this adapter


def test_utility_map_confident_only(store):
    store.apply_credit([_d(1, dw=20.0, dl=2.0)])   # confident-positive
    store.apply_credit([_d(2, dw=1.0)])            # sparse
    m = store.utility_map(FAM, confident_only=True)
    assert 1 in m and 2 not in m


def test_zero_utility_layer_rolls_back(store):
    store.apply_credit([_d(1, dw=5.0, dl=1.0)])
    v0 = store.layer_version()
    store.zero_utility_layer()
    assert store.posterior(1, FAM).wins == 0 and store.posterior(1, FAM).losses == 0
    assert store.layer_version() == v0 + 1


def test_isolation_episode_ids(store):
    store.mark_isolation(7, FAM)
    store.apply_credit([_d(1, dw=0.1)])
    assert store.isolation_episode_ids() == {7}


def test_apply_credit_transactional(store):
    """A failure inside the tick's tx rolls the batch back (atomic credit)."""
    with pytest.raises(RuntimeError):
        with tx(store.conn):
            store.apply_credit([_d(1, dw=0.12)])
            raise RuntimeError("mid-batch boom")
    assert store.posterior(1, FAM).wins == 0   # rolled back


# ── settled_exposures_since: the guardrail-3 settled stream (A6) ──────────────────
# These wire BOTH ledger + utility on ONE connection (the single-DB design): the real
# SqliteUtilityStore reads task_outcomes ⋈ exposure that the ledger store owns. This
# closes the port-vs-impl gap the M08 audit flagged — SqliteUtilityStore must be a
# COMPLETE UtilityStore, not just FakeUtilityStore.
from hive.adapters.store_sqlite import SqliteEpisodeStore          # noqa: E402
from hive.domain.models import OutcomeRow, SettledExposure          # noqa: E402
from hive.domain.ports import UtilityStore                          # noqa: E402


def _wired():
    """Ledger + utility store sharing one autocommit connection (production wiring)."""
    conn = connect(":memory:")
    return SqliteEpisodeStore(conn), SqliteUtilityStore(conn)


def _settle(ledger, *, task_ref, trace_id, family, state, settle_at, exposed):
    """Stage an exposure for the trace and a settled task_outcome for it."""
    ledger.record_exposure(trace_id, [(eid, 0.5) for eid in exposed], ts=settle_at - 1)
    ledger.link_task(OutcomeRow(
        task_ref=task_ref, trace_id=trace_id, family_scope=family,
        state=state, reward=(1.0 if state == "settled_pos" else -1.0),
        merge_ts=settle_at - 1, settle_at=settle_at))


def test_sqlite_utility_store_conforms_to_port():
    """The REAL adapter satisfies the (widened) UtilityStore Protocol — the audit's
    gap was that only FakeUtilityStore was conformance-checked."""
    assert isinstance(SqliteUtilityStore(connect(":memory:")), UtilityStore)


def test_settled_exposures_round_trip():
    ledger, util = _wired()
    _settle(ledger, task_ref="k1", trace_id="t1", family=FAM,
            state="clawed_back", settle_at=1000, exposed=(1, 2))
    rows = util.settled_exposures_since(FAM, 0)
    assert len(rows) == 1
    r = rows[0]
    assert isinstance(r, SettledExposure)
    assert r.family_scope == FAM and r.reward_sign == -1
    assert r.exposed == (1, 2) and r.settle_ts == 1000


def test_settled_exposures_maps_state_to_sign():
    ledger, util = _wired()
    _settle(ledger, task_ref="k1", trace_id="t1", family=FAM,
            state="settled_pos", settle_at=1000, exposed=(1,))     # → +1
    _settle(ledger, task_ref="k2", trace_id="t2", family=FAM,
            state="clawed_back", settle_at=1001, exposed=(2,))     # → −1
    by_eid = {r.exposed[0]: r.reward_sign for r in util.settled_exposures_since(FAM, 0)}
    assert by_eid == {1: 1, 2: -1}


def test_settled_exposures_window_floor_is_inclusive():
    ledger, util = _wired()
    _settle(ledger, task_ref="kIn", trace_id="tIn", family=FAM,
            state="clawed_back", settle_at=1000, exposed=(1,))     # == floor: included
    _settle(ledger, task_ref="kOut", trace_id="tOut", family=FAM,
            state="clawed_back", settle_at=999, exposed=(2,))      # < floor: excluded
    refs = {r.exposed[0] for r in util.settled_exposures_since(FAM, 1000)}
    assert refs == {1}                                             # boundary inclusive, same as the Fake


def test_settled_exposures_filters_by_family():
    ledger, util = _wired()
    _settle(ledger, task_ref="k1", trace_id="t1", family=FAM,
            state="clawed_back", settle_at=1000, exposed=(1,))
    _settle(ledger, task_ref="k2", trace_id="t2", family="r|rust|general",
            state="clawed_back", settle_at=1000, exposed=(2,))     # other family
    rows = util.settled_exposures_since(FAM, 0)
    assert {r.exposed[0] for r in rows} == {1}                     # no cross-family bleed


def test_settled_exposures_excludes_provisional():
    ledger, util = _wired()
    _settle(ledger, task_ref="k1", trace_id="t1", family=FAM,
            state="clawed_back", settle_at=1000, exposed=(1,))
    # a provisional (un-settled) outcome must NOT appear — reality hasn't landed yet.
    ledger.record_exposure("t2", [(2, 0.5)], ts=999)
    ledger.link_task(OutcomeRow(task_ref="k2", trace_id="t2", family_scope=FAM,
                                state="provisional", reward=0.2, merge_ts=999, settle_at=1000))
    rows = util.settled_exposures_since(FAM, 0)
    assert {r.exposed[0] for r in rows} == {1}


def test_monitor_over_real_adapter_flags_stale_ranker():
    """End-to-end on the REAL adapter: the ★ stale-ranker scenario gives divergence ≈0.9
    — parity with the Fake-based pinned test, proving the seam is wired, not phantom."""
    from hive.domain.attribution import PredictionBiasMonitor
    from tests.fakes import FakeClock
    ledger, util = _wired()
    util.apply_credit([CreditDelta(1, FAM, d_wins=8.0, d_losses=0.0)])   # Beta(9,1) ⇒ mean 0.9
    for i in range(10):                                                  # 10 in-window clawbacks
        _settle(ledger, task_ref=f"k{i}", trace_id=f"t{i}", family=FAM,
                state="clawed_back", settle_at=1_000_000 - i, exposed=(1,))
    mon = PredictionBiasMonitor(util, clock=FakeClock(1_000_000))
    assert abs(mon.divergence(FAM, 7 * 24 * 3600) - 0.9) < 1e-6
