"""C2(poison) — the adversarial-overlay substrate (DATA layer). ``build_poison_substrate`` overlays
committed-servable falsehoods on the clean ``build_substrate`` pool; ``PoisonSpec``/``PoisonTaskCase``
are the frozen self-asserting carriers; ``_mutate_value`` carves a falsehood out of a real turn;
``_assert_not_telegraphed`` refuses self-announcing content; ``make_credential_text`` synthesizes a
secret-floor-refusable string. Fully offline: the real LongMemEval tiny fixture, no backend, no
model, no network. Backend planting/supersede/falsity_map are a separate chunk and not tested here."""
from __future__ import annotations

from pathlib import Path

import pytest

from hive.app.config import AutonomyConfig
from hive.research.bench.dataset import load_longmemeval
from hive.research.bench.hivemind_backend import HivemindBackend
from hive.research.bench.mem0_backend import Mem0Backend
from hive.research.bench.substrate import Preloaded, preload
from hive.research.bench.substrate import build_substrate
from hive.research.bench.task_agent import _content_tokens
from hive.research.bench.poison_substrate import (
    POISON_VERSION,
    PoisonSpec,
    PoisonTaskCase,
    _assert_not_telegraphed,
    _mutate_value,
    build_poison_substrate,
    drive_demand,
    falsity_map,
    make_credential_text,
    plant_falsehood_unvouched,
    plant_falsehood_vouched,
    supersede_falsehood,
)
from tests.fakes._fakes import FakeClusterProvider
from tests.mcp._helpers import build_real_server
from tests.research.bench.test_mem0_backend import FakeMem0Client

_FIX = Path(__file__).parent / "fixtures" / "longmemeval_tiny.json"


def _cases():
    return load_longmemeval(str(_FIX))


def _gold_sources(cases) -> set[str]:
    """Every gold-evidence session id across the corpus — the set a false source must avoid."""
    return {s for c in cases for s in c.question.evidence_session_ids}


# ── build_poison_substrate: plants a distinct non-gold falsehood ────────────────
def test_build_poison_substrate_plants_distinct_nongold_falsehood():
    cases = _cases()
    mems, poison_cases, specs = build_poison_substrate(
        cases, seed=0, poison_frac=1.0, kinds=("mistake", "stale"))
    assert specs, "poison_frac=1.0 over answerable cases must synthesize ≥1 falsehood"
    gold_all = _gold_sources(cases)
    pool_sources = {src for src, _txt in mems}
    for spec in specs:
        # the false source is DISTINCT and absent from every gold set (a competing hard-negative)
        assert spec.false_source_id not in gold_all
        assert spec.false_source_id != spec.gold_source_id
        # the values are token-disjoint and genuinely different
        assert spec.true_value != spec.false_value
        assert not (_content_tokens(spec.true_value) & _content_tokens(spec.false_value))
        # the false tuple is actually present in the augmented pool, carrying the mutated text
        assert (spec.false_source_id, spec.false_text) in mems
        assert spec.false_source_id in pool_sources
        # the mutation actually changed the turn: the false value is present, planted in real prose
        assert spec.false_value in spec.false_text
    corrected = [c for c in poison_cases if c.regime == "corrected"]
    assert corrected, "a poisoned answerable case must surface as a corrected PoisonTaskCase"
    for c in corrected:
        assert c.false_source_ids and not (c.false_source_ids & frozenset(gold_all))


def test_poison_version_is_stamped():
    assert POISON_VERSION == "poison.v1"


# ── PoisonSpec: token-disjoint values are enforced ──────────────────────────────
def test_poison_spec_enforces_token_disjoint_values():
    # a false_value sharing a content token with the true value is an unconstructable state
    with pytest.raises(ValueError, match="token-disjoint|share"):
        PoisonSpec(
            gold_source_id="s1", false_source_id="poison::s1",
            true_value="port 8080", false_value="port 9090",   # shares the token "port"
            false_text="the dev server runs on port 9090", kind="mistake")


def test_poison_spec_rejects_false_source_equal_to_gold():
    with pytest.raises(ValueError, match="DISTINCT|gold"):
        PoisonSpec(
            gold_source_id="s1", false_source_id="s1",
            true_value="8080", false_value="9191",
            false_text="the dev server runs on port 9191", kind="mistake")


def test_poison_spec_accepts_disjoint_values():
    spec = PoisonSpec(
        gold_source_id="s1", false_source_id="poison::s1",
        true_value="8080", false_value="9191",
        false_text="the dev server runs on port 9191", kind="mistake")
    assert spec.kind == "mistake"


