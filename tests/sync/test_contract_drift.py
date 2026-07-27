"""The drift-materializer contract — the git prober's unit twin.

A tick materializes ``anchor_drift`` rows per (repo, tip, base_tip, anchor) by asking
GIT: per DISTINCT baseline, which paths existed at it and what ``base..tip`` did to
each; then, only for a symbol anchor whose file actually moved, whether that symbol's
own lines changed. A symbol binding is additionally resolved ONCE against its own
baseline, so a name that never existed cannot inherit ``fresh`` from a quiet file.
Tips = the canonical tip plus branch tips recall demanded via ``ref_requests`` within
the 7-day demand window. A probe fault materializes NOTHING — absence reads
``unverifiable``, the honest unknown, and the next tick retries. The cache is a
rebuildable Law-5 cache (wipe → repopulated; old tips pruned) and a retired memory's
anchor leaves the work list with its baseline.

The prober is also the ONE reader of the historical single-colon spelling (BUG-083):
the write gate refuses it and the census join cannot match it, but the stored
population still earns a computed verdict here.
"""

from __future__ import annotations

from tests.sync.conftest import (
    ANCHOR,
    REPO,
    RecordingRun,
    completed,
    drift_rows,
    git,
    make_service,
    meta,
    register_repo,
    seed_episode,
)

STALE = {"anchor_changed", "anchor_missing", "blast_radius_changed"}


def _armed(origin, store, tmp_path, **kw):
    register_repo(store, REPO, origin.url)
    return make_service(store, tmp_path, **kw)


def _verdict_for(store, tip: str, anchor: str = ANCHOR):
    for r in drift_rows(store, REPO, tip):
        if r["anchor"] == anchor:
            return r["verdict"]
    return None


def _anchors(svc, repo: str = REPO) -> set[str]:
    return {anchor for _eid, anchor, _base in svc._store.anchor_work_list(repo)}


def test_fresh_then_stale_at_the_moved_tip_and_prune(origin, store, tmp_path):
    """An intact anchor materializes ``fresh`` at the measured tip; a removed
    symbol materializes ``anchor_missing`` at the NEW tip; the old tip's rows are
    pruned (the cache key is the tip — a moved tip invalidates naturally)."""
    seed_episode(store, "greet lesson")
    svc = _armed(origin, store, tmp_path)
    svc.tick()  # mint the fp + materialize at tip0
    tip0 = origin.origin_sha("refs/heads/main")
    assert _verdict_for(store, tip0) == "fresh"
    assert meta(store, f"sync:{REPO}:last_tip") == tip0

    origin.commit(
        "app.py", "def farewell(name):\n    return 'bye ' + name\n", "rm greet"
    )
    origin.push()
    svc.tick()
    tip1 = origin.origin_sha("refs/heads/main")
    assert _verdict_for(store, tip1) == "anchor_missing"
    assert drift_rows(store, REPO, tip0) == [], (
        "drift_prune drops tips no longer live — the cache is bounded"
    )


def test_signature_change_materializes_anchor_changed(origin, store, tmp_path):
    seed_episode(store, "greet lesson")
    svc = _armed(origin, store, tmp_path)
    svc.tick()  # fp minted at tip0
    origin.commit(
        "app.py",
        'def greet(name, punct):\n    return "hi " + name + punct\n',
        "widen greet",
    )
    origin.push()
    svc.tick()
    tip1 = origin.origin_sha("refs/heads/main")
    assert _verdict_for(store, tip1) == "anchor_changed"


def test_a_body_rewrite_the_fingerprint_missed_is_detected(origin, store, tmp_path):
    """The measured failure that motivated the whole ladder: a completely rewritten
    body with an UNCHANGED signature and unchanged callees served ``fresh`` under the
    call-shape fingerprint. git sees the symbol's own lines move."""
    seed_episode(store, "greet lesson")
    svc = _armed(origin, store, tmp_path)
    svc.tick()
    assert _verdict_for(store, origin.origin_sha("refs/heads/main")) == "fresh"

    origin.commit(
        "app.py",
        'def greet(name):\n    prefix = "hi "\n    return prefix + name.strip()\n',
        "rewrite the body only",
    )
    origin.push()
    svc.tick()
    assert _verdict_for(store, origin.origin_sha("refs/heads/main")) == "anchor_changed"


