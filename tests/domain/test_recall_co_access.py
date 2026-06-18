"""ASSOCIATIVE RECALL (co-access edges) — the write-accrual + read-surfacing channel.

Phase 1 (write): a CONFIDENT recall upserts pairwise co-access edges over its served
hits, bounded by ``_ASSOC_FANOUT``; default OFF ⇒ the CoAccess port is NEVER touched;
``hits`` byte-identical with the flag on/off; fail-open (a raising port never breaks
recall); ABSTAIN/EMPTY writes no edges.

Phase 2 (read): a CONFIDENT recall surfaces co-accessed neighbors on the SEPARATE
``associations`` channel (never merged into ``hits``); the ``_ASSOC_MIN_WEIGHT`` floor
drops noise edges; an unservable neighbor is re-checked out; a neighbor already in
``hits`` is excluded; ``_ASSOC_TOP_N`` caps; default OFF ⇒ ``associations == ()``;
the read records NO exposure (the belt-ordering invariant); fail-open.

All against fakes (hash-speed, no SQLite/torch/clock).
"""
from __future__ import annotations

import numpy as np

from hive.domain.lifecycle import QUARANTINED
from hive.domain.models import (
    ABSTAIN, CONFIDENT, EMPTY_NO_DATA, RecallAssoc, RecallHit, RecallResult,
)
from hive.domain.recall import (
    _ASSOC_FANOUT, _ASSOC_MIN_WEIGHT, _ASSOC_TOP_N,
    NormalizedEntropyGate, RecallPipeline,
)
from tests.fakes._fakes import (
    FakeCoAccess, FakeEpisodeReader, FakeIndex, FakeLedger, FakeScanner,
)

D = 16
AGENT = "seat-A"


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


def _pipe(*, index, reader, co_access=None, co_access_enabled=False,
          associations_enabled=False, recall_top_n=10, h=0.5, beta=16.0,
          ledger=None, clock_now=None):
    return RecallPipeline(
        embedder=_StubProvider(_e(0)),
        index=index, gate=NormalizedEntropyGate(h, beta),
        reader=reader, recall_top_n=recall_top_n,
        ledger=ledger if ledger is not None else FakeLedger(),
        clock_now=clock_now or (lambda: 0), scanner=FakeScanner(),
        provisional_ttl_s=10**9,
        co_access=co_access, co_access_enabled=co_access_enabled,
        associations_enabled=associations_enabled)


def _confident_index(reader=None, n_extra_distractors=2):
    """gold(cos1) + weak distractors ⟹ peaked ⟹ CONFIDENT. Returns >1 resolved hit."""
    index = FakeIndex()
    reader = reader or FakeEpisodeReader()
    index.add(1, _e(0))
    reader.add(1, "the gold memory", trust="established")
    for k in range(n_extra_distractors):
        eid = 2 + k
        index.add(eid, _cos_vec(0.1 - 0.01 * k))
        reader.add(eid, f"distractor {eid}", trust="established")
    return index, reader


def _abstain_index(reader=None):
    """4 candidates with IDENTICAL cosine ⟹ uniform mass ⟹ abstain."""
    index = FakeIndex()
    reader = reader or FakeEpisodeReader()
    for eid in (1, 2, 3, 4):
        index.add(eid, _cos_vec(0.0))
        reader.add(eid, f"trusted-{eid}", trust="established")
    return index, reader


# ════════════════════════ PHASE 1 — write accrual ════════════════════════════

def test_co_access_off_by_default_port_never_touched():
    """Default OFF ⇒ byte-inert: the CoAccess port is never called."""
    index, reader = _confident_index()
    ca = FakeCoAccess()
    r = _pipe(index=index, reader=reader, co_access=ca).recall("q", agent_id=AGENT)
    assert r.state == CONFIDENT
    assert ca.writes == 0                              # never written


def test_co_access_inert_when_port_none_even_if_enabled():
    index, reader = _confident_index()
    r = _pipe(index=index, reader=reader, co_access=None,
              co_access_enabled=True).recall("q", agent_id=AGENT)
    assert r.state == CONFIDENT                        # no crash, nothing to write to


def test_confident_writes_pairwise_edges_over_served_hits():
    index, reader = _confident_index()                # eids 1,2,3 all resolve
    ca = FakeCoAccess()
    r = _pipe(index=index, reader=reader, co_access=ca,
              co_access_enabled=True).recall("q", agent_id=AGENT)
    served = {h.episode_id for h in r.hits}
    assert served == {1, 2, 3}
    assert ca.writes == 1                              # one upsert call
    # every C(served,2) pair accrued exactly once
    pairs = set(ca._edges.keys())
    assert pairs == {(1, 2), (1, 3), (2, 3)}