# ── PoisonTaskCase: regime invariants ───────────────────────────────────────────
def test_poison_task_case_corrected_requires_false_source_and_disjoint_values():
    with pytest.raises(ValueError, match="false source"):
        PoisonTaskCase(
            query="q", regime="corrected", kind="mistake",
            gold_source_ids=frozenset({"s1"}), false_source_ids=frozenset(),
            true_value="8080", false_value="9191", expected="8080")


def test_poison_task_case_clean_forbids_false_source():
    with pytest.raises(ValueError, match="NO false source"):
        PoisonTaskCase(
            query="q", regime="clean", kind="mistake",
            gold_source_ids=frozenset({"s1"}), false_source_ids=frozenset({"poison::s1"}),
            true_value=None, false_value=None, expected="8080")


# ── _assert_not_telegraphed: refuses a tell or an echo of the true value ────────
def test_substrate_refuses_telegraphed_falsehood():
    # a tell word must raise
    with pytest.raises(ValueError, match="telegraph"):
        _assert_not_telegraphed("this is the WRONG port 9090", true_value="8080")
    # an echo of the true value must raise (the poison row would credit the true answer)
    with pytest.raises(ValueError, match="echo"):
        _assert_not_telegraphed("the port is 8080 and also 9090", true_value="8080")


def test_assert_not_telegraphed_passes_a_clean_falsehood():
    # a plain wrong-valued copy with neither a tell nor the true value must NOT raise
    _assert_not_telegraphed("the dev server runs on port 9090", true_value="8080")


# ── _mutate_value: substitute the value, keep the surrounding prose ─────────────
def test_mutate_value_substitutes_only_the_value_keeping_prose():
    turn = "the dev server runs on port 8080"
    out = _mutate_value(turn, "8080", "9090")
    assert out == "the dev server runs on port 9090"
    assert "9090" in out and "8080" not in out
    # surrounding prose is retained verbatim
    assert "the dev server runs on port" in out


def test_mutate_value_falls_back_when_value_absent_from_turn():
    # a gold turn that does not contain the answer verbatim still ends carrying the false value
    out = _mutate_value("we use postgres for the main store", "postgres on port 5432", "zarquon")
    assert "zarquon" in out
    assert "we use postgres for the main store" in out


# ── make_credential_text: a named-real-prefix secret the floor refuses ──────────
def test_make_credential_text_carries_a_named_real_prefix():
    txt = make_credential_text("staging")
    assert ("sk-proj-" in txt) or ("ghp_" in txt)
    # wrapped in plausible prose, not a bare token
    assert "staging" in txt
    assert len(txt) > len("sk-proj-")


def test_make_credential_text_is_deterministic():
    assert make_credential_text("prod") == make_credential_text("prod")


# ── off-is-inert: kinds=() yields a byte-identical pool to build_substrate ───────
def test_pool_is_byte_inert_when_kinds_empty():
    cases = _cases()
    base_mems, _base_cases = build_substrate(cases, seed=3)
    mems, poison_cases, specs = build_poison_substrate(cases, seed=3, kinds=())
    assert mems == base_mems                 # byte-identical pool — the overlay is fully inert
    assert specs == ()                       # no falsehood synthesized
    assert all(c.regime != "corrected" for c in poison_cases)   # nothing poisoned
    assert all(not c.false_source_ids for c in poison_cases)


# ── robustness: an unsynthesizable falsehood keeps its case clean, never crashes ─
def test_build_poison_substrate_falls_back_to_clean_on_unsynthesizable_falsehood(monkeypatch):
    # A falsehood that would lexically telegraph itself (or fail the token-disjoint guard) on a given
    # case must keep THAT case clean — never abort the whole build. Real oracle property at larger n:
    # a verbose gold answer whose tokens recur in the gold turn. Force every synthesis to raise and
    # assert the build survives with all cases fallen back to clean.
    import hive.research.bench.poison_substrate as ps
    monkeypatch.setattr(ps, "_false_value_for", lambda true_value, seed_key: true_value)  # collides ⇒ raises
    mems, poison_cases, specs = ps.build_poison_substrate(_cases(), seed=0, poison_frac=1.0)
    assert specs == ()                                   # nothing synthesizable ⇒ no spec, no crash
    assert all(c.regime == "clean" for c in poison_cases)