def test_requested_ref_materializes_at_its_tip(origin, store, tmp_path):
    """Recall demand (``ref_requests``) drives non-canonical coverage: a touched
    branch is materialized at ITS tip on the next tick — the CT-6 shape, with the
    demand stamp far behind the service wall clock (the window anchors on the
    demand clock, so a lagging stamp never starves coverage)."""
    seed_episode(store, "greet lesson")
    svc = _armed(origin, store, tmp_path)
    svc.tick()  # fp minted at the canonical tip
    git(origin.work, "checkout", "-q", "-b", "feature")
    origin.commit("app.py", "def other():\n    return 1\n", "break on feature")
    origin.push("feature")
    git(origin.work, "checkout", "-q", "main")
    feature_tip = origin.origin_sha("refs/heads/feature")

    store.touch_ref_request(REPO, "feature", 424_242)
    svc.tick()

    v = _verdict_for(store, feature_tip)
    assert v is not None, "a requested ref must be materialized at ITS tip"
    assert v in STALE, f"the branch broke the anchor: {v!r}"


def test_demand_window_excludes_refs_older_than_7d(origin, store, tmp_path):
    """The exclusion side of the 7d window: a ref whose last demand is > 7d older
    than the newest demand (and the service clock) ages off the work list."""
    seed_episode(store, "greet lesson")
    for name, content in (
        ("old-branch", "def other():\n    return 1\n"),
        ("new-branch", "def other():\n    return 2\n"),
    ):
        git(origin.work, "checkout", "-q", "-b", name)
        origin.commit("app.py", content, f"break on {name}")
        origin.push(name)
        git(origin.work, "checkout", "-q", "main")
    old_tip = origin.origin_sha("refs/heads/old-branch")
    new_tip = origin.origin_sha("refs/heads/new-branch")

    store.touch_ref_request(REPO, "old-branch", 1_000_000)  # 10.4d before newest
    store.touch_ref_request(REPO, "new-branch", 1_900_000)
    svc = _armed(origin, store, tmp_path, now=lambda: 2_000_000)
    svc.tick()

    assert _verdict_for(store, new_tip) is not None, "in-window demand materializes"
    assert _verdict_for(store, old_tip) is None, (
        "demand older than the 7d window must NOT be materialized"
    )


def test_unknown_requested_ref_skips_silently(origin, store, tmp_path):
    """A demanded ref with no remote tip (deleted/never-pushed branch) is skipped
    silently — the canonical tip still materializes, no error is noted."""
    seed_episode(store, "greet lesson")
    store.touch_ref_request(REPO, "no-such-branch", 424_242)
    svc = _armed(origin, store, tmp_path)
    svc.tick()
    tip = origin.origin_sha("refs/heads/main")
    assert _verdict_for(store, tip) == "fresh"
    assert meta(store, f"sync:{REPO}:last_error") is None


def test_cache_is_rebuildable(origin, store, tmp_path):
    seed_episode(store, "greet lesson")
    svc = _armed(origin, store, tmp_path)
    svc.tick()
    tip = origin.origin_sha("refs/heads/main")
    assert drift_rows(store, REPO, tip), "precondition: materialized rows"

    store.conn.execute("DELETE FROM anchor_drift")
    assert not drift_rows(store, REPO, tip)
    svc.tick()
    assert drift_rows(store, REPO, tip), (
        "the drift cache is a rebuildable cache (Law 5): wipe → repopulated"
    )


# ── BUG-065: a retired memory's anchor leaves the verify work list ────────────
def test_the_work_list_excludes_a_deprecated_anchors_binding(origin, store, tmp_path):
    """The ONE work list carries the not-retired predicate — retirement flips TRUST,
    not status, so a status-only read would keep offering a pruned/superseded anchor
    to the materializer forever."""
    eid = seed_episode(store, "greet lesson")
    svc = _armed(origin, store, tmp_path)
    assert ANCHOR in _anchors(svc)

    assert store.deprecate(eid, actor="agent-a", ts=20)
    assert ANCHOR not in _anchors(svc)


def test_deprecated_anchor_is_pruned_from_the_drift_cache_at_a_live_tip(
    origin, store, tmp_path
):
    """The false-fresh close a tip-only prune would miss: a tick that leaves
    the canonical tip UNCHANGED must still drop a just-retired anchor's cached
    row there, or a later episode binding the same anchor would read a
    verdict computed against the retired episode's fingerprint."""
    eid = seed_episode(store, "greet lesson")
    svc = _armed(origin, store, tmp_path)
    svc.tick()
    tip = origin.origin_sha("refs/heads/main")
    assert _verdict_for(store, tip) == "fresh"

    assert store.deprecate(eid, actor="agent-a", ts=20)
    svc.tick()  # no commit in between: the canonical tip does not move
    assert _verdict_for(store, tip) is None, (
        "a retired anchor's cached row must not survive at a still-live tip"
    )


