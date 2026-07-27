"""The materialized drift cache (rebuildable, Law 5): ``drift_put`` (last-write-wins on
the (repo, tip_sha, base_tip, anchor) key — EVERY input to the verdict, DDL-ordered row
tuples), ``drift_get`` (an exact (repo, tip, base_tip) read per anchor; an
un-materialized anchor, or one asked at a baseline it was not judged from, is ABSENT —
the caller reads absence as ``unverifiable``, never false-fresh/false-stale),
``drift_prune`` (bounded growth, plus the BUG-065 ``keep_anchors`` false-fresh close and
its ``keep_base_tips`` twin), the ``ref_requests`` demand seam
(``touch_ref_request`` / ``requested_refs``), the ``ref_tips`` per-ref tip watermark
(``ref_tip`` / ``ref_tips_put`` / ``ref_tips_prune`` — the branch twin of the canonical
``sync:<repo>:last_tip`` meta key), and ``declared_refs`` — the materializer's
declared-coverage work list. Verdict strings ride verbatim — the cache never
interprets the wire vocabulary."""

from __future__ import annotations

import numpy as np

from hive.adapters.sqlite_db import connect
from hive.adapters.store_sqlite import SqliteEpisodeStore

TIP0, TIP1 = "a" * 40, "b" * 40
BASE, BASE2 = "9" * 40, "8" * 40


def _store() -> SqliteEpisodeStore:
    return SqliteEpisodeStore(connect(":memory:"))


def _declared(
    s: SqliteEpisodeStore,
    text: str,
    *,
    repo: str,
    ref: str,
    trust: str = "provisional",
) -> int:
    """Stage + complete one episode declaring ``ref`` for ``repo`` — the minimal
    fixture ``declared_refs`` reads."""
    eid, _ = s.stage(text=text, weight=1.0, proposed_by="w", ts=10, repos=[(repo, ref)])
    assert s.complete(
        eid,
        np.ones(4, dtype=np.float32),
        expected_version=0,
        trust=trust,
        last_active_ts=10,
    )
    return eid


def _row(
    repo="alpha",
    tip=TIP0,
    anchor="a.py::f",
    verdict="fresh",
    detail="{}",
    ts=100,
    base=BASE,
) -> tuple:
    return (repo, tip, base, anchor, verdict, detail, ts)


def _get(s, repo, tip, anchors, base=BASE):
    """``drift_get`` with a uniform baseline — the shape every caller but the
    two-baselines case uses."""
    return s.drift_get(repo, tip, dict.fromkeys(anchors, base), anchors)


# ── drift_put / drift_get ──────────────────────────────────────────────────────
def test_put_then_get_roundtrips_verdict_and_detail():
    s = _store()
    s.drift_put(
        [
            _row(verdict="anchor_missing", detail='{"reason":"gone"}'),
            _row(anchor="b.py::g", verdict="fresh"),
        ]
    )
    got = _get(s, "alpha", TIP0, ["a.py::f", "b.py::g"])
    assert got == {
        "a.py::f": ("anchor_missing", '{"reason":"gone"}'),
        "b.py::g": ("fresh", "{}"),
    }


def test_get_omits_unmaterialized_anchors():
    # absence IS the fail-safe signal: the caller reads a missing key as
    # unverifiable — the cache never fabricates a verdict for an unmeasured anchor.
    s = _store()
    s.drift_put([_row()])
    got = _get(s, "alpha", TIP0, ["a.py::f", "never.py::seen"])
    assert set(got) == {"a.py::f"}


def test_get_is_keyed_by_repo_tip_and_baseline_exactly():
    s = _store()
    s.drift_put([_row(tip=TIP0, verdict="fresh")])
    assert _get(s, "alpha", TIP1, ["a.py::f"]) == {}  # a moved tip reads empty
    assert _get(s, "beta", TIP0, ["a.py::f"]) == {}  # another repo reads empty
    assert _get(s, "alpha", TIP0, ["a.py::f"], base=BASE2) == {}, (
        "the verdict is a function of the BASELINE too — a row judged from another "
        "one must never answer this question (BUG-081, unconstructable)"
    )


