"""P1.5 — M04 RecallPipeline: the never-hallucinate enforcement point.

Full functional coverage against fakes only (hash-speed, no SQLite/network):
happy path, every failure mode (embedder raise / index raise / empty /
non-authoritative), every invariant (abstain⟹empty, abstain-no-resurrect
STRUCTURAL, EMPTY vs ABSTAIN distinct, top_cos∈[-1,1], unique trace, top_n
size-only, approved-only honest half), and the swap-seam (a 2nd index adapter ⟹
identical result). The abstain decision is now the pure AbsoluteRelevanceGate
(serve iff a candidate clears tau_serve); a flat-but-relevant field serves, a
peaked-but-weak field abstains.
"""
from __future__ import annotations

import numpy as np
import pytest

from hive.domain.models import ABSTAIN, CONFIDENT, EMPTY_NO_DATA
from hive.domain.recall import AbsoluteRelevanceGate, RecallPipeline
from tests.fakes._fakes import (
    FakeEpisodeReader, FakeIndex, FakeLedger, FakeScanner,
)

D = 16


# ── vector helpers (controlled cosines to e0, the query direction) ────────────
def _e(i: int, d: int = D) -> np.ndarray:
    v = np.zeros(d, dtype=np.float32)
    v[i] = 1.0
    return v


def _cos_vec(c: float, d: int = D) -> np.ndarray:
    """A unit vector whose cosine with e0 is exactly ``c``."""
    v = np.zeros(d, dtype=np.float32)
    v[0] = c
    v[1] = float(np.sqrt(max(0.0, 1.0 - c * c)))
    return v


class _StubProvider:
    """Returns a FIXED query vector for any text (decouples from hash randomness)."""
    def __init__(self, vec: np.ndarray, d: int = D) -> None:
        self._vec = np.asarray(vec, dtype=np.float32)
        self.d = d

    def encode(self, text: str) -> np.ndarray:
        return self._vec

    def encode_batch(self, texts):
        return np.stack([self._vec for _ in texts])


class _RaisingProvider(_StubProvider):
    def encode(self, text):
        raise RuntimeError("embedder boom")


class _NonAuthIndex(FakeIndex):
    def is_authoritative(self) -> bool:
        return False


class _RaisingSearchIndex(FakeIndex):
    def search(self, query, k):
        raise RuntimeError("search boom")


class _ListIndex:
    """Alternate adapter — list-backed instead of dict, same Protocol (swap seam)."""
    def __init__(self) -> None:
        self._rows: list[tuple[int, np.ndarray]] = []

    def add(self, episode_id: int, value: np.ndarray) -> None:
        self._rows.append((int(episode_id), np.asarray(value, dtype=np.float32)))

    def search(self, query, k):
        q = np.asarray(query, dtype=np.float32)
        scored = [(eid, float(np.dot(q, v))) for eid, v in self._rows]
        scored.sort(key=lambda t: t[1], reverse=True)
        return scored[:k]

    def is_authoritative(self) -> bool:
        return True

    def size(self) -> int:
        return len(self._rows)


def _pipe(*, index, reader, query_vec,
          recall_top_n=10, tau_serve=0.70, k_min=1, embedder=None, ledger=None,
          overscan=3, select=True, dup_tau=0.80, conflict_enabled=False):
    return RecallPipeline(
        embedder=embedder or _StubProvider(query_vec),
        index=index,
        gate=AbsoluteRelevanceGate(tau_serve, k_min),
        reader=reader,
        recall_top_n=recall_top_n,
        ledger=ledger if ledger is not None else FakeLedger(),
        clock_now=lambda: 0,
        scanner=FakeScanner(),
        provisional_ttl_s=10**9,        # effectively fresh-forever for these tests
        overscan=overscan,
        select=select,
        dup_tau=dup_tau,
        conflict_enabled=conflict_enabled,
    )


