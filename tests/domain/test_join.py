"""P0.1 — pure OutcomeJoiner (§11 association + settlement + clawback + net)."""
from __future__ import annotations

import random

from hive.domain.join import OutcomeJoiner
from hive.domain.models import CommitFact, OutcomeRow, RecallWindow, WindowTrace

_DAY = 86_400


def _joiner(**over) -> OutcomeJoiner:
    cfg = dict(assoc_window_s=1800, settle_days=7, provisional_reward=0.2,
               clawback_reward=-1.0, require_stamp=False, assoc_epsilon=0.1,
               rng=random.Random(0))
    cfg.update(over)
    return OutcomeJoiner(**cfg)


def _row(task_ref="sha1", trace_id="t1", state="provisional", settle_at=0,
         introduced=frozenset(), family="r|python|general") -> OutcomeRow:
    return OutcomeRow(task_ref=task_ref, trace_id=trace_id, family_scope=family,
                      state=state, reward=0.2, merge_ts=0, settle_at=settle_at,
                      introduced_lines=introduced)


# ── associate ────────────────────────────────────────────────────────────────

def test_window_primary_associates_in_window_traces():
    T = 10_000
    j = _joiner()
    commit = CommitFact(sha="abc", ts=T, repo_remote="r", files_touched=("a.py",))
    window = RecallWindow(items=(WindowTrace("t_in", T - 100), WindowTrace("t_out", T - 3600)))
    rows = j.associate([commit], window)
    assert len(rows) == 1
    r = rows[0]
    assert r.task_ref == "abc" and r.trace_id == "t_in" and r.state == "provisional"
    assert r.settle_at == T + 7 * _DAY


def test_out_of_window_traces_not_associated():
    T = 10_000
    j = _joiner()
    commit = CommitFact(sha="abc", ts=T)
    window = RecallWindow(items=(WindowTrace("t_out", T - 3600),))
    assert j.associate([commit], window) == []


def test_stamp_trailer_overrides_window():
    T = 10_000
    j = _joiner()
    commit = CommitFact(sha="abc", ts=T, trailer_traces=("T1", "T2"))
    window = RecallWindow(items=(WindowTrace("t_in", T - 100),))  # would-be window hit, ignored
    rows = j.associate([commit], window)
    assert {r.trace_id for r in rows} == {"T1", "T2"}


def test_require_stamp_drops_window_assoc():
    T = 10_000
    j = _joiner(require_stamp=True)
    commit = CommitFact(sha="abc", ts=T)  # unstamped
    window = RecallWindow(items=(WindowTrace("t_in", T - 100),))
    assert j.associate([commit], window) == []


# ── settle ───────────────────────────────────────────────────────────────────

def test_provisional_settles_after_settle_days():
    j = _joiner()
    ripe = _row(settle_at=100)
    emits = j.settle([ripe], now=200)
    assert len(emits) == 1 and emits[0].reward_sign == +1 and emits[0].magnitude == 0.2
    assert j.settle([_row(settle_at=300)], now=200) == []   # not ripe ⇒ no emit


def test_settle_is_idempotent():
    j = _joiner()
    assert j.settle([_row(state="settled_pos", settle_at=100)], now=200) == []


def test_clawed_back_row_never_settles():
    j = _joiner()
    assert j.settle([_row(state="clawed_back", settle_at=100)], now=200) == []


# ── clawback ─────────────────────────────────────────────────────────────────

def test_revert_fires_immediate_clawback():
    j = _joiner()
    row = _row(task_ref="good_sha")
    revert = CommitFact(sha="rev", ts=500, kind="revert", reverts="good_sha")
    emits = j.clawback([row], [revert], now=500)
    assert len(emits) == 1 and emits[0].reward_sign == -1 and emits[0].magnitude == 1.0


def test_same_file_no_blame_overlap_no_clawback():  # GUARD a — the false-positive direction
    j = _joiner()
    row = _row(introduced=frozenset({10, 11, 12}))
    bugfix = CommitFact(sha="bf", ts=600, kind="bugfix",
                        files_touched=("a.py",), touched_blame=frozenset())  # no overlap
    assert j.clawback([row], [bugfix], now=600) == []


def test_blame_overlap_fires_clawback():  # GUARD b
    j = _joiner()
    row = _row(introduced=frozenset({10, 11, 12}))
    bugfix = CommitFact(sha="bf", ts=600, kind="bugfix",
                        files_touched=("a.py",), touched_blame=frozenset({11, 99}))
    emits = j.clawback([row], [bugfix], now=600)
    assert len(emits) == 1 and emits[0].reward_sign == -1 and emits[0].magnitude == 1.0


# ── net (fixed hop order) ────────────────────────────────────────────────────

def test_hop_order_settle_then_clawback_nets():
    j = _joiner()
    row = _row(task_ref="s", trace_id="t1", settle_at=100)
    revert = CommitFact(sha="rev", ts=200, kind="revert", reverts="s")
    settle_emits = j.settle([row], now=200)
    clawback_emits = j.clawback([row], [revert], now=200)
    net = j.net_emits(settle_emits, clawback_emits)
    assert len(net) == 1 and net[0].reward_sign == -1   # the stale + is cancelled


# ── family derivation ────────────────────────────────────────────────────────

def test_family_scope_derived_at_link_time():
    j = _joiner()
    bug = CommitFact(sha="a", ts=1, kind="bugfix", repo_remote="github.com/acme/web",
                     files_touched=("svc/a.py", "svc/b.py"))
    assert OutcomeJoiner.derive_family(bug) == "github.com/acme/web|python|bugfix"

    dep = CommitFact(sha="b", ts=1, kind="commit", repo_remote="r",
                     files_touched=("pyproject.toml",), message="bump deps")
    assert OutcomeJoiner.derive_family(dep).endswith("|dep-upgrade")

    gen = CommitFact(sha="c", ts=1, kind="commit", repo_remote="r",
                     files_touched=("a.py",), message="add feature")
    assert OutcomeJoiner.derive_family(gen) == "r|python|general"
