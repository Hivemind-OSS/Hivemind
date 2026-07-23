"""Unit suite for ``hive.app.drift`` — the single owner of the §3.4 drift wire
semantics: the verify→wire mapping (else → unverifiable), the most-severe-wins
aggregation (CT-4's property vocabulary at unit level), the cache lookup at the
tip of the queried ref else canonical, and the fail-open ``attach_drift``
enrichment (a reader fault degrades that hit to unverifiable, never breaks the
read)."""
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
    aggregate_verdicts,
    attach_drift,
    canonical_tip_key,
    wire_verdict,
)

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


def test_canonical_tip_key_is_the_sync_watermark():
    assert canonical_tip_key("alpha") == "sync:alpha:last_tip"


# ── verify → wire mapping (§3.4 verbatim, else → unverifiable) ────────────────

@pytest.mark.parametrize(
    "state, reason, expected",
    [
        ("current", "", DRIFT_FRESH),
        ("stale", "signature_changed", DRIFT_ANCHOR_CHANGED),
        ("stale", "symbol_missing", DRIFT_ANCHOR_MISSING),
        ("radius_changed", "", DRIFT_BLAST_RADIUS_CHANGED),
        ("radius changed", "", DRIFT_BLAST_RADIUS_CHANGED),  # the table's spelling
        ("branch_scoped", "", DRIFT_BRANCH_SCOPED),
    ],
)
def test_the_five_named_arrows(state, reason, expected):
    assert wire_verdict(state, reason) == expected


def test_composite_stale_state_carries_its_reason():
    assert wire_verdict("stale/signature_changed") == DRIFT_ANCHOR_CHANGED
    assert wire_verdict("stale/symbol_missing") == DRIFT_ANCHOR_MISSING


def test_reason_codes_match_by_prefix():
    assert wire_verdict("stale", "signature_changed:def f(a, b)") == DRIFT_ANCHOR_CHANGED
    assert wire_verdict("stale", "symbol_missing: fn gone") == DRIFT_ANCHOR_MISSING


