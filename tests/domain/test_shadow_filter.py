"""CV3 — serve-time version shadowing. The pure ``shadow_filter`` (winner =
trust rank, then newer ts, then lower id; non-finite never shadows) and its
pipeline wiring: OFF ⇒ byte-identical (golden), ON ⇒ the filter runs at
RESOLVE, BEFORE exposure — a shadowed row's liveness is never refreshed by the
query that hid it (the resurrection-ordering pin)."""
from __future__ import annotations

import random

import numpy as np

from hive.domain.lifecycle import ESTABLISHED, PROVISIONAL
from hive.domain.models import CONFIDENT, AgentContext
from hive.domain.recall import NormalizedEntropyGate, RecallPipeline, shadow_filter
from hive.domain.surfacer import UtilitySurfacer
from tests.fakes._fakes import (
    FakeEpisodeReader, FakeIndex, FakeLedger, FakeScanner, FakeUtilityStore,
    make_episode,
)

D = 8
CTX = AgentContext("github.com/acme/web", "python", "general")


def _unit(*xs) -> np.ndarray:
    v = np.array(list(xs) + [0.0] * (D - len(xs)), dtype=np.float32)
    return v / np.linalg.norm(v)


A_VEC = _unit(1.0, 0.05)          # two near-duplicates: cos(A,B) ≈ 0.9999
B_VEC = _unit(1.0, 0.06)
FAR_VEC = _unit(0.0, 0.0, 1.0)    # well below any shadow_tau


def _ep(eid, *, trust=ESTABLISHED, ts=0, vec=None, text=None):
    return make_episode(eid, text or f"memory {eid}", trust=trust, ts=ts,
                        last_active_ts=10**9, value=vec)


# ── the pure filter ────────────────────────────────────────────────────────────
def test_shadow_prefers_trust_then_ts():
    # established beats a NEWER provisional near-dup (vouched > unverified)…
    est_old = _ep(1, trust=ESTABLISHED, ts=10, vec=A_VEC)
    prov_new = _ep(2, trust=PROVISIONAL, ts=99, vec=B_VEC)
    assert shadow_filter([prov_new, est_old], shadow_tau=0.95) == [est_old]
    # …and between two established rows, the newer wins
    est_new = _ep(3, trust=ESTABLISHED, ts=99, vec=B_VEC)
    assert shadow_filter([est_old, est_new], shadow_tau=0.95) == [est_new]
    # exact tie (trust + ts): lower episode_id wins, deterministically
    twin_a = _ep(4, trust=ESTABLISHED, ts=50, vec=A_VEC)
    twin_b = _ep(5, trust=ESTABLISHED, ts=50, vec=B_VEC)
    assert shadow_filter([twin_b, twin_a], shadow_tau=0.95) == [twin_a]


def test_shadow_below_tau_serves_both_and_preserves_order():
    a = _ep(1, ts=10, vec=A_VEC)
    far = _ep(2, ts=99, vec=FAR_VEC)
    assert shadow_filter([a, far], shadow_tau=0.95) == [a, far]
    assert shadow_filter([far, a], shadow_tau=0.95) == [far, a]   # input order kept
    assert shadow_filter([a], shadow_tau=0.95) == [a]             # k<2 passthrough


def test_nonfinite_never_shadows():
    # an undecidable vector (NaN / zero-norm / absent) neither shadows nor is
    # shadowed — fail-open is serve-both, the status quo
    nan_vec = np.array([np.nan] + [0.0] * (D - 1), dtype=np.float32)
    a = _ep(1, trust=ESTABLISHED, ts=10, vec=A_VEC)
    poisoned = _ep(2, trust=PROVISIONAL, ts=99, vec=nan_vec)
    valueless = _ep(3, trust=PROVISIONAL, ts=99, vec=None)
    zero = _ep(4, trust=PROVISIONAL, ts=99, vec=np.zeros(D, dtype=np.float32))
    assert shadow_filter([a, poisoned], shadow_tau=0.95) == [a, poisoned]
    assert shadow_filter([a, valueless], shadow_tau=0.95) == [a, valueless]
    assert shadow_filter([a, zero], shadow_tau=0.95) == [a, zero]


def test_shadow_chain_winner_takes_all():
    # A≈B≈C: the single winner serves; both losers are judged against the KEPT
    # set, so a chain cannot resurrect a middle row
    c_vec = _unit(1.0, 0.055)
    win = _ep(1, trust=ESTABLISHED, ts=99, vec=A_VEC)
    mid = _ep(2, trust=PROVISIONAL, ts=50, vec=B_VEC)
    low = _ep(3, trust=PROVISIONAL, ts=10, vec=c_vec)
    assert shadow_filter([low, mid, win], shadow_tau=0.95) == [win]


