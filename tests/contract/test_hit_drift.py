"""CT-4 — every served hit carries repos / anchors / drift (plan §4 intent 4,
§3.4).

Drift is read ONLY from the materialized ``anchor_drift`` cache at the tip of the
queried ref (``name@branch``) else the repo's canonical ref; a cache miss is
``unverifiable`` (fail-safe — never false-fresh, never false-stale); general ⇒
``n/a``; aggregation across anchors is most-severe-wins; a drift-reader fault
degrades the hit to ``unverifiable`` without breaking the read. Scenario matrix:
fresh; anchor_changed; anchor_missing; blast_radius_changed; branch_scoped;
unverifiable (no cache row); n/a (general); multi-anchor most-severe aggregation;
``name@branch`` ref routing.
"""

from __future__ import annotations

import json

import pytest

from tests.contract.conftest import (
    DRIFT_ANCHOR_CHANGED,
    DRIFT_ANCHOR_MISSING,
    DRIFT_BLAST_RADIUS,
    DRIFT_BRANCH_SCOPED,
    DRIFT_FRESH,
    DRIFT_NA,
    DRIFT_UNVERIFIABLE,
    drift_put,
    hit_for,
    make_rig,
    recall,
    ref_request_rows,
    register_repo,
    set_canonical_tip,
    write_ok,
)

TIP = "a" * 40
NOW = 1_000_000


def _rig(tmp_path):
    rig = make_rig(tmp_path)
    register_repo(
        rig.store, "alpha", "https://example.invalid/alpha.git", canonical_ref="main"
    )
    register_repo(
        rig.store, "beta", "https://example.invalid/beta.git", canonical_ref="main"
    )
    set_canonical_tip(rig.store, "alpha", TIP)
    return rig


def _anchored(rig, text: str, anchor: str, repo: str = "alpha") -> int:
    return write_ok(rig.server, text, anchors=[{"repo": repo, "anchor": anchor}])


def _drift_of(rig, text: str, eid: int, repos=None) -> dict:
    env = recall(rig.server, text, repos=repos)
    hit = hit_for(env, eid)
    assert "repos" in hit and "anchors" in hit, f"hit lacks scope fields: {hit}"
    drift = hit.get("drift")
    assert isinstance(drift, dict), f"every hit carries a drift object: {hit}"
    return drift


@pytest.mark.parametrize(
    "verdict",
    [
        DRIFT_FRESH,
        DRIFT_ANCHOR_CHANGED,
        DRIFT_ANCHOR_MISSING,
        DRIFT_BLAST_RADIUS,
    ],
)
def test_materialized_verdict_rides_the_hit(tmp_path, verdict):
    rig = _rig(tmp_path)
    text = f"fact about the {verdict} anchor"
    anchor = f"pkg/mod_{verdict}.py::fn"
    eid = _anchored(rig, text, anchor)
    drift_put(rig.store, [("alpha", TIP, anchor, verdict, "{}", NOW)])

    drift = _drift_of(rig, text, eid)
    assert drift.get("type") == verdict, f"cache verdict must ride the hit: {drift}"
    per_anchor = (drift.get("detail") or {}).get("per_anchor")
    assert per_anchor, f"detail.per_anchor is the per-anchor breakdown: {drift}"
    assert per_anchor[0].get("verdict") == verdict
    assert per_anchor[0].get("repo") == "alpha"
    assert per_anchor[0].get("anchor") == anchor


def test_branch_scoped_reached_only_through_the_memorys_declared_line(tmp_path):
    """Unlike every other member of the wire vocabulary, ``branch_scoped`` is
    never stored by a materializer — it is a served-time ROUTING of a real
    stale verdict through the memory's own declared line. Seeding it directly
    into the cache (as the parametrized case above does for every OTHER
    verdict) would prove nothing a production writer can reach; this drives
    the real declared-ref divergence instead — a memory that names a different
    line than the one it is read from, whose named line's own verdict is
    stale-tier."""
    rig = _rig(tmp_path)
    text = "fact about the branch_scoped anchor"
    anchor = "pkg/mod_branch_scoped.py::fn"
    eid = write_ok(
        rig.server,
        text,
        anchors=[{"repo": "alpha", "anchor": anchor}],
        repos=["alpha@feature"],
    )
    # materialized STALE at the canonical tip; the memory's declared line
    # (feature) is a different line than the one this cache row was judged at.
    drift_put(rig.store, [("alpha", TIP, anchor, DRIFT_BLAST_RADIUS, "{}", NOW)])

    drift = _drift_of(rig, text, eid, repos=["alpha"])  # consumer on canonical
    assert drift.get("type") == DRIFT_BRANCH_SCOPED, (
        f"a memory declared on a different line than the one it is read from "
        f"must route through branch_scoped, not the raw stale verdict: {drift}"
    )


def test_unmaterialized_tip_reads_unverifiable(tmp_path):
    rig = _rig(tmp_path)
    text = "fact whose tip was never materialized"
    eid = _anchored(rig, text, "pkg/cold.py::fn")
    # no anchor_drift row for (alpha, TIP, this anchor)
    drift = _drift_of(rig, text, eid)
    assert drift.get("type") == DRIFT_UNVERIFIABLE, (
        f"a cache miss is fail-safe unverifiable, never fresh/stale: {drift}"
    )


