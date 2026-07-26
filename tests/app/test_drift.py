"""Unit suite for ``hive.app.drift`` — the single owner of the §3.4 drift wire
semantics: the verify→wire mapping (an EXHAUSTIVE stale table, else →
unverifiable), the most-severe-wins aggregation (CT-4's property vocabulary at
unit level), the cache lookup at the tip of the queried ref else canonical, and
the fail-open ``attach_drift`` enrichment (a reader fault degrades that hit to
unverifiable, never breaks the read)."""

from __future__ import annotations

import itertools

import pytest

from hive.app.drift import (
    DRIFT_ANCHOR_CHANGED,
    DRIFT_ANCHOR_MISSING,
    DRIFT_BLAST_RADIUS_CHANGED,
    DRIFT_BRANCH_SCOPED,
    DRIFT_FRESH,
    DRIFT_NA,
    DRIFT_UNVERIFIABLE,
    SEVERITY_ORDER,
    WIRE_VERDICTS,
    _STALE_ARROWS,
    aggregate_verdicts,
    attach_drift,
    branch_route_verdict,
    wire_verdict,
)
from hive.domain.retirement import QUALIFYING_DRIFT

TIP = "a" * 40


# ── the wire vocabulary itself ────────────────────────────────────────────────


def test_severity_order_is_the_normative_one():
    assert SEVERITY_ORDER == (
        DRIFT_ANCHOR_MISSING,
        DRIFT_ANCHOR_CHANGED,
        DRIFT_BLAST_RADIUS_CHANGED,
        DRIFT_BRANCH_SCOPED,
        DRIFT_UNVERIFIABLE,
        DRIFT_FRESH,
    )
    assert set(WIRE_VERDICTS) == set(SEVERITY_ORDER) | {DRIFT_NA}


# ── verify → wire mapping (§3.4 verbatim, else → unverifiable) ────────────────


@pytest.mark.parametrize(
    "state, reason, expected",
    [
        ("current", "", DRIFT_FRESH),
        ("stale", "signature_changed", DRIFT_ANCHOR_CHANGED),
        ("stale", "symbol_missing", DRIFT_ANCHOR_MISSING),
        # a gone FILE is the same claim as a gone symbol — the thing this memory
        # names is not there — so it lands in the same tier, not in the fail-safe
        ("stale", "file_missing", DRIFT_ANCHOR_MISSING),
        ("radius_changed", "", DRIFT_BLAST_RADIUS_CHANGED),
        ("radius changed", "", DRIFT_BLAST_RADIUS_CHANGED),  # the table's spelling
        ("branch_scoped", "", DRIFT_BRANCH_SCOPED),
    ],
)
def test_the_six_named_arrows(state, reason, expected):
    assert wire_verdict(state, reason) == expected


def test_composite_stale_state_carries_its_reason():
    assert wire_verdict("stale/signature_changed") == DRIFT_ANCHOR_CHANGED
    assert wire_verdict("stale/symbol_missing") == DRIFT_ANCHOR_MISSING
    assert wire_verdict("stale/file_missing") == DRIFT_ANCHOR_MISSING


def test_reason_codes_match_by_prefix():
    assert (
        wire_verdict("stale", "signature_changed:def f(a, b)") == DRIFT_ANCHOR_CHANGED
    )
    assert wire_verdict("stale", "symbol_missing: fn gone") == DRIFT_ANCHOR_MISSING
    assert wire_verdict("stale", "file_missing: pkg/f.py") == DRIFT_ANCHOR_MISSING


