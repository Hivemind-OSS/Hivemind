"""P1.5 — M04 RecallPipeline: the never-hallucinate enforcement point.

Full functional coverage against fakes only (hash-speed, no SQLite/network):
happy path, every §6 failure mode (embedder raise / index raise / empty /
non-authoritative), every §2 invariant (abstain⟹empty, abstain-no-resurrect
STRUCTURAL, EMPTY vs ABSTAIN distinct, entropy∈[0,1], unique trace, top_n
size-only, approved-only honest half), the D1 per-hit recall_margin value (a pure
unit pin), the A2 family spy + cross-family isolation, and the swap-seam (a 2nd index
adapter ⟹ identical result). The move-#6 exposure ledger was removed with the
producer, so recall no longer records what it surfaced — those tests are gone.
"""
from __future__ import annotations

import random

import numpy as np
import pytest

from hive.domain.models import ABSTAIN, CONFIDENT, EMPTY_NO_DATA, AgentContext, Scored
from hive.domain.recall import (
    NormalizedEntropyGate, RecallPipeline, _recall_margins,
    _resolve_query_family, _softmax_mass_from_sims,
)
from hive.domain.surfacer import UtilitySurfacer
from tests.fakes._fakes import (
    FakeEpisodeReader, FakeIndex, FakeUtilityStore,
)

D = 16
CTX = AgentContext("github.com/acme/web", "python", "bugfix")
FAM = "github.com/acme/web|python|bugfix"


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
    def __init__(self, vec: np.ndarray, d: int = D, w_version: int = 1) -> None:
        self._vec = np.asarray(vec, dtype=np.float32)
        self.d = d
        self.w_version = w_version

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


class _SpyUtil(FakeUtilityStore):
    def __init__(self, *a, **k) -> None:
        super().__init__(*a, **k)
        self.families: list[str] = []

    def utility_map(self, family_scope, *, confident_only=True):
        self.families.append(family_scope)
        return super().utility_map(family_scope, confident_only=confident_only)


def _pipe(*, index, reader, query_vec, util=None, surfacer=None,
          recall_top_n=10, h=0.5, beta=16.0, embedder=None):
    return RecallPipeline(
        embedder=embedder or _StubProvider(query_vec),
        index=index,
        gate=NormalizedEntropyGate(h, beta),
        surfacer=surfacer or UtilitySurfacer(
            enabled=False, epsilon_explore=0.1, f_min=0.5, f_max=1.5,
            rng=random.Random(0)),
        reader=reader,
        utility_store=util or FakeUtilityStore(),
        recall_top_n=recall_top_n,
    )


def _confident_setup(top_n=10, util=None, surfacer=None):
    """gold(cos1) + two weak distractors ⟹ peaked ⟹ CONFIDENT."""
    index, reader = FakeIndex(), FakeEpisodeReader()
    for eid, vec, text in ((1, _e(0), "the gold memory"),
                           (2, _cos_vec(0.1), "distractor a"),
                           (3, _cos_vec(0.05), "distractor b")):
        index.add(eid, vec)
        reader.add(eid, text, weight=1.0)
    return _pipe(index=index, reader=reader, query_vec=_e(0),
                 util=util, surfacer=surfacer, recall_top_n=top_n)


# ── D1: per-hit recall_margin is the softmax-mass gap (pure pin + mutation target)
def test_recall_margins_are_mass_gaps():
    assert _recall_margins([0.7, 0.3]) == pytest.approx([0.4, 0.3])
    assert _recall_margins([0.5, 0.3, 0.2]) == pytest.approx([0.2, 0.1, 0.2])
    assert _recall_margins([1.0]) == pytest.approx([1.0])  # single hit ⇒ own mass
    assert _recall_margins([]) == []


# ── happy path ────────────────────────────────────────────────────────────────
def test_happy_path_returns_confident_hits():
    r = _confident_setup().recall("q", agent_id="A", agent_ctx=CTX)
    assert r.state == CONFIDENT
    assert r.hits[0].episode_id == 1 and r.hits[0].text == "the gold memory"
    assert 0.0 <= r.entropy_norm <= 1.0


def test_recall_at_5_over_held_out_pairs_meets_floor():
    # 6 planted golds; each query points exactly at one gold ⟹ that gold is top.
    index, reader = FakeIndex(), FakeEpisodeReader()
    golds = [(_e(i), f"gold-{i}") for i in range(6)]
    for eid, (vec, text) in enumerate(golds):
        index.add(eid, vec)
        reader.add(eid, text)
    hit_at5 = 0
    for eid, (vec, _text) in enumerate(golds):
        r = _pipe(index=index, reader=reader, query_vec=vec).recall(
            "q", agent_id="A", agent_ctx=CTX)
        if r.state == CONFIDENT and eid in {h.episode_id for h in r.hits[:5]}:
            hit_at5 += 1
    assert hit_at5 / len(golds) >= 0.33   # §6.1#1 recall@5 floor