def test_general_memory_reads_na(tmp_path):
    rig = _rig(tmp_path)
    text = "a general memory with no repo and no anchor"
    eid = write_ok(rig.server, text)
    drift = _drift_of(rig, text, eid)
    assert drift.get("type") == DRIFT_NA
    assert hit_for(recall(rig.server, text), eid).get("repos") == []


def test_multi_anchor_most_severe_wins(tmp_path):
    rig = _rig(tmp_path)
    text = "a memory bound to two anchors with different verdicts"
    eid = write_ok(
        rig.server,
        text,
        anchors=[
            {"repo": "alpha", "anchor": "pkg/ok.py::fn"},
            {"repo": "alpha", "anchor": "pkg/gone.py::fn"},
        ],
    )
    drift_put(
        rig.store,
        [
            ("alpha", TIP, "pkg/ok.py::fn", DRIFT_FRESH, "{}", NOW),
            ("alpha", TIP, "pkg/gone.py::fn", DRIFT_ANCHOR_MISSING, "{}", NOW),
        ],
    )
    drift = _drift_of(rig, text, eid)
    assert drift.get("type") == DRIFT_ANCHOR_MISSING, (
        f"most-severe wins across anchors: {drift}"
    )
    verdicts = {
        (p.get("anchor"), p.get("verdict"))
        for p in (drift.get("detail") or {}).get("per_anchor", [])
    }
    assert ("pkg/ok.py::fn", DRIFT_FRESH) in verdicts
    assert ("pkg/gone.py::fn", DRIFT_ANCHOR_MISSING) in verdicts


def test_partially_unverifiable_never_reads_fresh(tmp_path):
    rig = _rig(tmp_path)
    text = "a memory with one fresh and one unmaterialized anchor"
    eid = write_ok(
        rig.server,
        text,
        anchors=[
            {"repo": "alpha", "anchor": "pkg/warm.py::fn"},
            {"repo": "alpha", "anchor": "pkg/never.py::fn"},
        ],
    )
    drift_put(rig.store, [("alpha", TIP, "pkg/warm.py::fn", DRIFT_FRESH, "{}", NOW)])
    drift = _drift_of(rig, text, eid)
    assert drift.get("type") != DRIFT_FRESH, (
        f"a partially-unverifiable memory can never read fresh (fail-safe): {drift}"
    )
    assert drift.get("type") == DRIFT_UNVERIFIABLE


def test_name_at_branch_routes_ref_and_records_request(tmp_path):
    rig = _rig(tmp_path)
    text = "fact judged against a feature branch"
    eid = _anchored(rig, text, "pkg/feat.py::fn")
    # canonical tip IS materialized — but the queried ref is the branch
    drift_put(rig.store, [("alpha", TIP, "pkg/feat.py::fn", DRIFT_FRESH, "{}", NOW)])

    env = recall(rig.server, text, repos=["alpha@feature"])
    hit = hit_for(env, eid)
    drift = hit.get("drift") or {}
    assert drift.get("type") == DRIFT_UNVERIFIABLE, (
        f"an unmaterialized branch tip reads unverifiable, never the canonical "
        f"verdict: {drift}"
    )
    detail = drift.get("detail") or {}
    assert detail.get("ref") == "feature", (
        f"the queried ref rides drift.detail.ref: {drift}"
    )
    # the recall touch drives on-demand materialization via ref_requests
    rows = ref_request_rows(rig.store)
    assert any(r.get("repo") == "alpha" and r.get("ref") == "feature" for r in rows), (
        f"a name@branch recall must touch ref_requests: {rows}"
    )


def test_drift_reader_fault_degrades_not_breaks(tmp_path, monkeypatch):
    rig = _rig(tmp_path)
    text = "fact that must survive a drift-cache fault"
    eid = _anchored(rig, text, "pkg/faulty.py::fn")
    drift_put(rig.store, [("alpha", TIP, "pkg/faulty.py::fn", DRIFT_FRESH, "{}", NOW)])

    def boom(*a, **kw):
        raise RuntimeError("drift cache exploded")

    monkeypatch.setattr(rig.store, "drift_get", boom, raising=False)
    env = recall(rig.server, text)
    hit = hit_for(env, eid)  # the read NEVER breaks
    drift = hit.get("drift") or {}
    assert drift.get("type") == DRIFT_UNVERIFIABLE, (
        f"a reader fault degrades that hit to unverifiable: {drift}"
    )


def test_every_hit_carries_the_three_fields(tmp_path):
    rig = _rig(tmp_path)
    a = _anchored(rig, "anchored fact one", "pkg/one.py::fn")
    g = write_ok(rig.server, "general fact two")
    for text, eid in (("anchored fact one", a), ("general fact two", g)):
        env = recall(rig.server, text)
        hit = hit_for(env, eid)
        for key in ("repos", "anchors", "drift"):
            assert key in hit, f"served hit lacks {key!r}: {hit}"
        assert json.dumps(hit["drift"])  # json-serializable wire object