def test_every_stale_arrow_targets_the_wire_enum_without_shadowing():
    """The stale table decides by PREFIX in order, so two facts have to hold or
    the table quietly stops meaning what it reads as: every target must be a
    verdict the wire actually advertises (and a per-anchor one — ``n/a`` is an
    aggregate-only verdict), and no earlier prefix may swallow a later one that
    was given a DIFFERENT target, which would let table ORDER, not the decision,
    pick the tier."""
    for prefix, target in _STALE_ARROWS:
        assert target in WIRE_VERDICTS, f"{prefix} → {target} is off the wire enum"
        assert target in SEVERITY_ORDER, f"{prefix} → {target} is not per-anchor"
    for i, (earlier, earlier_target) in enumerate(_STALE_ARROWS):
        for later, later_target in _STALE_ARROWS[i + 1 :]:
            assert not (later.startswith(earlier) and later_target != earlier_target), (
                f"{earlier!r} shadows {later!r} with a different target "
                f"({earlier_target} vs {later_target}) — order decides silently"
            )


@pytest.mark.parametrize(
    "state, reason",
    [
        ("stale", ""),  # bare stale: incomparable, never false-stale
        # a stale reason this server does not RECOGNIZE — an older or newer
        # engine's output, a hostile cache row. Not "unenumerated": every reason
        # the engine can classify stale has a decided arrow above.
        ("stale", "some_new_reason"),
        ("stale", "module_relocated"),
        ("stale/xyzzy", ""),
        ("staleness", "signature_changed"),  # not the stale state
        ("CURRENT", ""),  # vocabulary is exact, no case folding
        ("unknown_state", ""),
        ("", ""),
        (None, ""),
        (42, "signature_changed"),
        ("stale", 42),
        (["current"], ""),
    ],
)
def test_everything_else_maps_unverifiable(state, reason):
    assert wire_verdict(state, reason) == DRIFT_UNVERIFIABLE


def test_the_fail_safe_fall_through_stays_silent_never_stale():
    """The stale arm's final fall-through is a FAIL-SAFE, not a bucket: a reason
    this server cannot recognize reads unverifiable, and unverifiable is outside
    the tier that qualifies a retirement — so an unrecognized reason can never
    destroy a memory on evidence nobody decoded."""
    for state, reason in (
        ("stale", "module_relocated"),
        ("stale/module_relocated", ""),
    ):
        verdict = wire_verdict(state, reason)
        assert verdict == DRIFT_UNVERIFIABLE
        assert verdict not in QUALIFYING_DRIFT


# ── branch_route_verdict: routing a materialized verdict through the memory's
# own declared line (§3.4, branch_scoped computed at read time) ───────────────


@pytest.mark.parametrize(
    "base, declared_ref, consumer_ref, expected",
    [
        # declared == consumer (the memory's own line): never softened, whatever
        # the base — this is what keeps a declared ref from becoming immunity.
        (DRIFT_ANCHOR_CHANGED, "feature", "feature", DRIFT_ANCHOR_CHANGED),
        (DRIFT_ANCHOR_MISSING, "feature", "feature", DRIFT_ANCHOR_MISSING),
        (DRIFT_BLAST_RADIUS_CHANGED, "feature", "feature", DRIFT_BLAST_RADIUS_CHANGED),
        (DRIFT_FRESH, "feature", "feature", DRIFT_FRESH),
        # declared != consumer, stale-tier base: routed to the advisory branch_scoped
        (DRIFT_ANCHOR_CHANGED, "feature", "main", DRIFT_BRANCH_SCOPED),
        (DRIFT_ANCHOR_MISSING, "feature", "main", DRIFT_BRANCH_SCOPED),
        (DRIFT_BLAST_RADIUS_CHANGED, "feature", "main", DRIFT_BRANCH_SCOPED),
        # declared != consumer, fresh base: NEVER downgraded — rides verbatim
        (DRIFT_FRESH, "feature", "main", DRIFT_FRESH),
        # declared != consumer, already-advisory/unverifiable/n/a bases: verbatim
        (DRIFT_UNVERIFIABLE, "feature", "main", DRIFT_UNVERIFIABLE),
        (DRIFT_NA, "feature", "main", DRIFT_NA),
        # no declared ref at all: base always verbatim, whatever the consumer
        (DRIFT_ANCHOR_CHANGED, "", "main", DRIFT_ANCHOR_CHANGED),
        (DRIFT_ANCHOR_CHANGED, "", "", DRIFT_ANCHOR_CHANGED),
        # unknown consumer (e.g. a registry/canonical read that came back empty):
        # base verbatim — routing needs BOTH refs known
        (DRIFT_ANCHOR_CHANGED, "feature", "", DRIFT_ANCHOR_CHANGED),
    ],
)
def test_branch_route_verdict_truth_table(base, declared_ref, consumer_ref, expected):
    assert (
        branch_route_verdict(base, declared_ref=declared_ref, consumer_ref=consumer_ref)
        == expected
    )