def test_two_baselines_on_one_anchor_at_one_tip_are_separate_rows():
    s = _store()
    s.drift_put(
        [
            _row(base=BASE, verdict="anchor_changed"),
            _row(base=BASE2, verdict="fresh"),
        ]
    )
    assert _get(s, "alpha", TIP0, ["a.py::f"])["a.py::f"][0] == "anchor_changed"
    assert _get(s, "alpha", TIP0, ["a.py::f"], base=BASE2)["a.py::f"][0] == "fresh"
    n = s.conn.execute("SELECT COUNT(*) AS c FROM anchor_drift").fetchone()["c"]
    assert n == 2, "one row per (tip, baseline, anchor) — neither displaces the other"


def test_prune_drops_rows_whose_baseline_left_the_work_list():
    s = _store()
    s.drift_put([_row(base=BASE), _row(base=BASE2)])
    dropped = s.drift_prune(
        "alpha", keep_tips=[TIP0], keep_anchors=["a.py::f"], keep_base_tips=[BASE]
    )
    assert dropped == 1
    assert _get(s, "alpha", TIP0, ["a.py::f"]) != {}
    assert _get(s, "alpha", TIP0, ["a.py::f"], base=BASE2) == {}


def test_get_empty_anchor_list_reads_empty():
    s = _store()
    s.drift_put([_row()])
    assert _get(s, "alpha", TIP0, []) == {}


def test_put_same_key_is_last_write_wins():
    # a re-materialization refreshes the cache row (rebuildable state, not a ledger).
    s = _store()
    s.drift_put([_row(verdict="fresh", ts=100)])
    s.drift_put([_row(verdict="anchor_changed", detail='{"v":2}', ts=200)])
    assert _get(s, "alpha", TIP0, ["a.py::f"]) == {
        "a.py::f": ("anchor_changed", '{"v":2}')
    }
    n = s.conn.execute("SELECT COUNT(*) AS c FROM anchor_drift").fetchone()["c"]
    assert n == 1  # replaced, never doubled


def test_put_empty_batch_is_a_noop():
    s = _store()
    s.drift_put([])
    assert s.conn.execute("SELECT COUNT(*) AS c FROM anchor_drift").fetchone()["c"] == 0


def test_verdict_strings_ride_verbatim():
    # the store persists whatever verdict it is given — the verify→wire mapping
    # lives upstream; the cache never interprets (or gatekeeps) the vocabulary.
    s = _store()
    s.drift_put(
        [_row(verdict="branch_scoped"), _row(anchor="x", verdict="unverifiable")]
    )
    got = _get(s, "alpha", TIP0, ["a.py::f", "x"])
    assert got["a.py::f"][0] == "branch_scoped" and got["x"][0] == "unverifiable"


# ── drift_prune: bounded growth for a rebuildable cache ────────────────────────
def test_prune_keeps_listed_tips_only():
    s = _store()
    s.drift_put([_row(tip=TIP0), _row(tip=TIP1), _row(repo="beta", tip=TIP0)])
    dropped = s.drift_prune("alpha", keep_tips=[TIP1])
    assert dropped == 1
    assert _get(s, "alpha", TIP0, ["a.py::f"]) == {}
    assert _get(s, "alpha", TIP1, ["a.py::f"]) != {}
    assert _get(s, "beta", TIP0, ["a.py::f"]) != {}  # other repos untouched


def test_prune_empty_keep_wipes_the_repo():
    s = _store()
    s.drift_put([_row(tip=TIP0), _row(tip=TIP1)])
    assert s.drift_prune("alpha", keep_tips=[]) == 2
    assert _get(s, "alpha", TIP0, ["a.py::f"]) == {}