def test_a_probe_fault_materializes_nothing_and_the_next_tick_recovers(
    origin, store, tmp_path
):
    """A git fault degrades that baseline group to the honest unknown by writing NO
    row at all. Caching the degradation would be worse than useless: presence is the
    skip predicate, so a cached ``unverifiable`` would freeze at that tip until the
    tip moved again — a fault would outlive itself."""
    seed_episode(store, "greet lesson")
    svc = _armed(origin, store, tmp_path)
    svc.tick()
    origin.commit("app.py", "def greet(name):\n    return name\n", "move greet")
    origin.push()

    def is_ls_tree(argv):
        return "ls-tree" in argv

    broken = _armed_again(origin, store, tmp_path, is_ls_tree)
    broken.tick()
    tip = origin.origin_sha("refs/heads/main")
    assert _verdict_for(store, tip) is None, "a faulted probe writes no verdict"
    error = meta(store, f"sync:{REPO}:last_error")
    assert error is not None and "drift" in error, error

    make_service(store, tmp_path).tick()
    assert _verdict_for(store, tip) == "anchor_changed", (
        "the next clean tick must produce what the faulted one withheld"
    )


def _armed_again(origin, store, tmp_path, predicate):
    """A second service over the ALREADY-registered repo with a scripted spawn."""
    return make_service(
        store,
        tmp_path,
        run=RecordingRun(script=[(predicate, completed(rc=1, stderr="unreadable"))]),
    )


def test_an_unbaselined_anchor_materializes_nothing_but_is_baselined(
    origin, store, tmp_path
):
    """An anchor whose repo had no watermark when it was written has nothing to
    measure from. The tick fills the baseline (insert-if-absent) and writes no verdict
    for it that pass; the served read is ``unverifiable`` by absence."""
    eid = seed_episode(store, "greet lesson")
    svc = _armed(origin, store, tmp_path)
    assert _anchors(svc) == {ANCHOR}
    svc.tick()
    tip = origin.origin_sha("refs/heads/main")
    baselines = {
        (int(r["episode_id"]), r["anchor"]): r["base_tip"]
        for r in store.conn.execute(
            "SELECT * FROM anchor_baselines WHERE repo=?", (REPO,)
        )
    }
    assert baselines == {(eid, ANCHOR): tip}
    assert _verdict_for(store, tip) == "fresh"


def test_a_symbol_that_never_existed_is_unverifiable_on_a_quiet_file(
    origin, store, tmp_path
):
    """``fresh`` is a positive claim, so a quiet file may not confer it on a symbol
    nobody ever saw. The resolution is measured ONCE at the immutable baseline and
    stored beside it, which is what lets a quiet tick answer without asking again."""
    seed_episode(store, "typo lesson", anchor="app.py::gre3t")
    svc = _armed(origin, store, tmp_path)
    svc.tick()
    tip = origin.origin_sha("refs/heads/main")
    assert _verdict_for(store, tip, "app.py::gre3t") == "unverifiable"
    assert _verdict_for(store, tip, ANCHOR) is None, "precondition: only the typo"

    probes = store.anchor_symbol_probes(REPO)
    assert probes == {(tip, "app.py::gre3t"): 0}, (
        "the resolution is recorded against the baseline it was measured at"
    )

    # and it stays the unknown once the file DOES move — never a false missing
    origin.commit("app.py", "def greet(name):\n    return name\n", "move greet")
    origin.push()
    svc.tick()
    assert (
        _verdict_for(store, origin.origin_sha("refs/heads/main"), "app.py::gre3t")
        == "unverifiable"
    ), "an anchor git could never resolve must not qualify a retirement"


def test_a_resolved_symbol_is_measured_once_not_every_tick(origin, store, tmp_path):
    """The cost side of the same fact: the baseline is an immutable commit, so the
    resolution is asked once per binding and a quiet tick asks nothing at all."""
    seed_episode(store, "greet lesson")
    svc = _armed(origin, store, tmp_path)
    svc.tick()
    tip = origin.origin_sha("refs/heads/main")
    assert store.anchor_symbol_probes(REPO) == {(tip, ANCHOR): 1}

    quiet = RecordingRun()
    make_service(store, tmp_path, run=quiet).tick()
    assert not [a for a in quiet.calls if "blame" in a], (
        f"a repo with no NEW binding re-asks nothing: {quiet.calls}"
    )
    assert _verdict_for(store, tip) == "fresh"


