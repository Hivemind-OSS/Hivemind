"""Hybrid (dense + lexical RRF) recall path — the flag-inert contracts.

AC1: hybrid OFF ⇒ byte-identical results AND ledger side effects vs a pipeline
built without the feature; the lexical port is never touched (spy-pinned).
AC2: a dense-miss / lexical-hit surfaces within the confident regime.
AC3: the gate decides on the dense distribution BEFORE any lexical I/O.
AC6: a fused id with no dense mass drops fail-closed; exposure margins stay
non-negative under fusion reordering (D-H7: gaps over mass-DESCENDING order).
Any lexical fault degrades to dense — never to EMPTY.
"""
from __future__ import annotations


import numpy as np
import pytest

from hive.domain.models import ABSTAIN, CONFIDENT
from hive.domain.recall import NormalizedEntropyGate, RecallPipeline
from tests.fakes._fakes import (
    FakeEpisodeReader, FakeIndex, FakeLedger, FakeScanner,
)

D = 16


def _e(i: int, d: int = D) -> np.ndarray:
    v = np.zeros(d, dtype=np.float32)
    v[i] = 1.0
    return v


def _cos_vec(c: float, d: int = D) -> np.ndarray:
    v = np.zeros(d, dtype=np.float32)
    v[0] = c
    v[1] = float(np.sqrt(max(0.0, 1.0 - c * c)))
    return v


class _StubProvider:
    def __init__(self, vec: np.ndarray, d: int = D, w_version: int = 1) -> None:
        self._vec = np.asarray(vec, dtype=np.float32)
        self.d = d
        self.w_version = w_version

    def encode(self, text: str) -> np.ndarray:
        return self._vec

    def encode_batch(self, texts):
        return np.stack([self._vec for _ in texts])