# ── drift_prune(keep_anchors=...): the BUG-065 false-fresh close ──────────────
def test_prune_keep_anchors_none_is_identical_to_omitting_it():
    # an EXPLICIT keep_anchors=None must reproduce today's tip-only prune
    # byte-for-byte — no behavior change for any existing caller.
    s = _store()
    s.drift_put([_row(tip=TIP0), _row(tip=TIP1), _row(repo="beta", tip=TIP0)])
    dropped = s.drift_prune("alpha", keep_tips=[TIP1], keep_anchors=None)
    assert dropped == 1
    assert _get(s, "alpha", TIP0, ["a.py::f"]) == {}
    assert _get(s, "alpha", TIP1, ["a.py::f"]) != {}
    assert _get(s, "beta", TIP0, ["a.py::f"]) != {}  # other repos untouched


def test_prune_with_keep_anchors_drops_rows_for_anchors_outside_the_set():
    # a row can be at a LIVE tip and still be dropped when its anchor left the
    # work list — the hole that lets a retired episode's stale cache answer a
    # brand-new episode binding the same anchor.
    s = _store()
    s.drift_put(
        [
            _row(tip=TIP0, anchor="a.py::f", verdict="fresh"),
            _row(tip=TIP0, anchor="b.py::g", verdict="fresh"),
            _row(tip=TIP1, anchor="a.py::f", verdict="anchor_changed"),
        ]
    )
    dropped = s.drift_prune("alpha", keep_tips=[TIP0, TIP1], keep_anchors=["a.py::f"])
    assert dropped == 1  # b.py::g at TIP0: its tip is live, its anchor is not
    assert _get(s, "alpha", TIP0, ["a.py::f", "b.py::g"]) == {
        "a.py::f": ("fresh", "{}")
    }
    assert _get(s, "alpha", TIP1, ["a.py::f"]) != {}


def test_prune_empty_keep_anchors_wipes_the_repo_all_retired():
    # an all-retired repo has no live anchors: an explicit EMPTY keep_anchors
    # means no anchor is live, so the whole cache drops even at a kept tip.
    s = _store()
    s.drift_put([_row(tip=TIP0), _row(repo="beta", tip=TIP0)])
    dropped = s.drift_prune("alpha", keep_tips=[TIP0], keep_anchors=[])
    assert dropped == 1
    assert _get(s, "alpha", TIP0, ["a.py::f"]) == {}
    assert _get(s, "beta", TIP0, ["a.py::f"]) != {}  # other repos untouched


def test_cache_is_rebuildable_wipe_then_repopulate():
    # the Law-5 posture at the store level: a wiped cache accepts the same rows again.
    s = _store()
    s.drift_put([_row()])
    s.conn.execute("DELETE FROM anchor_drift")
    s.drift_put([_row()])
    assert _get(s, "alpha", TIP0, ["a.py::f"]) == {"a.py::f": ("fresh", "{}")}


# ── ref_requests: the recall-touched materialization demand ────────────────────
def test_touch_then_requested_refs_roundtrip():
    s = _store()
    s.touch_ref_request("alpha", "feature", 100)
    s.touch_ref_request("alpha", "develop", 150)
    s.touch_ref_request("beta", "feature", 200)  # another repo's demand
    assert s.requested_refs("alpha", since_ts=0) == [
        "develop",
        "feature",
    ]  # ref-ordered
    assert s.requested_refs("beta", since_ts=0) == ["feature"]


def test_requested_refs_window_is_strict():
    s = _store()
    s.touch_ref_request("alpha", "feature", 100)
    assert s.requested_refs("alpha", since_ts=100) == []  # STRICT: ts > since
    assert s.requested_refs("alpha", since_ts=99) == ["feature"]


def test_touch_keeps_the_newest_request_stamp():
    s = _store()
    s.touch_ref_request("alpha", "feature", 100)
    s.touch_ref_request("alpha", "feature", 300)  # re-touch advances
    s.touch_ref_request("alpha", "feature", 200)  # out-of-order never rewinds
    row = s.conn.execute(
        "SELECT last_requested_ts FROM ref_requests "
        "WHERE repo='alpha' AND ref='feature'"
    ).fetchone()
    assert row["last_requested_ts"] == 300
    n = s.conn.execute("SELECT COUNT(*) AS c FROM ref_requests").fetchone()["c"]
    assert n == 1  # upsert, never a second row