def test_fanout_cap_bounds_pairs_independent_of_top_n():
    """A recall serving > _ASSOC_FANOUT hits writes only C(_ASSOC_FANOUT,2) pairs —
    the I7 upsert bound is the constant, NOT recall_top_n."""
    n = _ASSOC_FANOUT + 4                              # serve more hits than the fanout
    index = FakeIndex()
    reader = FakeEpisodeReader()
    index.add(1, _e(0))                               # one clear winner → CONFIDENT
    reader.add(1, "gold", trust="established")
    for eid in range(2, n + 1):                       # near-zero, descending, weak distractors
        index.add(eid, _cos_vec(0.02 - 0.0001 * eid))
        reader.add(eid, f"d{eid}", trust="established")
    ca = FakeCoAccess()
    r = _pipe(index=index, reader=reader, co_access=ca, co_access_enabled=True,
              recall_top_n=n).recall("q", agent_id=AGENT)
    assert len(r.hits) == n                            # all served as hits
    expected = _ASSOC_FANOUT * (_ASSOC_FANOUT - 1) // 2
    assert len(ca._edges) == expected                 # C(8,2)=28, not C(n,2)


def test_confident_hits_byte_identical_with_co_access_on_off():
    """The served hits are unchanged by the write accrual (golden)."""
    index, reader = _confident_index()
    base = _pipe(index=index, reader=reader).recall("q", agent_id=AGENT)
    withca = _pipe(index=index, reader=reader, co_access=FakeCoAccess(),
                   co_access_enabled=True).recall("q", agent_id=AGENT)
    assert base.hits == withca.hits


def test_co_access_write_fault_is_fail_open():
    """A raising record_co_access never breaks the trusted recall."""
    index, reader = _confident_index()
    ca = FakeCoAccess(raises=True)
    r = _pipe(index=index, reader=reader, co_access=ca,
              co_access_enabled=True).recall("q", agent_id=AGENT)
    assert r.state == CONFIDENT and r.hits[0].episode_id == 1
    assert ca.writes == 1                              # the raising call was attempted


def test_abstain_writes_no_edges():
    index, reader = _abstain_index()
    ca = FakeCoAccess()
    r = _pipe(index=index, reader=reader, co_access=ca,
              co_access_enabled=True).recall("q", agent_id=AGENT)
    assert r.state == ABSTAIN
    assert ca.writes == 0                              # no hits ⇒ no call


# ════════════════════════ PHASE 2 — neighbor surfacing ═══════════════════════

def test_associations_off_by_default_empty_tuple():
    """associations_enabled defaults False ⇒ () even with a populated edge table."""
    index, reader = _confident_index()
    ca = FakeCoAccess()
    reader.add(50, "neighbor", trust="established")
    ca._edges[(1, 50)] = 5.0                           # a strong edge to the gold hit
    r = _pipe(index=index, reader=reader, co_access=ca,
              co_access_enabled=True).recall("q", agent_id=AGENT)  # write on, READ off
    assert r.state == CONFIDENT and r.associations == ()


def test_confident_surfaces_neighbor_on_associations_channel():
    index, reader = _confident_index()
    ca = FakeCoAccess()
    reader.add(50, "the co-accessed neighbor", trust="established")
    ca._edges[(1, 50)] = 4.0                           # weight ≥ _ASSOC_MIN_WEIGHT (2.0)
    r = _pipe(index=index, reader=reader, co_access=ca,
              associations_enabled=True).recall("q", agent_id=AGENT)
    assert r.state == CONFIDENT
    assert [a.episode_id for a in r.associations] == [50]
    a = r.associations[0]
    assert a.text == "the co-accessed neighbor" and a.weight == 4.0
    # never merged into the trusted channel
    assert 50 not in {h.episode_id for h in r.hits}


def test_min_weight_floor_drops_noise_edges():
    """A single co-occurrence (weight < _ASSOC_MIN_WEIGHT) is noise — never surfaced."""
    index, reader = _confident_index()
    ca = FakeCoAccess()
    reader.add(50, "weak", trust="established")
    reader.add(51, "strong", trust="established")
    ca._edges[(1, 50)] = _ASSOC_MIN_WEIGHT - 0.5       # below floor
    ca._edges[(1, 51)] = _ASSOC_MIN_WEIGHT             # at the floor (inclusive)
    r = _pipe(index=index, reader=reader, co_access=ca,
              associations_enabled=True).recall("q", agent_id=AGENT)
    assert [a.episode_id for a in r.associations] == [51]


