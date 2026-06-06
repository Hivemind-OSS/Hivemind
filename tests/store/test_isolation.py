"""P1.7 — Isolation-slice writer (guardrail-2, A5) at approve().

approve() is the SINGLE deterministic writer of held-out membership: a hash-stable
~isolation_frac slice of episodes is stamped utility.isolation=1 IN THE SAME TX as the
approval flip, so the live loop's reweighting can never touch the held-out slice and the
Attributor's exclusion has a real source. _is_isolation is pure/idempotent (no RNG), so
membership is identical across calls and restarts.
"""
from __future__ import annotations

import numpy as np

from hive.adapters.index_exhaustive import ExhaustiveCosineIndex
from hive.adapters.sqlite_db import connect
from hive.adapters.store_sqlite import SqliteEpisodeStore, _is_isolation, _ISOLATION_FAMILY
from hive.adapters.utility_store_sqlite import SqliteUtilityStore

DIM = 4
FRAC = 0.05


def _unit(*xs):
    v = np.array(xs, dtype=np.float32)
    return v / np.linalg.norm(v)


def _wired(isolation_frac=FRAC):
    """Episode store + utility store on ONE conn (single-DB): approve() stamps the
    co-located utility.isolation column the utility store owns. The utility store is
    constructed FIRST so its table exists when the isolation-enabled ledger's fail-fast
    guard runs (guardrail-2 dependency made explicit)."""
    conn = connect(":memory:")
    util = SqliteUtilityStore(conn)
    ledger = SqliteEpisodeStore(conn, index=ExhaustiveCosineIndex(DIM),
                                isolation_frac=isolation_frac)
    return ledger, util


def _stage_n(ledger, n):
    """Stage n episodes (NO approve); return their sequential autoincrement ids (1..n
    in a fresh store)."""
    return [ledger.stage(text=f"insight {i}", weight=1.0, source="m", tags="", proposed_by="a")[0]
            for i in range(n)]


def _approve_n(ledger, n):
    """Stage + approve n episodes; return their (autoincrement) ids."""
    ids = _stage_n(ledger, n)
    for eid in ids:
        assert ledger.approve(eid, "human", _unit(1, 0, 0, 0), expected_version=0) is True
    return ids


def _first_held_out(frac=FRAC, limit=2000):
    """The smallest episode id the predicate holds out — deterministic, so staging that
    many episodes yields a known held-out id at the end. Returns None if NOTHING is held
    out within the bound (e.g. under the always-False mutation) so callers fail FAST
    instead of looping forever."""
    return next((k for k in range(1, limit) if _is_isolation(k, frac)), None)


# ── ★ the pinned guardrail-2 test (A5) ───────────────────────────────────────────
def test_isolation_membership_assigned_at_fraction():
    """(a) the hash predicate holds out ≈5% over 10,000 ids; (b) it is idempotent (no
    RNG); (c) approve() STAMPS exactly that held-out set in the utility table."""
    # (a) fraction ≈ 0.05 ± 0.01 over 10,000 ids — deterministic, pure, instant.
    held = [e for e in range(1, 10_001) if _is_isolation(e, FRAC)]
    assert 0.04 <= len(held) / 10_000 <= 0.06

    # (b) idempotent: same boolean every call (no RNG, no state).
    assert all(_is_isolation(e, FRAC) is _is_isolation(e, FRAC) for e in range(1, 501))

    # (c) approve() stamps the held-out slice — exactly, and non-empty (so the stamp is
    #     load-bearing, not a vacuous {}=={}).
    ledger, util = _wired()
    ids = _approve_n(ledger, 400)
    expected = {i for i in ids if _is_isolation(i, FRAC)}
    assert expected                                   # ≈20 of 400 — guaranteed non-empty
    assert util.isolation_episode_ids() == expected   # approve stamped exactly the held-out


