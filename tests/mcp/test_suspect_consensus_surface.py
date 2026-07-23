"""The suspect-consensus worklist via hive_health(include_suspect_consensus=true):
detection-only surfacing of PROVISIONAL promotions whose stamped demand had thin
effective independence. Default OFF / byte-inert; double-gated (config + request flag);
ids-only fields (Law 4); fail-open to []. A provisional is seeded by writing a memory,
flipping it to provisional trust, and stamping a promote audit (the 3b signal), per the
plan's direct-audit seeding option."""

from __future__ import annotations

import json

from hive.app.config import SuspectConsensusConfig
from tests.mcp._helpers import build_real_server, content, tool_call, write_text

D = 64


def _srv(*, n_eff_frac_max=0.5, top_n=10):
    sc = SuspectConsensusConfig(n_eff_frac_max=n_eff_frac_max, top_n=top_n)
    return build_real_server(d=D, suspect_consensus=sc)


def _seed_provisional(server, clock, text, *, rho_bar, n_eff, k):
    """Write a memory (v3: lands provisional directly) and stamp a promote audit
    carrying demand_independence — a servable provisional with known thin/independent
    demand, exactly the row the worklist reads."""
    eid = write_text(server, text)["id"]
    assert server.store.get_episode(eid).trust == "provisional"
    server.store.insert_audit(
        eid,
        "promote",
        "server",
        int(clock.now()),
        json.dumps(
            {
                "rule": "demand",
                "n_misses": k,
                "demand_independence": {"rho_bar": rho_bar, "n_eff": n_eff, "k": k},
            }
        ),
    )
    return eid


# ── byte-inert when off / unrequested (golden) ─────────────────────────────────
def test_absent_unless_requested():
    server, clock = _srv()
    _seed_provisional(server, clock, "thin demand promo", rho_bar=0.95, n_eff=1.0, k=4)
    snap = content(tool_call(server, "hive_health", {}))  # no flag
    assert "suspect_consensus" not in snap


def test_bare_server_surfaces_worklist_when_requested():
    # no suspect_consensus config passed ⇒ the default SuspectConsensusConfig (always-on, no
    # enabled toggle). The request flag is the SOLE gate, so the worklist surfaces when requested
    # — no config opt-in needed (the toggle was removed; symmetric with the conflict worklist).
    server, clock = build_real_server(d=D)
    eid = _seed_provisional(
        server, clock, "thin demand promo", rho_bar=0.95, n_eff=1.0, k=4
    )
    snap = content(
        tool_call(server, "hive_health", {"include_suspect_consensus": True})
    )
    assert "suspect_consensus" in snap
    assert snap["suspect_consensus"][0]["episode_id"] == eid


# ── the worklist surfaces a thin provisional ───────────────────────────────────
def test_surfaces_thin_provisional_ids_only():
    server, clock = _srv()
    eid = _seed_provisional(
        server, clock, "popular but correlated insight", rho_bar=0.95, n_eff=1.0, k=4
    )
    snap = content(
        tool_call(server, "hive_health", {"include_suspect_consensus": True})
    )
    assert "suspect_consensus" in snap
    work = snap["suspect_consensus"]
    assert len(work) == 1
    row = work[0]
    assert row["episode_id"] == eid
    assert row["n_eff"] == 1.0 and row["k"] == 4
    assert row["martingale_warning"] is True  # thin AND no settled win (always False)
    # ids + numbers + a prose note ONLY — no memory text rides it (Law 4)
    assert set(row) == {
        "episode_id",
        "rho_bar",
        "n_eff",
        "k",
        "martingale_warning",
        "note",
    }
    assert "popular but correlated insight" not in json.dumps(snap)


# ── the martingale clause: a self-reported settled win clears the warning ───────
def test_thin_with_settled_win_clears_martingale_warning():
    # a thin provisional is STILL surfaced (it is thin), but a recorded outcome_helped
    # corroborates it ⇒ martingale_warning=False (thin AND no settled win is the sharper signal).
    server, clock = _srv()
    eid = _seed_provisional(
        server, clock, "thin but corroborated", rho_bar=0.95, n_eff=1.0, k=4
    )
    tool_call(
        server, "hive_outcome", {"helped": [eid]}
    )  # the self-reported settled win
    snap = content(
        tool_call(server, "hive_health", {"include_suspect_consensus": True})
    )
    work = snap["suspect_consensus"]
    assert len(work) == 1 and work[0]["episode_id"] == eid
    assert work[0]["martingale_warning"] is False  # the win flipped the clause off


def test_thin_without_settled_win_keeps_warning():
    server, clock = _srv()
    eid = _seed_provisional(
        server, clock, "thin uncorroborated", rho_bar=0.95, n_eff=1.0, k=4
    )
    snap = content(
        tool_call(server, "hive_health", {"include_suspect_consensus": True})
    )
    assert snap["suspect_consensus"][0]["episode_id"] == eid
    assert (
        snap["suspect_consensus"][0]["martingale_warning"] is True
    )  # no win ⇒ still warned


def test_fully_independent_provisional_not_flagged():
    server, clock = _srv()
    _seed_provisional(
        server, clock, "well-corroborated insight", rho_bar=0.0, n_eff=4.0, k=4
    )  # n_eff/k = 1.0, not thin
    snap = content(
        tool_call(server, "hive_health", {"include_suspect_consensus": True})
    )
    assert snap["suspect_consensus"] == []


def test_established_promotion_not_in_worklist():
    # the worklist runs on PROVISIONAL only — an established (outcome-verified) memory
    # with a stamped audit is never surfaced (it left the demand-promoted population).
    server, clock = _srv()
    eid = write_text(server, "an outcome-verified fact")["id"]
    assert server.store.set_trust(eid, "established", now=int(clock.now()))
    server.store.insert_audit(
        eid,
        "promote",
        "server",
        int(clock.now()),
        json.dumps(
            {
                "rule": "demand",
                "n_misses": 4,
                "demand_independence": {"rho_bar": 0.95, "n_eff": 1.0, "k": 4},
            }
        ),
    )
    snap = content(
        tool_call(server, "hive_health", {"include_suspect_consensus": True})
    )
    assert snap["suspect_consensus"] == []


# ── fail-open: a provenance fault degrades to [] (no half-shape, no 500) ────────
def test_fail_open_returns_empty_not_500():
    server, clock = _srv()
    _seed_provisional(server, clock, "thin one", rho_bar=0.95, n_eff=1.0, k=4)

    def _boom(*a, **k):
        raise RuntimeError("provenance exploded")

    server.store.promotion_provenance = _boom
    snap = content(
        tool_call(server, "hive_health", {"include_suspect_consensus": True})
    )
    assert snap["ok"] is True  # health did NOT 500
    assert snap["suspect_consensus"] == []  # degraded to [], not a half-shape