# ── BUG-083: the historical single-colon spelling keeps a real verdict ────────
def test_a_historical_single_colon_anchor_still_reaches_a_verdict(
    origin, store, tmp_path
):
    """The write gate refuses ``path:Symbol`` and the census join cannot match it, but
    the stored population predates both. The prober resolves it against the tree so it
    keeps a computed verdict — and therefore stays inside machine-gated retirement
    coverage — instead of degrading to ``unverifiable`` forever."""
    seed_episode(store, "greet lesson", anchor="app.py:greet")
    svc = _armed(origin, store, tmp_path)
    svc.tick()
    tip0 = origin.origin_sha("refs/heads/main")
    assert _verdict_for(store, tip0, "app.py:greet") == "fresh"

    # its OWN lines move ⇒ the symbol tier fires, exactly as for `app.py::greet`
    origin.commit(
        "app.py", "def greet(name):\n    return name.strip()\n", "rewrite greet"
    )
    origin.push()
    svc.tick()
    assert _verdict_for(
        store, origin.origin_sha("refs/heads/main"), "app.py:greet"
    ) == ("anchor_changed")

    # and deletion still reaches the tier that qualifies retirement
    origin.commit("app.py", "def farewell(name):\n    return name\n", "rm greet")
    origin.push()
    svc.tick()
    assert _verdict_for(
        store, origin.origin_sha("refs/heads/main"), "app.py:greet"
    ) == ("anchor_missing")


def test_the_single_colon_leniency_does_not_reach_a_line_number(
    origin, store, tmp_path
):
    """The read side revives one shape, not every colon: a bare line number is a span,
    which no funcname resolver can match, so it stays the path it reads as — and the
    honest unknown."""
    seed_episode(store, "line lesson", anchor="app.py:12")
    svc = _armed(origin, store, tmp_path)
    svc.tick()
    tip = origin.origin_sha("refs/heads/main")
    assert _verdict_for(store, tip, "app.py:12") == "unverifiable"


def test_a_retired_memorys_baseline_leaves_with_its_anchor(origin, store, tmp_path):
    """The ``drift_prune`` twin: a baseline describes a binding, so it must not
    outlive one."""
    eid = seed_episode(store, "greet lesson")
    svc = _armed(origin, store, tmp_path)
    svc.tick()
    assert (
        store.conn.execute(
            "SELECT COUNT(*) c FROM anchor_baselines WHERE repo=?", (REPO,)
        ).fetchone()["c"]
        == 1
    )

    assert store.deprecate(eid, actor="agent-a", ts=20)
    svc.tick()
    assert (
        store.conn.execute(
            "SELECT COUNT(*) c FROM anchor_baselines WHERE repo=?", (REPO,)
        ).fetchone()["c"]
        == 0
    )


# ── one hostile binding may not fault the leg (the per-binding guard) ─────────
def test_an_unaskable_binding_leaves_its_row_unprobed_and_the_leg_intact(
    origin, store, tmp_path
):
    """A stored spelling git cannot even be HANDED — a control character reaches the
    argv and raises before the process starts — is the same case as one git cannot
    answer: the row stays unprobed and materializes nothing, while every other anchor
    in the repo still earns its verdict and the tick's prunes still run.

    Without the guard the raise escapes the whole materializer, so no verdict is
    written at all and every cached one freezes at its last value."""
    poison = "poison.py\x00::boom"
    seed_episode(store, "greet lesson")
    seed_episode(store, "poisoned binding", anchor=poison)
    svc = _armed(origin, store, tmp_path)

    svc.tick()

    tip = origin.origin_sha("refs/heads/main")
    assert _verdict_for(store, tip, ANCHOR) == "fresh", "a sibling lost its verdict"
    assert _verdict_for(store, tip, poison) is None, (
        "an unprobed binding must materialize NOTHING — absence is what the reader "
        "turns into `unverifiable`, the honest unknown"
    )
    probes = store.anchor_symbol_probes(REPO)
    assert probes[(tip, ANCHOR)] == 1
    assert probes[(tip, poison)] == -1, (
        "the unaskable binding recorded a probe result it never measured"
    )
    assert meta(store, f"sync:{REPO}:last_error") is None