# ── ref_tips: the materializer's per-ref tip watermark ─────────────────────────
def test_ref_tip_absent_reads_none():
    s = _store()
    assert s.ref_tip("alpha", "feature") is None  # never materialized ⇒ unknown tip


def test_ref_tips_put_then_ref_tip_roundtrips():
    s = _store()
    s.ref_tips_put([("alpha", "feature", TIP0, 100)])
    assert s.ref_tip("alpha", "feature") == TIP0
    assert s.ref_tip("alpha", "develop") is None  # another ref untouched
    assert s.ref_tip("beta", "feature") is None  # another repo untouched


def test_ref_tips_put_is_last_write_wins():
    s = _store()
    s.ref_tips_put([("alpha", "feature", TIP0, 100)])
    s.ref_tips_put([("alpha", "feature", TIP1, 200)])
    assert s.ref_tip("alpha", "feature") == TIP1
    n = s.conn.execute("SELECT COUNT(*) AS c FROM ref_tips").fetchone()["c"]
    assert n == 1  # replaced, never doubled


def test_ref_tips_put_empty_batch_is_a_noop():
    s = _store()
    s.ref_tips_put([])
    assert s.conn.execute("SELECT COUNT(*) AS c FROM ref_tips").fetchone()["c"] == 0


def test_ref_tips_prune_keeps_listed_refs_only():
    s = _store()
    s.ref_tips_put(
        [
            ("alpha", "feature", TIP0, 100),
            ("alpha", "develop", TIP1, 100),
            ("beta", "feature", TIP0, 100),
        ]
    )
    dropped = s.ref_tips_prune("alpha", keep_refs=["develop"])
    assert dropped == 1
    assert s.ref_tip("alpha", "feature") is None
    assert s.ref_tip("alpha", "develop") == TIP1
    assert s.ref_tip("beta", "feature") == TIP0  # other repos untouched


def test_ref_tips_prune_empty_keep_wipes_the_repo():
    s = _store()
    s.ref_tips_put([("alpha", "feature", TIP0, 100), ("alpha", "develop", TIP1, 100)])
    assert s.ref_tips_prune("alpha", keep_refs=[]) == 2
    assert s.ref_tip("alpha", "feature") is None
    assert s.ref_tip("alpha", "develop") is None


# ── declared_refs: the materializer's declared-coverage work list ──────────────
def test_declared_refs_lists_distinct_refs_from_live_episodes():
    s = _store()
    _declared(s, "on feature", repo="alpha", ref="feature")
    _declared(s, "on develop", repo="alpha", ref="develop")
    _declared(s, "another on feature", repo="alpha", ref="feature")  # dup ref
    _declared(s, "elsewhere", repo="beta", ref="feature")  # another repo
    assert s.declared_refs("alpha") == ["develop", "feature"]  # distinct, ref-ordered


def test_declared_refs_excludes_scope_only_and_pending_episodes():
    s = _store()
    eid, _ = s.stage(
        text="scope only, no line",
        weight=1.0,
        proposed_by="w",
        ts=10,
        repos=[("alpha", "")],
    )
    assert s.complete(
        eid,
        np.ones(4, dtype=np.float32),
        expected_version=0,
        trust="provisional",
        last_active_ts=10,
    )
    s.stage(
        text="pending, never completed",
        weight=1.0,
        proposed_by="w",
        ts=10,
        repos=[("alpha", "feature")],
    )
    assert s.declared_refs("alpha") == []


def test_declared_refs_excludes_deprecated_episodes():
    s = _store()
    eid = _declared(s, "later retired", repo="alpha", ref="feature")
    assert s.declared_refs("alpha") == ["feature"]
    s.conn.execute("UPDATE episodes SET trust='deprecated' WHERE id=?", (eid,))
    assert s.declared_refs("alpha") == []  # a retired line stops demanding coverage


def test_declared_refs_empty_repo_reads_empty():
    s = _store()
    assert s.declared_refs("alpha") == []