def _confident_setup(top_n=10):
    """gold(cos1) + two weak distractors ⟹ the gold clears tau_serve ⟹ CONFIDENT."""
    index, reader = FakeIndex(), FakeEpisodeReader()
    for eid, vec, text in ((1, _e(0), "the gold memory"),
                           (2, _cos_vec(0.1), "distractor a"),
                           (3, _cos_vec(0.05), "distractor b")):
        index.add(eid, vec)
        reader.add(eid, text, weight=1.0)
    return _pipe(index=index, reader=reader, query_vec=_e(0), recall_top_n=top_n)


# ── happy path ────────────────────────────────────────────────────────────────
def test_happy_path_returns_confident_hits():
    r = _confident_setup().recall("q", agent_id="A")
    assert r.state == CONFIDENT
    assert r.hits[0].episode_id == 1 and r.hits[0].text == "the gold memory"
    assert r.top_cos == pytest.approx(1.0)     # the gold's absolute cosine


def test_confident_hit_carries_kind_and_anchor():
    # the resolved episode's carried labels ride every hit (kind/anchor are read off
    # the resolved Episode, parallel to trust/ts/polarity).
    index, reader = FakeIndex(), FakeEpisodeReader()
    reader.add(1, "the gold memory", weight=1.0, kind="bug",
               anchor="hive/domain/recall.py")
    reader.add(2, "distractor a", weight=1.0)            # no labels ⇒ under-claims
    index.add(1, _e(0))
    index.add(2, _cos_vec(0.1))
    index.add(3, _cos_vec(0.05))
    reader.add(3, "distractor b", weight=1.0)
    r = _pipe(index=index, reader=reader, query_vec=_e(0)).recall("q", agent_id="A")
    assert r.state == CONFIDENT
    by_id = {h.episode_id: h for h in r.hits}
    assert by_id[1].kind == "bug" and by_id[1].anchor == "hive/domain/recall.py"
    assert by_id[2].kind == "note" and by_id[2].anchor == ""   # fail-safe defaults


def test_confident_hit_carries_provenance():
    # the resolved episode's provenance (its ORIGIN) rides every served hit, parallel to
    # kind/anchor; an unwired episode under-claims `agent_reasoned`, never `human`.
    index, reader = FakeIndex(), FakeEpisodeReader()
    reader.add(1, "the human-vouched memory", weight=1.0, provenance="human")
    reader.add(2, "an agent-reasoned distractor", weight=1.0)   # default ⇒ agent_reasoned
    index.add(1, _e(0))
    index.add(2, _cos_vec(0.1))
    r = _pipe(index=index, reader=reader, query_vec=_e(0)).recall("q", agent_id="A")
    assert r.state == CONFIDENT
    by_id = {h.episode_id: h for h in r.hits}
    assert by_id[1].provenance == "human"
    assert by_id[2].provenance == "agent_reasoned"             # fail-safe default


def test_recall_at_5_over_held_out_pairs_meets_floor():
    # 6 planted golds; each query points exactly at one gold ⟹ that gold is top (cos 1).
    index, reader = FakeIndex(), FakeEpisodeReader()
    golds = [(_e(i), f"gold-{i}") for i in range(6)]
    for eid, (vec, text) in enumerate(golds):
        index.add(eid, vec)
        reader.add(eid, text)
    hit_at5 = 0
    for eid, (vec, _text) in enumerate(golds):
        r = _pipe(index=index, reader=reader, query_vec=vec).recall(
            "q", agent_id="A")
        if r.state == CONFIDENT and eid in {h.episode_id for h in r.hits[:5]}:
            hit_at5 += 1
    assert hit_at5 / len(golds) >= 0.33   # recall@5 floor


# ── abstain ───────────────────────────────────────────────────────────────────
def test_abstain_returns_empty_hits():
    # 4 candidates ALL at cosine 0 with the query ⟹ none clear tau_serve ⟹ abstain
    index, reader = FakeIndex(), FakeEpisodeReader()
    for eid in (1, 2, 3, 4):
        index.add(eid, _cos_vec(0.0))   # all cos 0 with query e0
        reader.add(eid, f"t{eid}")
    r = _pipe(index=index, reader=reader, query_vec=_e(0)).recall(
        "q", agent_id="A")
    assert r.state == ABSTAIN and r.hits == ()