# ── abstain ───────────────────────────────────────────────────────────────────
def test_abstain_returns_empty_hits():
    # 4 candidates with IDENTICAL cosine ⟹ uniform mass ⟹ max entropy ⟹ abstain
    index, reader = FakeIndex(), FakeEpisodeReader()
    for eid in (1, 2, 3, 4):
        index.add(eid, _cos_vec(0.0))   # all cos 0 with query e0
        reader.add(eid, f"t{eid}")
    r = _pipe(index=index, reader=reader, query_vec=_e(0)).recall(
        "q", agent_id="A", agent_ctx=CTX)
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
        "q", agent_id="A", agent_ctx=CTX)
    assert r.state == ABSTAIN and r.hits == ()


def test_empty_index_is_empty_no_data():
    r = _pipe(index=FakeIndex(), reader=FakeEpisodeReader(), query_vec=_e(0)).recall(
        "q", agent_id="A", agent_ctx=CTX)
    assert r.state == EMPTY_NO_DATA and r.hits == () and r.entropy_norm == 0.0


def test_empty_and_abstain_are_distinct_states():
    assert EMPTY_NO_DATA != ABSTAIN   # not conflated (entropy 0.0 vs gate-fired)


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
        "q", agent_id="A", agent_ctx=CTX)
    assert r.state == EMPTY_NO_DATA and r.hits == ()   # never silently flips to ANN


# ── trace id ──────────────────────────────────────────────────────────────────
def test_trace_id_emitted_and_unique():
    p = _confident_setup()
    a = p.recall("q", agent_id="A", agent_ctx=CTX)
    b = p.recall("q", agent_id="A", agent_ctx=CTX)
    assert a.trace_id and b.trace_id and a.trace_id != b.trace_id
    # present on abstain too
    idx, rdr = FakeIndex(), FakeEpisodeReader()
    for eid in (1, 2, 3):
        idx.add(eid, _cos_vec(0.0)); rdr.add(eid, "t")
    assert _pipe(index=idx, reader=rdr, query_vec=_e(0)).recall(
        "q", agent_id="A", agent_ctx=CTX).trace_id


# ── top_n is hits-length only, never the abstain decision ─────────────────────
def test_recall_top_n_size_only():
    r1 = _confident_setup(top_n=1).recall("q", agent_id="A", agent_ctx=CTX)
    r5 = _confident_setup(top_n=5).recall("q", agent_id="A", agent_ctx=CTX)
    assert r1.state == CONFIDENT and r5.state == CONFIDENT   # same gate decision
    assert len(r1.hits) == 1 and len(r5.hits) == 3           # only hits length changes
    assert r1.entropy_norm == pytest.approx(r5.entropy_norm)


# ── fail-closed on every internal raise ───────────────────────────────────────
def test_embedder_failure_is_empty_no_data():
    idx, rdr = FakeIndex(), FakeEpisodeReader()
    idx.add(1, _e(0)); rdr.add(1, "gold")
    r = _pipe(index=idx, reader=rdr, query_vec=_e(0),
              embedder=_RaisingProvider(_e(0))).recall("q", agent_id="A", agent_ctx=CTX)
    assert r.state == EMPTY_NO_DATA and r.hits == ()   # never raises into the caller


def test_index_search_raise_is_empty_no_data():
    idx = _RaisingSearchIndex()
    idx.add(1, _e(0))
    r = _pipe(index=idx, reader=FakeEpisodeReader(), query_vec=_e(0)).recall(
        "q", agent_id="A", agent_ctx=CTX)
    assert r.state == EMPTY_NO_DATA


# ── swap seam: a second RecallIndex adapter ⟹ identical (non-trace) result ────
def test_recall_against_alternate_index_adapter():
    def run(make_index):
        idx, rdr = make_index(), FakeEpisodeReader()
        for eid, vec, text in ((1, _e(0), "gold"), (2, _cos_vec(0.1), "d")):
            idx.add(eid, vec); rdr.add(eid, text)
        r = _pipe(index=idx, reader=rdr, query_vec=_e(0)).recall(
            "q", agent_id="A", agent_ctx=CTX)
        return (r.state, tuple((h.episode_id, h.text, round(h.sim, 6)) for h in r.hits),
                round(r.entropy_norm, 9), round(r.top_margin, 9))
    assert run(FakeIndex) == run(_ListIndex)   # internal repr differs; result identical