@pytest.mark.parametrize(
    "state, reason",
    [
        ("stale", ""),                       # bare stale: incomparable, never false-stale
        ("stale", "some_new_reason"),
        ("stale/xyzzy", ""),
        ("staleness", "signature_changed"),  # not the stale state
        ("CURRENT", ""),                     # vocabulary is exact, no case folding
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
    assert (aggregate_verdicts([DRIFT_FRESH, DRIFT_UNVERIFIABLE])
            == DRIFT_UNVERIFIABLE)


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
        assert (aggregate_verdicts([DRIFT_ANCHOR_MISSING, member])
                == DRIFT_ANCHOR_MISSING)


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

    def __init__(self, *, registry=(("alpha", "main"),), tips=None, drift=None):
        self._registry = [_Repo(n, c) for n, c in registry]
        self.conn = _FakeConn(tips or {})
        self._drift = drift or {}      # (repo, tip) -> {anchor: (verdict, detail)}
        self.touches: list[tuple[str, str, int]] = []
        self.drift_get_calls = 0

    def repo_registry(self):
        return list(self._registry)

    def drift_get(self, repo, tip_sha, anchors):
        self.drift_get_calls += 1
        rows = self._drift.get((repo, tip_sha), {})
        return {a: rows[a] for a in anchors if a in rows}

    def touch_ref_request(self, repo, ref, ts):
        self.touches.append((repo, ref, int(ts)))


def _store(**kw):
    kw.setdefault("tips", {"sync:alpha:last_tip": TIP})
    return FakeStore(**kw)


def _hit(anchors):
    return {"episode_id": 1, "text": "t", "anchors": anchors}


def test_materialized_verdict_rides_the_hit():
    store = _store(drift={("alpha", TIP): {"a.py::f": (DRIFT_ANCHOR_CHANGED, "{}")}})
    (hit,) = attach_drift([_hit([{"repo": "alpha", "anchor": "a.py::f"}])],
                          store=store)
    assert hit["drift"]["type"] == DRIFT_ANCHOR_CHANGED
    (entry,) = hit["drift"]["detail"]["per_anchor"]
    assert entry == {"repo": "alpha", "anchor": "a.py::f", "tip_sha": TIP,
                     "verdict": DRIFT_ANCHOR_CHANGED}


def test_cache_miss_at_a_resolved_tip_is_unverifiable():
    store = _store()  # tip known, no anchor_drift row
    (hit,) = attach_drift([_hit([{"repo": "alpha", "anchor": "cold.py::f"}])],
                          store=store)
    assert hit["drift"]["type"] == DRIFT_UNVERIFIABLE
    (entry,) = hit["drift"]["detail"]["per_anchor"]
    assert entry["verdict"] == DRIFT_UNVERIFIABLE
    assert entry["tip_sha"] == TIP  # the tip WAS resolved; the anchor is absent


def test_unknown_tip_is_unverifiable_without_tip_sha():
    store = FakeStore()  # no watermark meta at all
    (hit,) = attach_drift([_hit([{"repo": "alpha", "anchor": "a.py::f"}])],
                          store=store)
    assert hit["drift"]["type"] == DRIFT_UNVERIFIABLE
    (entry,) = hit["drift"]["detail"]["per_anchor"]
    assert "tip_sha" not in entry
    assert store.drift_get_calls == 0  # nothing to look up under no tip


def test_general_hit_is_na_and_never_consults_the_reader():
    store = _store()
    hits = attach_drift([_hit([]), {"episode_id": 2, "text": "no anchors key"}],
                        store=store)
    for hit in hits:
        assert hit["drift"] == {"type": DRIFT_NA, "detail": {"per_anchor": []}}
    assert store.drift_get_calls == 0


def test_multi_anchor_most_severe_wins():
    store = _store(drift={("alpha", TIP): {
        "ok.py::f": (DRIFT_FRESH, "{}"),
        "gone.py::f": (DRIFT_ANCHOR_MISSING, "{}"),
    }})
    (hit,) = attach_drift([_hit([{"repo": "alpha", "anchor": "ok.py::f"},
                                 {"repo": "alpha", "anchor": "gone.py::f"}])],
                          store=store)
    assert hit["drift"]["type"] == DRIFT_ANCHOR_MISSING
    verdicts = {(e["anchor"], e["verdict"])
                for e in hit["drift"]["detail"]["per_anchor"]}
    assert verdicts == {("ok.py::f", DRIFT_FRESH),
                        ("gone.py::f", DRIFT_ANCHOR_MISSING)}
    assert store.drift_get_calls == 1  # one batched read per (hit, repo)


def test_partially_unmaterialized_hit_never_reads_fresh():
    store = _store(drift={("alpha", TIP): {"warm.py::f": (DRIFT_FRESH, "{}")}})
    (hit,) = attach_drift([_hit([{"repo": "alpha", "anchor": "warm.py::f"},
                                 {"repo": "alpha", "anchor": "never.py::f"}])],
                          store=store)
    assert hit["drift"]["type"] == DRIFT_UNVERIFIABLE


def test_out_of_vocabulary_cache_row_serves_unverifiable():
    store = _store(drift={("alpha", TIP): {"a.py::f": ("bogus_verdict", "{}")}})
    (hit,) = attach_drift([_hit([{"repo": "alpha", "anchor": "a.py::f"}])],
                          store=store)
    assert hit["drift"]["type"] == DRIFT_UNVERIFIABLE
    (entry,) = hit["drift"]["detail"]["per_anchor"]
    assert entry["verdict"] == DRIFT_UNVERIFIABLE  # the wire shape promises the enum


# ── name@branch ref routing ───────────────────────────────────────────────────

def test_queried_branch_degrades_and_records_demand():
    store = _store(drift={("alpha", TIP): {"a.py::f": (DRIFT_FRESH, "{}")}})
    (hit,) = attach_drift([_hit([{"repo": "alpha", "anchor": "a.py::f"}])],
                          store=store, queried_repos=["alpha@feature"], now=424_242)
    drift = hit["drift"]
    assert drift["type"] == DRIFT_UNVERIFIABLE, (
        "an unmaterialized branch tip must never serve the canonical verdict")
    assert drift["detail"]["ref"] == "feature"
    assert store.touches == [("alpha", "feature", 424_242)]
    assert store.drift_get_calls == 0


def test_demand_is_the_query_not_the_hit():
    store = _store()
    out = attach_drift([], store=store, queried_repos=["alpha@feature"], now=7)
    assert out == []
    assert store.touches == [("alpha", "feature", 7)]


def test_queried_canonical_branch_routes_canonical():
    store = _store(drift={("alpha", TIP): {"a.py::f": (DRIFT_FRESH, "{}")}})
    (hit,) = attach_drift([_hit([{"repo": "alpha", "anchor": "a.py::f"}])],
                          store=store, queried_repos=["alpha@main"], now=7)
    assert hit["drift"]["type"] == DRIFT_FRESH
    assert "ref" not in hit["drift"]["detail"]
    assert store.touches == []  # the canonical ref needs no materialization demand


def test_plain_name_scope_routes_canonical():
    store = _store(drift={("alpha", TIP): {"a.py::f": (DRIFT_FRESH, "{}")}})
    (hit,) = attach_drift([_hit([{"repo": "alpha", "anchor": "a.py::f"}])],
                          store=store, queried_repos=["alpha"])
    assert hit["drift"]["type"] == DRIFT_FRESH
    assert store.touches == []


def test_parsed_pair_scope_entries_are_accepted():
    store = _store()
    (hit,) = attach_drift([_hit([{"repo": "alpha", "anchor": "a.py::f"}])],
                          store=store, queried_repos=[("alpha", "feature")], now=9)
    assert hit["drift"]["detail"]["ref"] == "feature"
    assert store.touches == [("alpha", "feature", 9)]


def test_branch_scope_only_touches_its_own_repo():
    store = _store(registry=(("alpha", "main"), ("beta", "main")),
                   tips={"sync:alpha:last_tip": TIP, "sync:beta:last_tip": TIP},
                   drift={("beta", TIP): {"b.py::g": (DRIFT_FRESH, "{}")}})
    (hit,) = attach_drift([_hit([{"repo": "beta", "anchor": "b.py::g"}])],
                          store=store,
                          queried_repos=["alpha@feature", "beta"], now=5)
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
    assert hits[0]["drift"] == {"type": DRIFT_UNVERIFIABLE,
                                "detail": {"per_anchor": []}}
    assert hits[1]["drift"]["type"] == DRIFT_NA  # untouched by the fault


def test_registry_fault_degrades_not_breaks():
    store = _store(drift={("alpha", TIP): {"a.py::f": (DRIFT_FRESH, "{}")}})
    store.repo_registry = None  # not even callable
    (hit,) = attach_drift([_hit([{"repo": "alpha", "anchor": "a.py::f"}])],
                          store=store)
    # canonical tip lookup needs no registry — the verdict still rides
    assert hit["drift"]["type"] == DRIFT_FRESH


def test_touch_fault_never_breaks_the_read():
    store = _store()

    def boom(*a, **kw):
        raise RuntimeError("ref_requests write exploded")

    store.touch_ref_request = boom
    (hit,) = attach_drift([_hit([{"repo": "alpha", "anchor": "a.py::f"}])],
                          store=store, queried_repos=["alpha@feature"])
    assert hit["drift"]["type"] == DRIFT_UNVERIFIABLE
    assert hit["drift"]["detail"]["ref"] == "feature"


def test_store_without_conn_degrades_to_unverifiable():
    class Bare:
        def repo_registry(self):
            return []

    (hit,) = attach_drift([_hit([{"repo": "alpha", "anchor": "a.py::f"}])],
                          store=Bare())
    assert hit["drift"]["type"] == DRIFT_UNVERIFIABLE


def test_malformed_anchor_entry_contributes_unverifiable():
    store = _store(drift={("alpha", TIP): {"a.py::f": (DRIFT_FRESH, "{}")}})
    (hit,) = attach_drift(
        [_hit([{"repo": "alpha", "anchor": "a.py::f"}, "not-an-object"])],
        store=store)
    assert hit["drift"]["type"] == DRIFT_UNVERIFIABLE, (
        "an unparseable binding can never let the hit read fresh")
    assert len(hit["drift"]["detail"]["per_anchor"]) == 2


def test_non_dict_hits_are_skipped_and_list_identity_kept():
    store = _store()
    hits = [None, "hit", _hit([])]
    out = attach_drift(hits, store=store)
    assert out is hits
    assert out[2]["drift"]["type"] == DRIFT_NA


def test_hostile_scope_entries_are_ignored_not_fatal():
    store = _store(drift={("alpha", TIP): {"a.py::f": (DRIFT_FRESH, "{}")}})
    (hit,) = attach_drift([_hit([{"repo": "alpha", "anchor": "a.py::f"}])],
                          store=store,
                          queried_repos=[None, 42, ("a", "b", "c"), ""])
    assert hit["drift"]["type"] == DRIFT_FRESH
    assert store.touches == []