# ── backend layer: planting / demand-promotion / supersede / falsity ────────────
# The data-layer tests above are model-free; the helpers below drive the REAL in-process
# server (a FakeClusterProvider for controllable geometry) and a FakeMem0Client, so the
# planting/supersede/demand mechanics are exercised end-to-end without a model or network.
#
# FakeClusterProvider geometry: a text carrying ``cid=<N>`` embeds onto cluster direction N
# (same cid ⇒ cosine≈1, different cids ⇒ orthogonal). A spec's ``false_text`` and the miss
# query share a cid so the falsehood is the demanded candidate; a servable competitor on a
# DIFFERENT cid stays below ``competitor_tau`` (no veto).

_CID_FALSE = 5            # the falsehood + its matching miss-query cluster
_CID_GOLD = 7             # a distinct cluster for a corrected/gold competitor

_DEMAND_AUTONOMY = AutonomyConfig(demand_m=2, demand_tau=0.5, competitor_tau=0.85)


def _spec(cid: int, *, false_value: str = "9191", true_value: str = "8080",
          kind: str = "mistake", gold_source_id: str = "s1") -> PoisonSpec:
    """A controllable falsehood whose text + scoring values sit on one cluster ``cid``."""
    return PoisonSpec(
        gold_source_id=gold_source_id, false_source_id=f"poison::{gold_source_id}",
        true_value=true_value, false_value=false_value,
        false_text=f"the dev server runs on port {false_value} cid={cid}", kind=kind)


def _hivemind_factory(*, h: float, autonomy=None):
    """A zero-arg server factory the HivemindBackend resets through, fixing the embedder +
    autonomy across every rebuild so the demand geometry is deterministic per arm."""
    def factory():
        server, _clock = build_real_server(
            d=64, h=h, embedder=FakeClusterProvider(d=64), autonomy=autonomy)
        return server
    return factory


def _hivemind_backend(*, h: float = 1.0, autonomy=None) -> HivemindBackend:
    return HivemindBackend(_hivemind_factory(h=h, autonomy=autonomy))


def _recall_ids(backend, seat: str, query: str) -> tuple[str, ...]:
    obs = backend.recall(seat, query)
    return () if obs.abstained else obs.ranked_ids


def _servable_under_writer(backend, *, writer_seat: str, query: str) -> bool:
    """Probe servability under the WRITER's own seat so the verifying recall never injects a
    DISTINCT-identity miss (which would itself supply demand and falsely promote). Demand must come
    ONLY from ``drive_demand``'s ``miss_seat`` — that is the variable RULE-2 fault #3 perturbs."""
    return bool(_recall_ids(backend, writer_seat, query))


# ── plant_falsehood_vouched: servable in BOTH backends (selection, not admission) ──
def test_plant_falsehood_vouched_makes_it_servable_in_both_backends():
    spec = _spec(_CID_FALSE)
    query = f"what port does the dev server use cid={_CID_FALSE}"

    # Hivemind abstain-off (h=1.0): the orchestrator-vouched commit ESTABLISHES it servable.
    hm = _hivemind_backend(h=1.0)
    planted = plant_falsehood_vouched(hm, (spec,), approver="orchestrator")
    assert spec.false_source_id in planted and planted[spec.false_source_id]
    assert planted[spec.false_source_id] in _recall_ids(hm, "reader", query)

    # mem0 (trust-everything): the same propose→commit seam ADDs it to the store.
    m0 = Mem0Backend(FakeMem0Client())
    planted0 = plant_falsehood_vouched(m0, (spec,), approver="orchestrator")
    assert spec.false_source_id in planted0 and planted0[spec.false_source_id]
    # mem0's Jaccard search keys on shared tokens, not cid; query the false text itself.
    assert planted0[spec.false_source_id] in _recall_ids(m0, "reader", spec.false_text)


# ── POSITIVE control (asserted FIRST): two distinct seats promote via real demand ──
def test_two_distinct_seats_can_promote_via_real_demand_path():
    spec = _spec(_CID_FALSE)
    miss_q = f"what port does the dev server use cid={_CID_FALSE}"
    hm = _hivemind_backend(h=1.0, autonomy=_DEMAND_AUTONOMY)

    # plant the falsehood unvouched: a quarantined capture under ONE subagent seat.
    proposals = plant_falsehood_unvouched(hm, (spec,), writer_seat="sub-a")
    assert proposals.get(spec.false_source_id)              # quarantined handle returned

    # drive demand from a DISTINCT seat: n_other ≥ 1, matched ≥ demand_m ⇒ promotion. (No
    # pre-check recall — a distinct-identity probe would itself inject the demand we are testing.)
    drive_demand(hm, miss_query=miss_q, miss_seat="sub-b", n=2)

    # the falsehood is now servable — the demand mechanism is genuinely non-vacuous. Probe under
    # the WRITER seat so the verification never supplies the distinct identity itself.
    assert _servable_under_writer(hm, writer_seat="sub-a", query=miss_q), \
        "two distinct seats must promote the capture"