# ── A2: family spy + cross-family isolation (pipeline-level) ──────────────────
def test_recall_queries_utility_map_with_resolved_family():
    spy = _SpyUtil()
    _confident_setup(util=spy).recall("q", agent_id="A", agent_ctx=CTX)
    assert spy.families == [FAM]   # exactly the resolved family, no cross-family fold


def _ab_reorder_setup(util, *, surfacer):
    index, reader = FakeIndex(), FakeEpisodeReader()
    index.add(1, _e(0)); reader.add(1, "A", weight=1.0)          # sim 1.0 (base rank 0)
    index.add(2, _cos_vec(0.5)); reader.add(2, "B", weight=1.0)  # sim 0.5 (base rank 1)
    return _pipe(index=index, reader=reader, query_vec=_e(0), util=util,
                 surfacer=surfacer)


def test_cross_family_posterior_does_not_reorder():
    surf = UtilitySurfacer(enabled=True, epsilon_explore=0.0, f_min=0.5, f_max=1.5,
                           rng=random.Random(0))
    from hive.domain.attribution import CreditDelta
    # confident posterior on B, but under a DIFFERENT family than the query's
    other = FakeUtilityStore()
    other.apply_credit([CreditDelta(episode_id=2, family_scope="other|fam|x",
                                    d_wins=8.0, d_losses=0.0, source_agent="s")])
    r = _ab_reorder_setup(other, surfacer=surf).recall("q", agent_id="A", agent_ctx=CTX)
    assert [h.episode_id for h in r.hits] == [1, 2]   # NOT reordered (family mismatch)
    # positive control: same posterior under the QUERY's family ⟹ B promoted
    same = FakeUtilityStore()
    same.apply_credit([CreditDelta(episode_id=2, family_scope=FAM,
                                   d_wins=8.0, d_losses=0.0, source_agent="s")])
    r2 = _ab_reorder_setup(same, surfacer=surf).recall("q", agent_id="A", agent_ctx=CTX)
    assert [h.episode_id for h in r2.hits] == [2, 1]   # B promoted by confident utility


# ── Phase-1 surfacer inertness through the pipeline ───────────────────────────
def test_surfacer_disabled_is_passthrough_through_pipeline():
    from hive.domain.attribution import CreditDelta
    util = FakeUtilityStore()
    util.apply_credit([CreditDelta(episode_id=2, family_scope=FAM,
                                   d_wins=8.0, d_losses=0.0, source_agent="s")])
    surf = UtilitySurfacer(enabled=False, epsilon_explore=0.0, f_min=0.5, f_max=1.5,
                           rng=random.Random(0))
    r = _ab_reorder_setup(util, surfacer=surf).recall("q", agent_id="A", agent_ctx=CTX)
    assert [h.episode_id for h in r.hits] == [1, 2]   # sim order, utility ignored (Phase-1)


# ── CONFIDENT iff boundary (gate-pass + ≥1 hit) + resolve-away fail-closed ────
def test_single_candidate_passes_to_confident_with_that_hit():
    idx, rdr = FakeIndex(), FakeEpisodeReader()
    idx.add(7, _e(0)); rdr.add(7, "only")
    r = _pipe(index=idx, reader=rdr, query_vec=_e(0)).recall(
        "q", agent_id="A", agent_ctx=CTX)
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
        "q", agent_id="A", agent_ctx=CTX)
    assert r.state == EMPTY_NO_DATA


def test_raising_surfacer_is_empty_no_data():
    # #C: a raising surfacer (a Phase-2 collaborator) must degrade to EMPTY, not raise.
    class _BoomSurfacer:
        def order(self, scored, utility_map, *, family_scope):
            raise RuntimeError("surfacer boom")

    r = _confident_setup(surfacer=_BoomSurfacer()).recall(
        "q", agent_id="A", agent_ctx=CTX)
    assert r.state == EMPTY_NO_DATA and r.hits == ()


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
        "q", agent_id="A", agent_ctx=CTX)
    assert r.state != CONFIDENT
    assert all(h.sim == h.sim for h in r.hits)   # no NaN sim surfaced (NaN != NaN)


def test_confident_result_must_carry_hits():
    # #E: the §2.1 biconditional made fully structural — CONFIDENT with empty hits is
    # unconstructable (the reverse of abstain-no-resurrect: no empty-confident either).
    from hive.domain.models import RecallResult
    with pytest.raises(ValueError):
        RecallResult(CONFIDENT, "t", (), 0.0, 0.0)


def test_gate_passes_but_all_resolve_away_is_empty_no_data():
    idx = FakeIndex()
    idx.add(7, _e(0))                       # gate will pass (single peaked candidate)
    r = _pipe(index=idx, reader=FakeEpisodeReader(), query_vec=_e(0)).recall(
        "q", agent_id="A", agent_ctx=CTX)
    assert r.state == EMPTY_NO_DATA and r.hits == ()  # fail-closed