def test_branch_route_verdict_never_returns_fresh_for_a_stale_base():
    for base in QUALIFYING_DRIFT:
        for declared, consumer in (
            ("feature", "main"),
            ("main", "feature"),
            ("a", "b"),
        ):
            assert (
                branch_route_verdict(base, declared_ref=declared, consumer_ref=consumer)
                != DRIFT_FRESH
            )


def test_branch_route_verdict_is_pure_and_total_over_every_wire_member():
    # every advertised verdict, routed with an off-set pair, either stays put or
    # becomes the advisory branch_scoped — never anything outside the enum.
    for base in WIRE_VERDICTS:
        routed = branch_route_verdict(base, declared_ref="feature", consumer_ref="main")
        assert routed in (base, DRIFT_BRANCH_SCOPED)


# ── most-severe-wins aggregation (the CT-4 property vocabulary) ───────────────


def test_empty_aggregates_na():
    assert aggregate_verdicts([]) == DRIFT_NA
    assert aggregate_verdicts(iter(())) == DRIFT_NA


@pytest.mark.parametrize("verdict", SEVERITY_ORDER)
def test_singleton_identity(verdict):
    assert aggregate_verdicts([verdict]) == verdict


def test_most_severe_wins_over_every_pair():
    for a, b in itertools.product(SEVERITY_ORDER, SEVERITY_ORDER):
        expected = a if SEVERITY_ORDER.index(a) <= SEVERITY_ORDER.index(b) else b
        assert aggregate_verdicts([a, b]) == expected


def test_fresh_only_when_all_fresh():
    assert aggregate_verdicts([DRIFT_FRESH, DRIFT_FRESH, DRIFT_FRESH]) == DRIFT_FRESH
    for other in SEVERITY_ORDER[:-1]:
        assert aggregate_verdicts([DRIFT_FRESH, other]) != DRIFT_FRESH


def test_partially_unverifiable_never_fresh():
    assert aggregate_verdicts([DRIFT_FRESH, DRIFT_UNVERIFIABLE]) == DRIFT_UNVERIFIABLE


def test_permutation_invariant_and_idempotent():
    vs = [DRIFT_FRESH, DRIFT_BRANCH_SCOPED, DRIFT_ANCHOR_CHANGED, DRIFT_FRESH]
    for perm in itertools.permutations(vs):
        assert aggregate_verdicts(list(perm)) == DRIFT_ANCHOR_CHANGED
    assert aggregate_verdicts([aggregate_verdicts(vs)]) == aggregate_verdicts(vs)


def test_hostile_members_coerce_to_unverifiable_never_fresh():
    # out-of-vocabulary, aggregate-only n/a, non-str, unhashable — all fail-safe
    for member in ("bogus", DRIFT_NA, None, 42, ["fresh"]):
        assert aggregate_verdicts([member]) == DRIFT_UNVERIFIABLE
        assert aggregate_verdicts([DRIFT_FRESH, member]) == DRIFT_UNVERIFIABLE
        assert (
            aggregate_verdicts([DRIFT_ANCHOR_MISSING, member]) == DRIFT_ANCHOR_MISSING
        )


# ── attach_drift: the fail-open recall-side enrichment ────────────────────────


class _Row(dict):
    """A mapping row (the sqlite3.Row read shape: row['value'])."""


class _FakeConn:
    def __init__(self, meta: dict[str, str]):
        self.meta = meta

    def execute(self, sql, args=()):
        assert "FROM meta" in sql, f"unexpected store SQL from drift: {sql}"
        value = self.meta.get(args[0])
        row = None if value is None else _Row(value=value)

        class _Cursor:
            def fetchone(_self):
                return row

        return _Cursor()