def test_abstain_no_resurrect():
    # ★ suppress ⟹ hits empty: the suppress branch returns before resolve, and a
    # non-CONFIDENT RecallResult carrying hits is structurally unconstructable — no
    # fallback path can repopulate a refused query.
    index, reader = FakeIndex(), FakeEpisodeReader()
    for eid in (1, 2, 3, 4):
        index.add(eid, _cos_vec(0.0))
        reader.add(eid, f"t{eid}")
    r = _pipe(index=index, reader=reader, query_vec=_e(0)).recall(
        "q", agent_id="A")
    assert r.state == ABSTAIN and r.hits == ()


def test_peaked_but_weak_field_abstains():
    # ★ the new-honesty pin end-to-end: a single peaked-but-absolutely-weak candidate
    # (cos 0.5 < tau_serve) ABSTAINS — the old entropy gate served it (n_eff=1 ⇒ entropy 0).
    index, reader = FakeIndex(), FakeEpisodeReader()
    index.add(1, _cos_vec(0.5))
    reader.add(1, "a weak near-miss")
    r = _pipe(index=index, reader=reader, query_vec=_e(0)).recall("q", agent_id="A")
    assert r.state == ABSTAIN and r.hits == ()


def test_empty_index_is_empty_no_data():
    r = _pipe(index=FakeIndex(), reader=FakeEpisodeReader(), query_vec=_e(0)).recall(
        "q", agent_id="A")
    assert r.state == EMPTY_NO_DATA and r.hits == () and r.top_cos == 0.0


def test_empty_and_abstain_are_distinct_states():
    assert EMPTY_NO_DATA != ABSTAIN   # not conflated (top_cos 0.0 vs gate-fired)


# ── approved-only (M04's honest half) + never-flip ANN guard ──────────────────
def test_recall_has_no_index_mutation_verb():
    # the Store is the SOLE index mutator; the pipeline has NO mutation verb, so a
    # pending row is unrepresentable here (the real approved-only guard lives in M03).
    pipe = _confident_setup()
    for verb in ("add", "remove", "drop", "sync_approved", "rebuild_from_store"):
        assert not hasattr(pipe, verb)


def test_authoritative_index_required():
    idx = _NonAuthIndex()
    idx.add(1, _e(0))
    r = _pipe(index=idx, reader=FakeEpisodeReader(), query_vec=_e(0)).recall(
        "q", agent_id="A")
    assert r.state == EMPTY_NO_DATA and r.hits == ()   # never silently flips to ANN


# ── trace id ──────────────────────────────────────────────────────────────────
def test_trace_id_emitted_and_unique():
    p = _confident_setup()
    a = p.recall("q", agent_id="A")
    b = p.recall("q", agent_id="A")
    assert a.trace_id and b.trace_id and a.trace_id != b.trace_id
    # present on abstain too
    idx, rdr = FakeIndex(), FakeEpisodeReader()
    for eid in (1, 2, 3):
        idx.add(eid, _cos_vec(0.0)); rdr.add(eid, "t")
    assert _pipe(index=idx, reader=rdr, query_vec=_e(0)).recall(
        "q", agent_id="A").trace_id


# ── top_n is hits-length only, never the abstain decision ─────────────────────
def test_recall_top_n_size_only():
    r1 = _confident_setup(top_n=1).recall("q", agent_id="A")
    r5 = _confident_setup(top_n=5).recall("q", agent_id="A")
    assert r1.state == CONFIDENT and r5.state == CONFIDENT   # same gate decision
    assert len(r1.hits) == 1 and len(r5.hits) == 3           # only hits length changes
    assert r1.top_cos == pytest.approx(r5.top_cos)


# ── fail-closed on every internal raise ───────────────────────────────────────
def test_embedder_failure_is_empty_no_data():
    idx, rdr = FakeIndex(), FakeEpisodeReader()
    idx.add(1, _e(0)); rdr.add(1, "gold")
    r = _pipe(index=idx, reader=rdr, query_vec=_e(0),
              embedder=_RaisingProvider(_e(0))).recall("q", agent_id="A")
    assert r.state == EMPTY_NO_DATA and r.hits == ()   # never raises into the caller