def test_unservable_neighbor_is_filtered():
    """A neighbor whose row has decayed (not is_servable) is re-checked out — a
    co-access edge never resurrects a row past its lifecycle."""
    index, reader = _confident_index()
    ca = FakeCoAccess()
    # a quarantined (never-servable) neighbor with a strong edge
    reader.add(50, "decayed neighbor", trust=QUARANTINED)
    ca._edges[(1, 50)] = 9.0
    r = _pipe(index=index, reader=reader, co_access=ca,
              associations_enabled=True).recall("q", agent_id=AGENT)
    assert r.associations == ()                        # unservable ⇒ dropped


def test_neighbor_already_in_hits_excluded():
    """A co-accessed eid that is ALSO a served hit is not duplicated onto associations."""
    index, reader = _confident_index()                 # hits = {1,2,3}
    ca = FakeCoAccess()
    ca._edges[(1, 2)] = 7.0                            # 2 is already a hit
    r = _pipe(index=index, reader=reader, co_access=ca,
              associations_enabled=True).recall("q", agent_id=AGENT)
    assert r.associations == ()


def test_associations_capped_at_assoc_top_n():
    index, reader = _confident_index()
    ca = FakeCoAccess()
    # _ASSOC_TOP_N + 3 strong neighbors, descending weight
    for k in range(_ASSOC_TOP_N + 3):
        nid = 100 + k
        reader.add(nid, f"n{nid}", trust="established")
        ca._edges[(1, nid)] = 50.0 - k
    r = _pipe(index=index, reader=reader, co_access=ca,
              associations_enabled=True).recall("q", agent_id=AGENT)
    assert len(r.associations) == _ASSOC_TOP_N
    # the strongest survive (weight-desc)
    assert [a.episode_id for a in r.associations] == [100 + k for k in range(_ASSOC_TOP_N)]


def test_associations_read_records_no_exposure():
    """The neighbor read is READ-ONLY: it must NOT record exposure for the neighbor
    (no liveness refresh — the exposure-resurrection belt-ordering invariant)."""
    index, reader = _confident_index()
    ca = FakeCoAccess()
    ledger = FakeLedger()
    reader.add(50, "neighbor", trust="established")
    ca._edges[(1, 50)] = 4.0
    r = _pipe(index=index, reader=reader, co_access=ca, associations_enabled=True,
              ledger=ledger).recall("q", agent_id=AGENT)
    assert [a.episode_id for a in r.associations] == [50]
    exposed = {eid for ex in ledger.exposures for eid, _m in ex["items"]}
    assert 50 not in exposed                           # neighbor never exposed
    assert exposed == {h.episode_id for h in r.hits}   # only the trusted hits


def test_confident_hits_byte_identical_with_associations_on_off():
    index, reader = _confident_index()
    base = _pipe(index=index, reader=reader).recall("q", agent_id=AGENT)
    ca = FakeCoAccess()
    reader.add(50, "neighbor", trust="established")
    ca._edges[(1, 50)] = 4.0
    witha = _pipe(index=index, reader=reader, co_access=ca,
                  associations_enabled=True).recall("q", agent_id=AGENT)
    assert base.hits == witha.hits                     # hits untouched by associations
    assert base.associations == () and witha.associations != ()


def test_abstain_carries_empty_associations():
    index, reader = _abstain_index()
    ca = FakeCoAccess()
    ca._edges[(1, 2)] = 9.0
    r = _pipe(index=index, reader=reader, co_access=ca,
              associations_enabled=True).recall("q", agent_id=AGENT)
    assert r.state == ABSTAIN and r.associations == ()


def test_associations_read_fault_is_fail_open():
    """A raising co_access_neighbors never breaks the trusted recall — associations=()."""
    index, reader = _confident_index()
    ca = FakeCoAccess(raises=True)
    r = _pipe(index=index, reader=reader, co_access=ca,
              associations_enabled=True).recall("q", agent_id=AGENT)
    assert r.state == CONFIDENT and r.hits[0].episode_id == 1
    assert r.associations == ()


# ── carrier invariants (RecallAssoc distinct; associations ride CONFIDENT) ─────
def test_recallassoc_frozen_distinct_from_hit():
    a = RecallAssoc(episode_id=7, text="related", weight=3.0)
    assert a.trust == QUARANTINED and a.ts == 0
    assert not isinstance(a, RecallHit)                # a different type by design
    import pytest
    with pytest.raises(Exception):
        a.weight = 9.0                                 # frozen


def test_associations_constructable_on_confident():
    asc = (RecallAssoc(1, "r", 2.0),)
    c = RecallResult(CONFIDENT, "t", (RecallHit(2, "h", 0.9),), 0.1, 0.5,
                     associations=asc)
    assert c.associations == asc


def test_associations_default_empty_on_classmethods():
    assert RecallResult.empty("t").associations == ()
    assert RecallResult.abstain("t", 0.5, 0.0).associations == ()