class _Repo:
    def __init__(self, name, canonical_ref):
        self.name = name
        self.canonical_ref = canonical_ref


class FakeStore:
    """The duck-typed SqliteEpisodeStore surface attach_drift consumes."""

    def __init__(
        self, *, registry=(("alpha", "main"),), tips=None, drift=None, ref_tips=None
    ):
        self._registry = [_Repo(n, c) for n, c in registry]
        self.conn = _FakeConn(tips or {})
        self._drift = drift or {}  # (repo, tip) -> {anchor: (verdict, detail)}
        self._ref_tips = ref_tips or {}  # (repo, ref) -> tip_sha
        self.touches: list[tuple[str, str, int]] = []
        self.drift_get_calls = 0
        self.ref_tip_calls = 0

    def repo_registry(self):
        return list(self._registry)

    def drift_get(self, repo, tip_sha, anchors):
        self.drift_get_calls += 1
        rows = self._drift.get((repo, tip_sha), {})
        return {a: rows[a] for a in anchors if a in rows}

    def ref_tip(self, repo, ref):
        self.ref_tip_calls += 1
        return self._ref_tips.get((repo, ref))

    def touch_ref_request(self, repo, ref, ts):
        self.touches.append((repo, ref, int(ts)))


def _store(**kw):
    kw.setdefault("tips", {"sync:alpha:last_tip": TIP})
    return FakeStore(**kw)


def _hit(anchors):
    return {"episode_id": 1, "text": "t", "anchors": anchors}


def test_materialized_verdict_rides_the_hit():
    store = _store(drift={("alpha", TIP): {"a.py::f": (DRIFT_ANCHOR_CHANGED, "{}")}})
    (hit,) = attach_drift([_hit([{"repo": "alpha", "anchor": "a.py::f"}])], store=store)
    assert hit["drift"]["type"] == DRIFT_ANCHOR_CHANGED
    (entry,) = hit["drift"]["detail"]["per_anchor"]
    assert entry == {
        "repo": "alpha",
        "anchor": "a.py::f",
        "tip_sha": TIP,
        "verdict": DRIFT_ANCHOR_CHANGED,
    }


def test_cache_miss_at_a_resolved_tip_is_unverifiable():
    store = _store()  # tip known, no anchor_drift row
    (hit,) = attach_drift(
        [_hit([{"repo": "alpha", "anchor": "cold.py::f"}])], store=store
    )
    assert hit["drift"]["type"] == DRIFT_UNVERIFIABLE
    (entry,) = hit["drift"]["detail"]["per_anchor"]
    assert entry["verdict"] == DRIFT_UNVERIFIABLE
    assert entry["tip_sha"] == TIP  # the tip WAS resolved; the anchor is absent


def test_unknown_tip_is_unverifiable_without_tip_sha():
    store = FakeStore()  # no watermark meta at all
    (hit,) = attach_drift([_hit([{"repo": "alpha", "anchor": "a.py::f"}])], store=store)
    assert hit["drift"]["type"] == DRIFT_UNVERIFIABLE
    (entry,) = hit["drift"]["detail"]["per_anchor"]
    assert "tip_sha" not in entry
    assert store.drift_get_calls == 0  # nothing to look up under no tip


def test_general_hit_is_na_and_never_consults_the_reader():
    store = _store()
    hits = attach_drift(
        [_hit([]), {"episode_id": 2, "text": "no anchors key"}], store=store
    )
    for hit in hits:
        assert hit["drift"] == {"type": DRIFT_NA, "detail": {"per_anchor": []}}
    assert store.drift_get_calls == 0