def test_index_search_raise_is_empty_no_data():
    idx = _RaisingSearchIndex()
    idx.add(1, _e(0))
    r = _pipe(index=idx, reader=FakeEpisodeReader(), query_vec=_e(0)).recall(
        "q", agent_id="A")
    assert r.state == EMPTY_NO_DATA


# ── swap seam: a second RecallIndex adapter ⟹ identical (non-trace) result ────
def test_recall_against_alternate_index_adapter():
    def run(make_index):
        idx, rdr = make_index(), FakeEpisodeReader()
        for eid, vec, text in ((1, _e(0), "gold"), (2, _cos_vec(0.1), "d")):
            idx.add(eid, vec); rdr.add(eid, text)
        r = _pipe(index=idx, reader=rdr, query_vec=_e(0)).recall(
            "q", agent_id="A")
        return (r.state, tuple((h.episode_id, h.text, round(h.sim, 6)) for h in r.hits),
                round(r.top_cos, 9))
    assert run(FakeIndex) == run(_ListIndex)   # internal repr differs; result identical


# ── CONFIDENT iff boundary (gate-pass + ≥1 hit) + resolve-away fail-closed ────
def test_single_candidate_passes_to_confident_with_that_hit():
    idx, rdr = FakeIndex(), FakeEpisodeReader()
    idx.add(7, _e(0)); rdr.add(7, "only")
    r = _pipe(index=idx, reader=rdr, query_vec=_e(0)).recall(
        "q", agent_id="A")
    assert r.state == CONFIDENT and len(r.hits) == 1 and r.hits[0].episode_id == 7


# ── AUDIT wf_e5fdbb3c-5f1 hardening regression tests ──────────────────────────
def test_malformed_search_result_is_empty_no_data():
    # #D: a contract-violating adapter (NULL cosine / wrong arity) must fail-closed,
    # not raise into the caller. The sims float-coercion must be inside the fail-closed try.
    class _BadSimIndex(FakeIndex):
        def search(self, query, k):
            return [(1, None)]   # NULL distance from a misbehaving pgvector/Qdrant adapter

    idx = _BadSimIndex()
    idx.add(1, _e(0))
    r = _pipe(index=idx, reader=FakeEpisodeReader(), query_vec=_e(0)).recall(
        "q", agent_id="A")
    assert r.state == EMPTY_NO_DATA


def test_nan_sim_index_never_confident():
    # #A end-to-end: an adapter emitting a NaN cosine must NOT yield CONFIDENT, and no
    # RecallHit carrying sim=NaN may surface (the gate fail-closes on the undecidable set).
    class _NaNIndex(FakeIndex):
        def search(self, query, k):
            return [(1, 1.0), (2, float("nan"))]

    idx = _NaNIndex()
    idx.add(1, _e(0))
    idx.add(2, _e(1))
    reader = FakeEpisodeReader()
    reader.add(1, "real gold"); reader.add(2, "corrupted")   # resolvable ⇒ would surface but for the gate
    r = _pipe(index=idx, reader=reader, query_vec=_e(0)).recall(
        "q", agent_id="A")
    assert r.state != CONFIDENT
    assert all(h.sim == h.sim for h in r.hits)   # no NaN sim surfaced (NaN != NaN)


def test_confident_result_must_carry_hits():
    # #E: the CONFIDENT<->has-hits biconditional made fully structural — CONFIDENT with empty
    # hits is unconstructable (the reverse of abstain-no-resurrect: no empty-confident either).
    from hive.domain.models import RecallResult
    with pytest.raises(ValueError):
        RecallResult(CONFIDENT, "t", (), 0.0)


def test_gate_passes_but_all_resolve_away_is_empty_no_data():
    idx = FakeIndex()
    idx.add(7, _e(0))                       # gate will pass (single relevant candidate)
    r = _pipe(index=idx, reader=FakeEpisodeReader(), query_vec=_e(0)).recall(
        "q", agent_id="A")
    assert r.state == EMPTY_NO_DATA and r.hits == ()  # fail-closed