# ── pipeline wiring ────────────────────────────────────────────────────────────
class _StubProvider:
    def __init__(self, vec: np.ndarray) -> None:
        self._vec = np.asarray(vec, dtype=np.float32)
        self.d = D
        self.w_version = 1

    def encode(self, text: str) -> np.ndarray:
        return self._vec

    def encode_batch(self, texts):
        return np.stack([self._vec for _ in texts])


def _fixture(*, b_trust=PROVISIONAL):
    """Two near-duplicate servable rows (established id=1 ts=10; b_trust id=2
    ts=99) + one far row (id=3), indexed and resolvable WITH their vectors."""
    index, reader = FakeIndex(), FakeEpisodeReader()
    for eid, vec, trust, ts in ((1, A_VEC, ESTABLISHED, 10),
                                (2, B_VEC, b_trust, 99),
                                (3, FAR_VEC, ESTABLISHED, 20)):
        index.add(eid, vec)
        reader.add(eid, f"memory {eid}", trust=trust, ts=ts,
                   last_active_ts=10**9, value=vec)
    return index, reader


def _pipe(index, reader, ledger, **feature_kwargs):
    """feature_kwargs absent ⇒ the pipeline exactly as built before CV3 (the
    golden baseline). The gate is held open (h=1.0) — gating is not under test."""
    return RecallPipeline(
        embedder=_StubProvider(A_VEC), index=index,
        gate=NormalizedEntropyGate(1.0, 16.0),
        surfacer=UtilitySurfacer(enabled=False, epsilon_explore=0.1,
                                 f_min=0.5, f_max=1.5, rng=random.Random(0)),
        reader=reader, utility_store=FakeUtilityStore(),
        recall_top_n=10, ledger=ledger, clock_now=lambda: 0,
        scanner=FakeScanner(), provisional_ttl_s=10**18,
        lifecycle=None, **feature_kwargs)


def test_shadow_off_is_byte_identical():
    # ★ the golden: shadow=False output (hits AND exposure side effects) equals
    # a pipeline built before the feature existed
    index, reader = _fixture()
    led_base, led_off = FakeLedger(), FakeLedger()
    base = _pipe(index, reader, led_base).recall("q", agent_id="a", agent_ctx=CTX)
    off = _pipe(index, reader, led_off, shadow_enabled=False,
                shadow_tau=0.95).recall("q", agent_id="a", agent_ctx=CTX)
    assert off.state == base.state == CONFIDENT
    assert off.hits == base.hits                              # both near-dups served
    assert {h.episode_id for h in off.hits} == {1, 2, 3}
    assert [e["items"] for e in led_off.exposures] == \
        [e["items"] for e in led_base.exposures]


def test_shadow_on_serves_winner_only():
    index, reader = _fixture()
    res = _pipe(index, reader, FakeLedger(), shadow_enabled=True,
                shadow_tau=0.95).recall("q", agent_id="a", agent_ctx=CTX)
    assert res.state == CONFIDENT
    ids = [h.episode_id for h in res.hits]
    assert 1 in ids and 3 in ids and 2 not in ids             # vouched row wins


def test_shadowed_row_gets_no_exposure():
    # ★ the resurrection-ordering pin: the ledger sees ONLY the post-filter set
    index, reader = _fixture()
    ledger = FakeLedger()
    _pipe(index, reader, ledger, shadow_enabled=True,
          shadow_tau=0.95).recall("q", agent_id="a", agent_ctx=CTX)
    assert len(ledger.exposures) == 1
    exposed = {eid for eid, _m in ledger.exposures[0]["items"]}
    assert exposed == {1, 3}                                  # 2 was hidden ⇒ no refresh


def test_shadow_on_nonfinite_serves_both_in_pipeline():
    index, reader = _fixture()
    nan_vec = np.array([np.nan] + [0.0] * (D - 1), dtype=np.float32)
    reader.add(2, "memory 2", trust=PROVISIONAL, ts=99,
               last_active_ts=10**9, value=nan_vec)           # poisoned carrier
    res = _pipe(index, reader, FakeLedger(), shadow_enabled=True,
                shadow_tau=0.95).recall("q", agent_id="a", agent_ctx=CTX)
    assert {h.episode_id for h in res.hits} == {1, 2, 3}      # fail-open: both serve