def test_multi_anchor_most_severe_wins():
    store = _store(
        drift={
            ("alpha", TIP): {
                "ok.py::f": (DRIFT_FRESH, "{}"),
                "gone.py::f": (DRIFT_ANCHOR_MISSING, "{}"),
            }
        }
    )
    (hit,) = attach_drift(
        [
            _hit(
                [
                    {"repo": "alpha", "anchor": "ok.py::f"},
                    {"repo": "alpha", "anchor": "gone.py::f"},
                ]
            )
        ],
        store=store,
    )
    assert hit["drift"]["type"] == DRIFT_ANCHOR_MISSING
    verdicts = {
        (e["anchor"], e["verdict"]) for e in hit["drift"]["detail"]["per_anchor"]
    }
    assert verdicts == {("ok.py::f", DRIFT_FRESH), ("gone.py::f", DRIFT_ANCHOR_MISSING)}
    assert store.drift_get_calls == 1  # one batched read per (hit, repo)


def test_partially_unmaterialized_hit_never_reads_fresh():
    store = _store(drift={("alpha", TIP): {"warm.py::f": (DRIFT_FRESH, "{}")}})
    (hit,) = attach_drift(
        [
            _hit(
                [
                    {"repo": "alpha", "anchor": "warm.py::f"},
                    {"repo": "alpha", "anchor": "never.py::f"},
                ]
            )
        ],
        store=store,
    )
    assert hit["drift"]["type"] == DRIFT_UNVERIFIABLE


def test_out_of_vocabulary_cache_row_serves_unverifiable():
    store = _store(drift={("alpha", TIP): {"a.py::f": ("bogus_verdict", "{}")}})
    (hit,) = attach_drift([_hit([{"repo": "alpha", "anchor": "a.py::f"}])], store=store)
    assert hit["drift"]["type"] == DRIFT_UNVERIFIABLE
    (entry,) = hit["drift"]["detail"]["per_anchor"]
    assert entry["verdict"] == DRIFT_UNVERIFIABLE  # the wire shape promises the enum


# ── name@branch ref routing ───────────────────────────────────────────────────


def test_unresolved_queried_branch_degrades_and_records_demand():
    """No ``ref_tips`` row for ``(alpha, feature)`` yet: the branch tip is
    genuinely UNKNOWN — ``tip_for`` is consulted (never hardcoded to None) and
    honestly reports no ``tip_sha``, distinct from a resolved-but-unverified
    tip (below)."""
    store = _store(drift={("alpha", TIP): {"a.py::f": (DRIFT_FRESH, "{}")}})
    (hit,) = attach_drift(
        [_hit([{"repo": "alpha", "anchor": "a.py::f"}])],
        store=store,
        queried_repos=["alpha@feature"],
        now=424_242,
    )
    drift = hit["drift"]
    assert drift["type"] == DRIFT_UNVERIFIABLE, (
        "an unresolved branch tip must never serve the canonical verdict"
    )
    assert drift["detail"]["ref"] == "feature"
    (entry,) = drift["detail"]["per_anchor"]
    assert "tip_sha" not in entry, "an unresolved tip is honestly absent"
    assert store.touches == [("alpha", "feature", 424_242)]
    assert store.drift_get_calls == 0
    assert store.ref_tip_calls == 1


def test_resolved_queried_branch_serves_its_materialized_verdict():
    """``tip_for`` is the single owner of tip resolution: a branch tip
    the materializer already recorded in ``ref_tips`` is looked up and its
    cached verdict rides the hit — the read half of BUG-063."""
    feature_tip = "b" * 40
    store = _store(
        ref_tips={("alpha", "feature"): feature_tip},
        drift={("alpha", feature_tip): {"a.py::f": (DRIFT_ANCHOR_CHANGED, "{}")}},
    )
    (hit,) = attach_drift(
        [_hit([{"repo": "alpha", "anchor": "a.py::f"}])],
        store=store,
        queried_repos=["alpha@feature"],
        now=1,
    )
    assert hit["drift"]["type"] == DRIFT_ANCHOR_CHANGED
    assert hit["drift"]["detail"]["ref"] == "feature", (
        "detail.ref rides whenever a repo was branch-routed, resolved or not"
    )
    (entry,) = hit["drift"]["detail"]["per_anchor"]
    assert entry["tip_sha"] == feature_tip
    assert entry["verdict"] == DRIFT_ANCHOR_CHANGED
    assert store.touches == [("alpha", "feature", 1)]  # demand still recorded