# ── select_served (pure): trust-dominance THEN MMR over a sim-desc pool ─────────
from hive.domain.models import Episode, Scored, content_hash
from hive.domain.recall import select_served


def _S(eid, sim):
    return Scored(eid, 1.0, sim)


def _ep(eid, vec, *, trust="established", polarity="neutral", ts=0, text=None):
    text = text or f"memory-{eid}"
    return Episode(id=eid, tenant_id="t", text=text, weight=1.0, ts=ts,
                   tags="", content_hash=content_hash(text), status="approved",
                   proposed_by="x", value=vec, trust=trust, polarity=polarity)


def test_select_drops_trust_dominated_near_dup():
    # of a near-dup pair (cos ≫ dup_tau) with strict trust dominance, the lower-trust member
    # is dropped (the folded-in conflict.suppress).
    items = [(_S(1, 0.95), _ep(1, _cos_vec(0.95), trust="established")),
             (_S(2, 0.92), _ep(2, _cos_vec(0.92), trust="provisional"))]
    kept = select_served(items, dup_tau=0.80, top_n=10)
    assert [s.episode_id for s, _ in kept] == [1]


def test_select_keeps_higher_trust_truth_over_higher_sim_poison():
    # ★ the decisive trust-first pin: a HIGHER-cosine LOWER-trust poison sits ABOVE a
    # lower-cosine higher-trust truth in sim order. Pure greedy-by-sim MMR would keep the
    # poison and drop the truth; trust-dominance runs FIRST and drops the poison, so the
    # TRUTH survives. Reordering the two passes reds this.
    items = [(_S(2, 0.96), _ep(2, _cos_vec(0.96), trust="provisional")),   # higher sim, poison
             (_S(1, 0.93), _ep(1, _cos_vec(0.93), trust="established"))]   # lower sim, truth
    kept = select_served(items, dup_tau=0.80, top_n=10)
    assert [s.episode_id for s, _ in kept] == [1]


def test_select_collapses_same_trust_echoes_to_higher_sim():
    # equal trust ⇒ trust-dominance drops nothing; MMR collapses the cosine echo to the
    # higher-sim representative (items are sim-desc, so the first survivor wins).
    items = [(_S(1, 0.95), _ep(1, _cos_vec(0.95), trust="established")),
             (_S(2, 0.92), _ep(2, _cos_vec(0.92), trust="established"))]
    kept = select_served(items, dup_tau=0.80, top_n=10)
    assert [s.episode_id for s, _ in kept] == [1]


def test_select_caps_at_top_n_after_filtering():
    # three mutually-distinct (orthogonal, no near-dup) items: nothing is filtered, the cap
    # takes the top_n by the input sim order.
    items = [(_S(1, 0.90), _ep(1, _e(1))),
             (_S(2, 0.85), _ep(2, _e(2))),
             (_S(3, 0.80), _ep(3, _e(3)))]
    kept = select_served(items, dup_tau=0.80, top_n=2)
    assert [s.episode_id for s, _ in kept] == [1, 2]


def test_select_keeps_both_on_undecidable_cosine():
    # an undecidable cosine (a None vector) is treated as NOT-a-near-dup — REMOVE-only, so
    # keep both (never drop on uncertainty).
    items = [(_S(1, 0.95), _ep(1, None)), (_S(2, 0.92), _ep(2, None))]
    kept = select_served(items, dup_tau=0.80, top_n=10)
    assert [s.episode_id for s, _ in kept] == [1, 2]


def test_select_empty_pool():
    assert select_served([], dup_tau=0.80, top_n=10) == []