def test_isolation_membership_stable_across_restart():
    """Membership is a pure function of the id: re-deriving (or re-opening the store on
    the same DB) yields the SAME set — never re-rolled."""
    # pure determinism across repeated derivations
    first = {e for e in range(1, 2001) if _is_isolation(e, FRAC)}
    second = {e for e in range(1, 2001) if _is_isolation(e, FRAC)}
    assert first == second

    # the STAMPED membership persists: a fresh utility-store handle on the same conn
    # (simulating a process restart) reads back the identical held-out set.
    ledger, util = _wired()
    ids = _approve_n(ledger, 400)
    stamped = util.isolation_episode_ids()
    util_reopened = SqliteUtilityStore(util.conn)      # "restart" — same DB, new object
    assert util_reopened.isolation_episode_ids() == stamped == {i for i in ids if _is_isolation(i, FRAC)}


# ── belts ─────────────────────────────────────────────────────────────────────
def test_isolation_frac_positive_requires_utility_table():
    """FAIL FAST: enabling the held-out slice (isolation_frac>0) without a utility table
    on the connection raises a clear ValueError at construction — never a silent stamp
    failure on the first held-out approval (audit wf_17978e86: half-wired coupling)."""
    import pytest
    conn = connect(":memory:")                         # no SqliteUtilityStore ⇒ no utility table
    with pytest.raises(ValueError, match="utility table"):
        SqliteEpisodeStore(conn, index=ExhaustiveCosineIndex(DIM), isolation_frac=0.05)
    # frac=0.0 (off) is fine with no utility table — the guardrail is opt-in.
    SqliteEpisodeStore(connect(":memory:"), index=ExhaustiveCosineIndex(DIM))   # no raise


def test_isolation_frac_zero_holds_out_nothing():
    """isolation_frac=0.0 (the default, Phase-1 conservative) ⟹ _is_isolation is always
    False and approve() stamps NOTHING (no held-out slice unless explicitly enabled)."""
    assert not any(_is_isolation(e, 0.0) for e in range(1, 1001))
    ledger, util = _wired(isolation_frac=0.0)
    _approve_n(ledger, 200)
    assert util.isolation_episode_ids() == set()


def test_non_isolated_episode_not_stamped():
    """An approved episode NOT in the held-out slice has no isolation row."""
    ledger, util = _wired()
    ids = _approve_n(ledger, 400)
    not_held = {i for i in ids if not _is_isolation(i, FRAC)}
    assert not_held & util.isolation_episode_ids() == set()   # none of the non-held are stamped


def test_isolation_not_stamped_when_approval_fails():
    """ATOMICITY: a stale-version approve does NOT flip the row, and therefore must NOT
    stamp isolation — no orphan held-out membership for an un-approved episode."""
    target = _first_held_out()
    assert target is not None                          # fast-fail under a holds-out-nothing mutation
    ledger, util = _wired()
    eid = _stage_n(ledger, target)[-1]                 # ids 1..target; the last is held out
    assert _is_isolation(eid, FRAC)                    # precondition: this id WOULD be stamped on a real flip
    assert ledger.approve(eid, "human", _unit(1, 0, 0, 0), expected_version=99) is False  # stale ⇒ no flip
    assert eid not in util.isolation_episode_ids()     # not stamped — approval never happened
    assert ledger.get_episode(eid).status == "pending"


def test_isolation_stamp_idempotent_on_reapprove():
    """Re-approving an already-approved held-out episode (idempotent no-op) leaves a
    single isolation membership, not a duplicate/cleared one."""
    target = _first_held_out()
    assert target is not None
    ledger, util = _wired()
    eid = _stage_n(ledger, target)[-1]
    assert _is_isolation(eid, FRAC)
    ledger.approve(eid, "human", _unit(1, 0, 0, 0), expected_version=0)
    ledger.approve(eid, "human2", _unit(0, 1, 0, 0), expected_version=0)   # idempotent no-op
    assert eid in util.isolation_episode_ids()
    assert util.posterior(eid, _ISOLATION_FAMILY).isolation is True