def test_resolved_but_not_yet_verified_branch_tip_is_unverifiable_not_fresh():
    """The budget-starved shape (I1): ``ref_tips`` already knows the tip, but no
    ``anchor_drift`` row exists for it yet — this must read unverifiable WITH
    the tip known, never fall back to a fresher-looking verdict from a
    different (e.g. canonical) tip."""
    feature_tip = "c" * 40
    store = _store(ref_tips={("alpha", "feature"): feature_tip})  # no drift row yet
    (hit,) = attach_drift(
        [_hit([{"repo": "alpha", "anchor": "a.py::f"}])],
        store=store,
        queried_repos=["alpha@feature"],
        now=1,
    )
    assert hit["drift"]["type"] == DRIFT_UNVERIFIABLE
    (entry,) = hit["drift"]["detail"]["per_anchor"]
    assert entry["tip_sha"] == feature_tip, "the tip is known even though unverified"
    assert entry["verdict"] == DRIFT_UNVERIFIABLE


def test_demand_is_the_query_not_the_hit():
    store = _store()
    out = attach_drift([], store=store, queried_repos=["alpha@feature"], now=7)
    assert out == []
    assert store.touches == [("alpha", "feature", 7)]


def test_queried_canonical_branch_routes_canonical():
    store = _store(drift={("alpha", TIP): {"a.py::f": (DRIFT_FRESH, "{}")}})
    (hit,) = attach_drift(
        [_hit([{"repo": "alpha", "anchor": "a.py::f"}])],
        store=store,
        queried_repos=["alpha@main"],
        now=7,
    )
    assert hit["drift"]["type"] == DRIFT_FRESH
    assert "ref" not in hit["drift"]["detail"]
    assert store.touches == []  # the canonical ref needs no materialization demand


def test_plain_name_scope_routes_canonical():
    store = _store(drift={("alpha", TIP): {"a.py::f": (DRIFT_FRESH, "{}")}})
    (hit,) = attach_drift(
        [_hit([{"repo": "alpha", "anchor": "a.py::f"}])],
        store=store,
        queried_repos=["alpha"],
    )
    assert hit["drift"]["type"] == DRIFT_FRESH
    assert store.touches == []


def test_parsed_pair_scope_entries_are_accepted():
    store = _store()
    (hit,) = attach_drift(
        [_hit([{"repo": "alpha", "anchor": "a.py::f"}])],
        store=store,
        queried_repos=[("alpha", "feature")],
        now=9,
    )
    assert hit["drift"]["detail"]["ref"] == "feature"
    assert store.touches == [("alpha", "feature", 9)]


def test_branch_scope_only_touches_its_own_repo():
    store = _store(
        registry=(("alpha", "main"), ("beta", "main")),
        tips={"sync:alpha:last_tip": TIP, "sync:beta:last_tip": TIP},
        drift={("beta", TIP): {"b.py::g": (DRIFT_FRESH, "{}")}},
    )
    (hit,) = attach_drift(
        [_hit([{"repo": "beta", "anchor": "b.py::g"}])],
        store=store,
        queried_repos=["alpha@feature", "beta"],
        now=5,
    )
    assert hit["drift"]["type"] == DRIFT_FRESH  # beta rides canonical, unaffected
    assert store.touches == [("alpha", "feature", 5)]


# ── fail-open: a reader fault degrades, never breaks the read ─────────────────


class _BoomStore(FakeStore):
    def drift_get(self, repo, tip_sha, anchors):
        raise RuntimeError("drift cache exploded")