# ── NEGATIVE control (asserted AFTER the positive): one seat cannot self-promote ──
def test_single_seat_cannot_self_promote():
    spec = _spec(_CID_FALSE)
    miss_q = f"what port does the dev server use cid={_CID_FALSE}"
    hm = _hivemind_backend(h=1.0, autonomy=_DEMAND_AUTONOMY)

    plant_falsehood_unvouched(hm, (spec,), writer_seat="sub-a")
    # SAME-seat demand: n_other == 0 ⇒ self_demand ⇒ the anti-gaming floor refuses.
    drive_demand(hm, miss_query=miss_q, miss_seat="sub-a", n=2)

    # probe under the writer seat too ⇒ no probe ever supplies a distinct identity; promotion can
    # only come from drive_demand, which here is self-demand ⇒ never servable.
    assert not _servable_under_writer(hm, writer_seat="sub-a", query=miss_q), \
        "a single seat must not self-promote"


# ── supersede de-indexes the false fact and serves the fresh gold ──────────────
def test_supersede_deindexes_the_false_fact_and_serves_the_fresh_gold():
    spec = _spec(_CID_GOLD, false_value="9191", true_value="8080")
    anchor = f"dev server port cid={_CID_GOLD}"
    corrected_text = f"the dev server runs on port {spec.true_value} cid={_CID_GOLD}"

    # Hivemind: plant servable, then human-vouched supersede.
    hm = _hivemind_backend(h=1.0)
    pre = Preloaded(mem_source={}, mem_text={})
    planted = plant_falsehood_vouched(hm, (spec,), approver="orchestrator")
    false_mid = planted[spec.false_source_id]
    pre.mem_source[false_mid] = spec.false_source_id
    pre.mem_text[false_mid] = spec.false_text
    assert false_mid in _recall_ids(hm, "reader", anchor)        # false is servable first

    n = supersede_falsehood(hm, (spec,), pre, approver="orchestrator",
                            gold_text_for={spec.false_source_id: corrected_text})
    assert n == 1
    served = _recall_ids(hm, "reader", anchor)
    assert false_mid not in served                               # de-indexed
    # the fresh row is served and now maps to the GOLD source for served_gold to fire.
    fresh = [m for m in served if pre.mem_source.get(m) == spec.gold_source_id]
    assert fresh and pre.mem_text[fresh[0]] == corrected_text

    # mem0 has no supersede ⇒ getattr-probe no-ops; both rows stay servable.
    m0 = Mem0Backend(FakeMem0Client())
    pre0 = Preloaded(mem_source={}, mem_text={})
    planted0 = plant_falsehood_vouched(m0, (spec,), approver="orchestrator")
    pre0.mem_source[planted0[spec.false_source_id]] = spec.false_source_id
    pre0.mem_text[planted0[spec.false_source_id]] = spec.false_text
    n0 = supersede_falsehood(m0, (spec,), pre0, approver="orchestrator",
                             gold_text_for={spec.false_source_id: corrected_text})
    assert n0 == 0
    assert planted0[spec.false_source_id] in _recall_ids(m0, "reader", spec.false_text)


# ── falsity_map skips a refused credential write, but mem0 stores it ────────────
def test_falsity_map_skips_refused_credential_writes():
    cred_spec = PoisonSpec(
        gold_source_id="s9", false_source_id="poison::s9",
        true_value="", false_value="", kind="credential",
        false_text=make_credential_text("staging"))

    # Hivemind: the shipped secret floor REFUSES the credential at write ⇒ no mem_id, so it
    # contributes no key to falsity_map (a refused entry never leaks an empty handle).
    hm = _hivemind_backend(h=1.0)
    pre = preload(hm, [(cred_spec.false_source_id, cred_spec.false_text)],
                  approver="orchestrator")
    assert falsity_map(pre, (cred_spec,)) == frozenset()
    assert cred_spec.false_source_id not in pre.mem_source.values()

    # mem0 (trust-everything): the credential IS stored, so its mem_id IS in falsity_map.
    m0 = Mem0Backend(FakeMem0Client())
    pre0 = preload(m0, [(cred_spec.false_source_id, cred_spec.false_text)],
                   approver="orchestrator")
    fm0 = falsity_map(pre0, (cred_spec,))
    assert len(fm0) == 1 and next(iter(fm0)) in pre0.mem_source
