"""The L4 empirical-gate arm pair: ``verified-outcome`` (E — captures + SHA-bound verified
evidence + ``verified_promotion=ON``) vs ``l4-off`` (C′ — identical plants + evidence, flag OFF).

The pair isolates the verified-win promotion rung end-to-end: the pool (gold AND poison) is
planted as CAPTURES only (quarantined, unservable), ``outcome_verified_helped`` rows are injected
on gold-source captures and ``outcome_verified_hurt`` on poison-source captures via the store's
``append_evidence`` seam (the bench plays the census-ingest role), demand is driven, and ONLY the
E arm may promote — and only the gold. The gate's PASS (``clean_win``) is success-led: the E−C′
success delta improves (CI lo > 0) AND the false-serve delta does not worsen (CI lo ≤ 0).

Offline: the REAL in-process server (FakeProvider embedder — exact-text queries give cosine 1.0,
so the demand drive deterministically reaches the rung), zero API, zero model download.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hive.app.config import AutonomyConfig
from hive.domain.evidence_kinds import (
    EK_OUTCOME_VERIFIED_HELPED, EK_OUTCOME_VERIFIED_HURT,
)
from hive.research.bench.hivemind_backend import HivemindBackend
from hive.research.bench.llm import FakeLLM
from hive.research.bench.poison_agent import PoisonArmObs, PoisonTaskObs
from hive.research.bench.poison_run import (
    ARM_L4_OFF, ARM_L4_ON, _REQUIRED_PROVENANCE_L4, inject_verified_evidence,
    main, score_l4_arms,
)
from hive.research.bench.poison_substrate import PoisonTaskCase, preload_captures
from tests.mcp._helpers import build_real_server

_FIX = Path(__file__).parent / "fixtures" / "longmemeval_tiny.json"

_GOLD_TEXT = "the deploy port for the billing service is 7443"
_POISON_TEXT = "the deploy port for the billing service is 9999"
_MEMORIES = [("gold-s", _GOLD_TEXT), ("poison::gold-s", _POISON_TEXT),
             ("hay-s", "the cafeteria menu rotates weekly")]
_CASES = [PoisonTaskCase(
    query=_GOLD_TEXT,                      # exact-text query ⇒ cosine 1.0 under any embedder
    regime="corrected", kind="mistake",
    gold_source_ids=frozenset({"gold-s"}),
    false_source_ids=frozenset({"poison::gold-s"}),
    true_value="7443", false_value="9999", expected="7443")]

_STAMP = {"base_sha": "bench-l4-seed-0", "head_sha": "d" * 8,
          "combdrift": {"engine": "bench-l4/poison.v1"},
          "matrix_head": {"graph_sha256": "d" * 8, "commit_sha": "bench-l4-seed-0",
                          "engine_version": "bench-l4/poison.v1"}}


def _l4_env(flag: bool):
    """A real in-process server (FakeProvider embedder) in the L4 posture: the demand rule
    inert (demand_m huge) so the verified rung is the ONLY mechanical path out of quarantine,
    ``verified_promotion`` per arm. Returns (backend, store) — the store handle is the
    evidence-injection seam."""
    server, _clock = build_real_server(
        autonomy=AutonomyConfig(demand_m=10**9, verified_promotion=flag))
    return HivemindBackend(lambda: server), server.store


def _plant_and_inject(flag: bool):
    backend, store = _l4_env(flag)
    pre = preload_captures(backend, _MEMORIES)
    counts = inject_verified_evidence(store, pre, _CASES, ts=1000, stamp=_STAMP,
                                      receipt_sha256="cafe" * 4)
    return backend, store, pre, counts


# ── preload_captures: propose-only, quarantined, source-mapped ──────────────────
def test_preload_captures_lands_quarantined_and_maps_sources():
    backend, _store = _l4_env(False)
    pre = preload_captures(backend, _MEMORIES)
    assert set(pre.mem_source.values()) == {"gold-s", "poison::gold-s", "hay-s"}
    assert set(pre.mem_text) == set(pre.mem_source)
    # captures are structurally unservable: even an exact-text recall abstains
    assert backend.recall("reader", _GOLD_TEXT).abstained is True
    assert backend.recall("reader", _POISON_TEXT).abstained is True


# ── inject_verified_evidence: helped on gold, hurt on poison, stamped, idempotent ──
def test_inject_marks_gold_helped_and_poison_hurt_with_stamped_payloads():
    _backend, store, pre, counts = _plant_and_inject(False)
    assert counts["helped_rows"] == 1 and counts["hurt_rows"] == 1
    assert counts["inserted"] == 2 and counts["skipped"] == 0
    by_source = {src: int(mid) for mid, src in pre.mem_source.items()}
    assert store.verified_wins([by_source["gold-s"]]) == {by_source["gold-s"]}
    assert store.verified_wins([by_source["poison::gold-s"]]) == set()   # hurt is never a win
    assert store.verified_wins([by_source["hay-s"]]) == set()            # neutral: no evidence
    # payloads are the real ingest's rendered shape, carrying the full version stamp (L7)
    rows = store.conn.execute(
        "SELECT kind, payload FROM evidence_events WHERE kind IN (?, ?)",
        (EK_OUTCOME_VERIFIED_HELPED, EK_OUTCOME_VERIFIED_HURT)).fetchall()
    assert {r["kind"] for r in rows} == {EK_OUTCOME_VERIFIED_HELPED, EK_OUTCOME_VERIFIED_HURT}
    for r in rows:
        p = json.loads(r["payload"])
        assert p["schema"] == "outcome_verified/v1"
        assert p["stamp"] == _STAMP
        assert p["reason"] == ("corroborated" if r["kind"] == EK_OUTCOME_VERIFIED_HELPED
                               else "contradicted")


def test_inject_is_idempotent_on_reinjection():
    _backend, store, pre, first = _plant_and_inject(False)
    again = inject_verified_evidence(store, pre, _CASES, ts=2000, stamp=_STAMP,
                                     receipt_sha256="cafe" * 4)
    assert again["inserted"] == 0
    assert again["skipped"] == first["inserted"] == 2


def test_inject_refuses_a_gold_false_source_overlap():
    backend, store = _l4_env(False)
    pre = preload_captures(backend, _MEMORIES)
    bad = [PoisonTaskCase(query="q", regime="corrected", kind="mistake",
                          gold_source_ids=frozenset({"gold-s"}),
                          false_source_ids=frozenset({"poison::gold-s"}),
                          true_value="7443", false_value="9999", expected="7443"),
           PoisonTaskCase(query="q2", regime="clean", kind="mistake",
                          gold_source_ids=frozenset({"poison::gold-s"}),
                          false_source_ids=frozenset(), true_value=None,
                          false_value=None, expected=None)]
    with pytest.raises(ValueError, match="overlap"):
        inject_verified_evidence(store, pre, bad, ts=1000, stamp=_STAMP,
                                 receipt_sha256="cafe" * 4)


# ── the rung end-to-end: E promotes verified gold ONLY; C′ promotes nothing ─────
def test_l4_on_promotes_and_serves_verified_gold_never_poison():
    backend, store, pre, _counts = _plant_and_inject(True)
    from hive.research.bench.poison_run import drive_l4_demand
    drive_l4_demand(backend, pre, _CASES)
    by_source = {src: mid for mid, src in pre.mem_source.items()}
    # gold promoted out of quarantine; poison and the neutral haystack row stayed
    assert store.get_episode(int(by_source["gold-s"])).trust == "provisional"
    assert store.get_episode(int(by_source["poison::gold-s"])).trust == "quarantined"
    assert store.get_episode(int(by_source["hay-s"])).trust == "quarantined"
    obs = backend.recall("reader", _GOLD_TEXT)
    assert obs.abstained is False and by_source["gold-s"] in obs.ranked_ids
    assert by_source["poison::gold-s"] not in obs.ranked_ids


def test_l4_off_promotes_nothing_and_serves_nothing():
    backend, store, pre, _counts = _plant_and_inject(False)
    from hive.research.bench.poison_run import drive_l4_demand
    drive_l4_demand(backend, pre, _CASES)
    assert store.trust_counts()["provisional"] == 0
    assert backend.recall("reader", _GOLD_TEXT).abstained is True
    assert backend.recall("reader", _POISON_TEXT).abstained is True


# ── clean_win semantics: success-led with the FSR non-regression clause ─────────
def _task(query: str, *, success: bool, false_serve: bool) -> PoisonTaskObs:
    # success ⇒ the gold was served AND the true value emitted (the derived ``success``
    # property's conjunction); false_serve ⇒ a poison row was ALSO served — the reply stays
    # clean (emitted_false False), so the two axes vary independently, exactly the grid the
    # clean_win conjunction discriminates.
    served = (1 if success else 0) + (1 if false_serve else 0)
    return PoisonTaskObs(
        query=query, regime="corrected", kind="mistake", answerable=True,
        served_text_count=served, served_false_count=1 if false_serve else 0,
        served_gold=success, false_served=false_serve,
        emitted_false=False, emitted_true=success,
        abstained=(served == 0), refused=False)


def _pair(e_tasks, c_tasks):
    return {ARM_L4_ON: PoisonArmObs(arm=ARM_L4_ON, tasks=tuple(e_tasks)),
            ARM_L4_OFF: PoisonArmObs(arm=ARM_L4_OFF, tasks=tuple(c_tasks))}


def test_l4_clean_win_success_up_fsr_flat():
    e = [_task(f"q{i}", success=True, false_serve=False) for i in range(6)]
    c = [_task(f"q{i}", success=False, false_serve=False) for i in range(6)]
    d = score_l4_arms(_pair(e, c), seed=0)["deltas"]["E-Cprime"]
    assert d["success"]["improves"] is True and d["false_serve"]["worsens"] is False
    assert d["clean_win"] is True


def test_l4_clean_win_refused_when_fsr_worsens():
    # success up AND false-serve up ⇒ the conjunction refuses the win
    e = [_task(f"q{i}", success=True, false_serve=True) for i in range(6)]
    c = [_task(f"q{i}", success=False, false_serve=False) for i in range(6)]
    d = score_l4_arms(_pair(e, c), seed=0)["deltas"]["E-Cprime"]
    assert d["success"]["improves"] is True and d["false_serve"]["worsens"] is True
    assert d["clean_win"] is False


def test_l4_clean_win_refused_without_a_success_improvement():
    # identical arms ⇒ zero delta ⇒ no improvement ⇒ no win (a flat E is NOT a pass)
    e = [_task(f"q{i}", success=False, false_serve=False) for i in range(6)]
    d = score_l4_arms(_pair(e, list(e)), seed=0)["deltas"]["E-Cprime"]
    assert d["success"]["improves"] is False
    assert d["clean_win"] is False


def test_score_l4_arms_requires_aligned_nonempty_corrected_slice():
    e = [_task("qa", success=True, false_serve=False)]
    c = [_task("qb", success=False, false_serve=False)]
    with pytest.raises(ValueError, match="task-aligned"):
        score_l4_arms(_pair(e, c), seed=0)
    with pytest.raises(ValueError, match="corrected-answerable"):
        score_l4_arms({ARM_L4_ON: PoisonArmObs(arm=ARM_L4_ON, tasks=()),
                       ARM_L4_OFF: PoisonArmObs(arm=ARM_L4_OFF, tasks=())}, seed=0)


# ── main --l4 offline: report shape, provenance, and the C′ byte-inertness pin ───
def _offline_env_factory(arm: str):
    return _l4_env(arm == ARM_L4_ON)


def test_main_l4_offline_writes_a_provenance_stamped_gate_report(tmp_path):
    out = tmp_path / "l4.json"
    rc = main(["--l4", "--dataset", str(_FIX), "--out", str(out)],
              l4_env_factory=_offline_env_factory,
              llm_factory=lambda arm: FakeLLM())
    assert rc == 0 and out.exists()
    rep = json.loads(out.read_text())
    assert set(rep["arms"]) == {ARM_L4_OFF, ARM_L4_ON}
    for arm in rep["arms"].values():
        assert "false_serve_rate" in arm and "success_rate" in arm and "by_regime" in arm
    assert set(rep["deltas"]) == {"E-Cprime"}
    d = rep["deltas"]["E-Cprime"]
    assert set(d) == {"false_serve", "success", "clean_win"}
    assert d["false_serve"]["lo"] <= d["false_serve"]["hi"]
    assert d["success"]["lo"] <= d["success"]["hi"]
    assert isinstance(d["clean_win"], bool)
    prov = rep["provenance"]
    for key in _REQUIRED_PROVENANCE_L4:
        assert prov.get(key) not in (None, ""), f"missing provenance {key!r}"
    # the pairing invariants, pinned in provenance: the flag is the ONLY difference —
    # C′ promoted NOTHING; E promoted at least the verified gold (the drive is exact-text,
    # so the rung is deterministically reached even under the hash embedder).
    assert prov["promoted"][ARM_L4_OFF] == 0
    assert prov["promoted"][ARM_L4_ON] >= 1
    for arm in (ARM_L4_OFF, ARM_L4_ON):
        assert prov["evidence_injected"][arm]["helped_rows"] >= 1
        assert prov["evidence_injected"][arm]["hurt_rows"] >= 1
    assert prov["autonomy"]["demand_m"] == 10**9
    assert prov["verified_stamp"]["combdrift"] and prov["verified_stamp"]["matrix_head"]


def test_main_l4_raises_when_the_off_arm_promotes(tmp_path):
    # an env factory that ignores the flag (both arms ON) breaks the pairing — the runner
    # must REFUSE (a C′ that promotes is not a control), never emit a report.
    out = tmp_path / "l4.json"
    with pytest.raises(ValueError, match="l4-off"):
        main(["--l4", "--dataset", str(_FIX), "--out", str(out)],
             l4_env_factory=lambda arm: _l4_env(True),
             llm_factory=lambda arm: FakeLLM())
    assert not out.exists()