def test_reader_fault_degrades_that_hit_only():
    store = _BoomStore(tips={"sync:alpha:last_tip": TIP})
    anchored = _hit([{"repo": "alpha", "anchor": "a.py::f"}])
    general = {"episode_id": 2, "anchors": []}
    hits = attach_drift([anchored, general], store=store)
    assert hits[0]["drift"] == {
        "type": DRIFT_UNVERIFIABLE,
        "detail": {"per_anchor": []},
    }
    assert hits[1]["drift"]["type"] == DRIFT_NA  # untouched by the fault


def test_registry_fault_degrades_not_breaks():
    store = _store(drift={("alpha", TIP): {"a.py::f": (DRIFT_FRESH, "{}")}})
    store.repo_registry = None  # not even callable
    (hit,) = attach_drift([_hit([{"repo": "alpha", "anchor": "a.py::f"}])], store=store)
    # canonical tip lookup needs no registry — the verdict still rides
    assert hit["drift"]["type"] == DRIFT_FRESH


def test_touch_fault_never_breaks_the_read():
    store = _store()

    def boom(*a, **kw):
        raise RuntimeError("ref_requests write exploded")

    store.touch_ref_request = boom
    (hit,) = attach_drift(
        [_hit([{"repo": "alpha", "anchor": "a.py::f"}])],
        store=store,
        queried_repos=["alpha@feature"],
    )
    assert hit["drift"]["type"] == DRIFT_UNVERIFIABLE
    assert hit["drift"]["detail"]["ref"] == "feature"


def test_store_without_conn_degrades_to_unverifiable():
    class Bare:
        def repo_registry(self):
            return []

    (hit,) = attach_drift(
        [_hit([{"repo": "alpha", "anchor": "a.py::f"}])], store=Bare()
    )
    assert hit["drift"]["type"] == DRIFT_UNVERIFIABLE


def test_malformed_anchor_entry_contributes_unverifiable():
    store = _store(drift={("alpha", TIP): {"a.py::f": (DRIFT_FRESH, "{}")}})
    (hit,) = attach_drift(
        [_hit([{"repo": "alpha", "anchor": "a.py::f"}, "not-an-object"])], store=store
    )
    assert hit["drift"]["type"] == DRIFT_UNVERIFIABLE, (
        "an unparseable binding can never let the hit read fresh"
    )
    assert len(hit["drift"]["detail"]["per_anchor"]) == 2


def test_non_dict_hits_are_skipped_and_list_identity_kept():
    store = _store()
    hits = [None, "hit", _hit([])]
    out = attach_drift(hits, store=store)
    assert out is hits
    assert out[2]["drift"]["type"] == DRIFT_NA


def test_hostile_scope_entries_are_ignored_not_fatal():
    store = _store(drift={("alpha", TIP): {"a.py::f": (DRIFT_FRESH, "{}")}})
    (hit,) = attach_drift(
        [_hit([{"repo": "alpha", "anchor": "a.py::f"}])],
        store=store,
        queried_repos=[None, 42, ("a", "b", "c"), ""],
    )
    assert hit["drift"]["type"] == DRIFT_FRESH
    assert store.touches == []


def test_the_anchor_moved_tier_has_one_owner():
    """J8. "Which verdicts mean the anchor MOVED" is ONE fact with two policies —
    it qualifies a retirement in the gate, and it is what ``branch_route_verdict``
    may soften to ``branch_scoped``. Asserted by IDENTITY, not by value: a second
    copy that happens to agree today would pass a value check and drift tomorrow.
    The tier lives in hive/domain/ because app may import domain, never the
    reverse."""
    import hive.app.drift as drift_module

    assert not hasattr(drift_module, "STALE_TIER"), (
        "the app-side copy of the anchor-moved tier must not exist — one fact, "
        "one owner"
    )
    routed = {
        base
        for base in WIRE_VERDICTS
        # a base that IS branch_scoped rides verbatim — softening is a CHANGE
        if branch_route_verdict(base, declared_ref="feature", consumer_ref="main")
        != base
    }
    assert routed == QUALIFYING_DRIFT, (
        "branch_route_verdict must soften EXACTLY the tier the retirement gate "
        f"qualifies on: {routed} != {set(QUALIFYING_DRIFT)}"
    )