# ── select_served wired through the pipeline (default ON) ──────────────────────
def _pair_pipe(*, select, gold_trust="established", poison_trust="provisional", ledger=None):
    """A confident pipe over a near-dup pair (both clear tau_serve): a gold and a poison whose
    vectors are ~identical so select_served pairs them; distinct trust tiers so trust-dominance
    can act."""
    index, reader = FakeIndex(), FakeEpisodeReader()
    gold_v, poison_v = _cos_vec(0.95), _cos_vec(0.92)
    index.add(1, gold_v)
    reader.add(1, "the port is 8080", trust=gold_trust, value=gold_v)
    index.add(2, poison_v)
    reader.add(2, "the port is 9090", trust=poison_trust, value=poison_v)
    return _pipe(index=index, reader=reader, query_vec=_e(0), ledger=ledger, select=select)


def test_select_false_serves_both_near_dups_byte_identical():
    # OFF ⇒ byte-identical naive truncation: both near-dups served in dense order, no dedup.
    r = _pair_pipe(select=False).recall("q", agent_id="A")
    assert r.state == CONFIDENT
    assert [h.episode_id for h in r.hits] == [1, 2]


def test_select_on_drops_lower_trust_poison_keeps_gold():
    # ON ⇒ the strictly-lower-trust provisional poison (id=2) is dropped; the gold is served.
    r = _pair_pipe(select=True).recall("q", agent_id="A")
    assert r.state == CONFIDENT
    assert {h.episode_id for h in r.hits} == {1}


def test_selected_out_row_is_not_exposed_belt_ordering():
    # a row select_served dropped must NOT be exposed — exposure refreshes liveness, so a
    # dropped row must never reach the ledger (belt-ordering invariant).
    led = FakeLedger()
    _pair_pipe(select=True, ledger=led).recall("q", agent_id="A")
    exposed = {e for ex in led.exposures for e, _m in ex["items"]}
    assert exposed == {1}                     # only the surviving gold, never the poison


def test_select_fails_closed_on_detector_error(monkeypatch):
    # a select_served detector fault is inside the surface try ⇒ EMPTY_NO_DATA (fail-closed):
    # a selection stage that cannot decide must abstain, never serve an un-vetted set.
    import hive.domain.recall as recall_mod

    def boom(*a, **k):
        raise RuntimeError("detector boom")
    monkeypatch.setattr(recall_mod, "detect_conflicts", boom)
    r = _pair_pipe(select=True).recall("q", agent_id="A")
    assert r.state == EMPTY_NO_DATA and r.hits == ()


# ── tau_serve: a FLAT-but-relevant field serves at the product default, abstains at 1.0 ──
def _flat_relevant_pipe(*, tau_serve):
    """Three candidates at cosines 0.85/0.84/0.83 to the query ⇒ a FLAT field, but all
    absolutely relevant. The shift-invariant entropy gate abstained on the flatness; the
    absolute-relevance gate SERVES when they clear tau_serve (≤ 0.85) and abstains at 1.0."""
    index, reader = FakeIndex(), FakeEpisodeReader()
    for eid, c, text in ((1, 0.85, "the gold fact"),
                         (2, 0.84, "a sibling fact"),
                         (3, 0.83, "another sibling")):
        index.add(eid, _cos_vec(c))
        reader.add(eid, text)
    return _pipe(index=index, reader=reader, query_vec=_e(0), tau_serve=tau_serve)


def test_flat_relevant_field_serves_at_product_default():
    # at tau_serve=0.70 the flat-relevant field is SERVED — CONFIDENT with the gold + siblings,
    # the biconditional honored (hits non-empty), exposure recorded for the served set.
    led = FakeLedger()
    pipe = _flat_relevant_pipe(tau_serve=0.70)
    pipe.ledger = led
    r = pipe.recall("q", agent_id="A")
    assert r.state == CONFIDENT
    assert {h.episode_id for h in r.hits} == {1, 2, 3}
    assert {e for ex in led.exposures for e, _m in ex["items"]} == {1, 2, 3}


def test_flat_relevant_field_abstains_at_tau_serve_1p0():
    # tau_serve=1.0 ⇒ only an exact-match field serves: the SAME flat field (top cos 0.85 < 1.0)
    # abstains — the principled knob, not a flatness coincidence.
    r = _flat_relevant_pipe(tau_serve=1.0).recall("q", agent_id="A")
    assert r.state == ABSTAIN and r.hits == ()