class _SpyLexical:
    """Records every call; raises so an unexpected invocation can never silently
    shape the result (the degrade path would mask a raise — the CALL LIST is the
    strict never-touched contract)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def search_text(self, query: str, k: int) -> list[tuple[int, float]]:
        self.calls.append((query, k))
        raise AssertionError("lexical channel must not be reached")


class _StubLexical:
    def __init__(self, results: list[tuple[int, float]]) -> None:
        self.results = list(results)
        self.calls: list[tuple[str, int]] = []

    def search_text(self, query: str, k: int) -> list[tuple[int, float]]:
        self.calls.append((query, k))
        return list(self.results)


class _SpyLifecycle:
    def __init__(self) -> None:
        self.on_miss_agents: list[str] = []

    def on_miss(self, vector, agent_id: str):
        self.on_miss_agents.append(agent_id)
        return []


def _pipe(*, index, reader, query_vec, ledger, lifecycle=None, recall_top_n=10,
          **feature_kwargs):
    """feature_kwargs absent ⇒ the pipeline is built exactly as before the
    feature existed (the AC1 baseline)."""
    return RecallPipeline(
        embedder=_StubProvider(query_vec), index=index,
        gate=NormalizedEntropyGate(0.5, 16.0),
        reader=reader,
        recall_top_n=recall_top_n, ledger=ledger, clock_now=lambda: 0,
        scanner=FakeScanner(), provisional_ttl_s=10**9,
        lifecycle=lifecycle, **feature_kwargs)


def _confident_world():
    """gold (cos 1.0) + two weak distractors ⇒ peaked ⇒ CONFIDENT."""
    index, reader = FakeIndex(), FakeEpisodeReader()
    for eid, vec, text in ((1, _e(0), "the gold memory"),
                           (2, _cos_vec(0.2), "distractor a"),
                           (3, _cos_vec(0.1), "distractor b")):
        index.add(eid, vec)
        reader.add(eid, text)
    return index, reader


def _abstain_world():
    """four identical cosines ⇒ uniform mass ⇒ max entropy ⇒ ABSTAIN."""
    index, reader = FakeIndex(), FakeEpisodeReader()
    for eid in (1, 2, 3, 4):
        index.add(eid, _cos_vec(0.0))
        reader.add(eid, f"t{eid}")
    return index, reader


def _obs(r):
    """The observable result minus the per-call random trace_id."""
    return (r.state,
            tuple((h.episode_id, h.text, h.sim, h.trust, h.ts) for h in r.hits),
            r.entropy_norm, r.top_margin)


def _exposures_obs(led: FakeLedger):
    return [(e["items"], e["agent_id"], e["ts"]) for e in led.exposures]


# ── AC1: hybrid off ⇒ byte-identical, lexical untouched ───────────────────────
def test_hybrid_off_is_byte_identical_and_lexical_untouched():
    spy = _SpyLexical()

    # confident world: result + exposure rows must match the feature-absent build
    index, reader = _confident_world()
    led_base, led_off = FakeLedger(), FakeLedger()
    r_base = _pipe(index=index, reader=reader, query_vec=_e(0),
                   ledger=led_base).recall("q", agent_id="A")
    r_off = _pipe(index=index, reader=reader, query_vec=_e(0), ledger=led_off,
                  lexical_index=spy, hybrid_enabled=False,
                  ).recall("q", agent_id="A")
    assert _obs(r_off) == _obs(r_base)
    assert _exposures_obs(led_off) == _exposures_obs(led_base)
    assert led_off.misses == led_base.misses == []

    # abstain world: miss rows + on_miss triggers must match too
    aindex, areader = _abstain_world()
    led_b2, led_o2 = FakeLedger(), FakeLedger()
    life_b, life_o = _SpyLifecycle(), _SpyLifecycle()
    rb = _pipe(index=aindex, reader=areader, query_vec=_e(0), ledger=led_b2,
               lifecycle=life_b).recall("q2", agent_id="A")
    ro = _pipe(index=aindex, reader=areader, query_vec=_e(0), ledger=led_o2,
               lifecycle=life_o, lexical_index=spy, hybrid_enabled=False,
               ).recall("q2", agent_id="A")
    assert _obs(ro) == _obs(rb) and ro.state == ABSTAIN
    assert led_o2.misses == led_b2.misses and len(led_o2.misses) == 1
    assert life_o.on_miss_agents == life_b.on_miss_agents == ["A"]

    assert spy.calls == []                 # the port was NEVER reached


# ── AC3: the gate runs on the dense distribution before any lexical I/O ───────
def test_gate_abstains_before_lexical_io():
    spy = _SpyLexical()
    index, reader = _abstain_world()
    base_led, led = FakeLedger(), FakeLedger()
    r_base = _pipe(index=index, reader=reader, query_vec=_e(0),
                   ledger=base_led).recall("q", agent_id="A")
    r = _pipe(index=index, reader=reader, query_vec=_e(0), ledger=led,
              lexical_index=spy, hybrid_enabled=True,
              ).recall("q", agent_id="A")
    assert r.state == ABSTAIN and r.hits == ()
    assert _obs(r) == _obs(r_base)         # abstain identical with hybrid ON
    assert led.misses == base_led.misses
    assert spy.calls == []                 # abstain happened BEFORE lexical I/O


# ── AC2: dense-miss / lexical-hit surfaces within the confident regime ────────
def test_lexical_only_hit_surfaces_confident():
    index, reader = _confident_world()     # eid 3 is dense rank 2 — below top_n=2
    lex = _StubLexical([(3, 5.0)])         # lexical ranks it #1
    led = FakeLedger()
    r = _pipe(index=index, reader=reader, query_vec=_e(0), ledger=led,
              recall_top_n=2, lexical_index=lex, hybrid_enabled=True,
              ).recall("exact identifier q", agent_id="A")
    assert r.state == CONFIDENT
    ids = [h.episode_id for h in r.hits]
    assert 3 in ids and 1 in ids           # the lexical resurfacing AND the dense top
    assert lex.calls == [("exact identifier q", 2)]
    # dense-only control: without hybrid, eid 3 cannot make the top-2 shortlist
    r_dense = _pipe(index=index, reader=reader, query_vec=_e(0), ledger=FakeLedger(),
                    recall_top_n=2).recall("exact identifier q", agent_id="A")
    assert 3 not in [h.episode_id for h in r_dense.hits]
    # the surfaced sim stays the HONEST dense cosine, never the lexical score
    assert {h.episode_id: pytest.approx(h.sim, abs=1e-6) for h in r.hits}[3] == 0.1


# ── lexical fault ⇒ dense-only result, CONFIDENT, never EMPTY ─────────────────
def test_lexical_fault_degrades_to_dense():
    class _BoomLexical:
        def search_text(self, query, k):
            raise RuntimeError("fts boom")

    index, reader = _confident_world()
    r_dense = _pipe(index=index, reader=reader, query_vec=_e(0),
                    ledger=FakeLedger()).recall("q", agent_id="A")
    r = _pipe(index=index, reader=reader, query_vec=_e(0), ledger=FakeLedger(),
              lexical_index=_BoomLexical(), hybrid_enabled=True,
              ).recall("q", agent_id="A")
    assert r.state == CONFIDENT
    assert _obs(r) == _obs(r_dense)        # degraded TO DENSE, not to empty


# ── AC6: fused id with no dense mass drops fail-closed (no KeyError) ──────────
def test_fused_id_without_dense_mass_dropped():
    index, reader = FakeIndex(), FakeEpisodeReader()
    index.add(1, _e(0)); reader.add(1, "gold")
    index.add(2, _cos_vec(0.2)); reader.add(2, "weak")
    reader.add(99, "resolvable but never dense-searched")   # text exists; mass does not
    lex = _StubLexical([(99, 9.9)])
    r = _pipe(index=index, reader=reader, query_vec=_e(0), ledger=FakeLedger(),
              lexical_index=lex, hybrid_enabled=True,
              ).recall("q", agent_id="A")
    assert r.state == CONFIDENT
    assert [h.episode_id for h in r.hits] == [1, 2]         # 99 dropped at resolve
    assert all(h.episode_id != 99 for h in r.hits)


# ── AC6/D-V3: margins non-negative under fusion; off ⇒ byte-equal ─────────────
def test_margins_nonnegative_under_fusion():
    index, reader = _confident_world()
    # fusion puts low-mass eid 3 ABOVE high-mass eid 1 (non-monotone mass order)
    lex = _StubLexical([(3, 5.0)])
    led = FakeLedger()
    r = _pipe(index=index, reader=reader, query_vec=_e(0), ledger=led,
              recall_top_n=2, lexical_index=lex, hybrid_enabled=True,
              ).recall("q", agent_id="A")
    assert r.state == CONFIDENT
    assert [h.episode_id for h in r.hits] == [3, 1]         # fused, mass-NON-monotone
    items = led.exposures[0]["items"]
    assert {eid for eid, _m in items} == {3, 1}
    assert all(m >= 0.0 for _eid, m in items)               # D-H7: never negative
    # the gap structure: the top-mass hit carries (its mass − next mass), the
    # low-mass hit carries its own mass ⇒ the SUM telescopes to the top mass,
    # which the gate-identical softmax bounds in (0, 1]
    assert 0.0 < sum(m for _e, m in items) <= 1.0

    # hybrid OFF on the same world ⇒ margins byte-equal the feature-absent build
    led_off, led_base = FakeLedger(), FakeLedger()
    _pipe(index=index, reader=reader, query_vec=_e(0), ledger=led_off,
          recall_top_n=2, lexical_index=_StubLexical([(3, 5.0)]),
          hybrid_enabled=False).recall("q", agent_id="A")
    _pipe(index=index, reader=reader, query_vec=_e(0), ledger=led_base,
          recall_top_n=2).recall("q", agent_id="A")
    assert _exposures_obs(led_off) == _exposures_obs(led_base)
