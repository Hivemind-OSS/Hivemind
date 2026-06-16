"""SELF-QUARANTINE resurfacing — the read channel that hands a seat back its OWN
unpromoted quarantined captures as labeled drafts, strictly separate from the
trusted ``hits``.

The load-bearing contracts proven here (against fakes only — hash-speed, no
SQLite/torch/clock):
  * default OFF ⇒ byte-inert: the draft_reader is NEVER touched;
  * the trusted result is untouched — on ABSTAIN, ``hits`` stays () AND the miss is
    STILL recorded (demand accrual preserved); on CONFIDENT, ``hits`` is identical
    to the no-drafts run;
  * seat-scoping is the whole safety model: only ``writer == agent_id`` rows surface;
  * the relevance floor ``draft_tau`` drops weak matches;
  * resurfacing records NO exposure for the draft rows (no liveness refresh — the
    quarantine TTL clock and the learning loop are untouched; cf. the
    exposure-resurrection belt-ordering rule);
  * fail-open: a draft_reader that raises never breaks recall;
  * the cold empty-index path still carries drafts (only quarantined rows exist yet);
  * top-N cap; and the RecallResult/RecallDraft carrier invariants (drafts ride any
    state; the never-hallucinate biconditional still binds ``hits`` only).
"""
from __future__ import annotations


import numpy as np
import pytest

from hive.domain.lifecycle import QUARANTINED
from hive.domain.models import (
    ABSTAIN, CONFIDENT, EMPTY_NO_DATA, RecallDraft, RecallHit,
    RecallResult,
)
from hive.domain.recall import NormalizedEntropyGate, RecallPipeline
from tests.fakes._fakes import (
    FakeEpisodeReader, FakeIndex, FakeLedger, FakeQuarantineReader, FakeScanner,
)

D = 16
AGENT = "seat-A"


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


def _pipe(*, index, reader, query_vec=None, draft_reader=None,
          drafts_enabled=False, draft_tau=0.6, recall_top_n=10, h=0.5,
          beta=16.0, ledger=None, clock_now=None, quarantine_ttl_s=10**9):
    return RecallPipeline(
        embedder=_StubProvider(_e(0) if query_vec is None else query_vec),
        index=index, gate=NormalizedEntropyGate(h, beta),
        reader=reader, recall_top_n=recall_top_n,
        ledger=ledger if ledger is not None else FakeLedger(),
        clock_now=clock_now or (lambda: 0), scanner=FakeScanner(),
        provisional_ttl_s=10**9,
        draft_reader=draft_reader, drafts_enabled=drafts_enabled,
        draft_tau=draft_tau, quarantine_ttl_s=quarantine_ttl_s)


def _abstain_index(reader=None):
    """4 candidates with IDENTICAL cosine ⟹ uniform mass ⟹ max entropy ⟹ abstain."""
    index = FakeIndex()
    reader = reader or FakeEpisodeReader()
    for eid in (1, 2, 3, 4):
        index.add(eid, _cos_vec(0.0))
        reader.add(eid, f"trusted-{eid}", trust="established")
    return index, reader


def _confident_index(reader=None):
    """gold(cos1) + two weak distractors ⟹ peaked ⟹ CONFIDENT."""
    index = FakeIndex()
    reader = reader or FakeEpisodeReader()
    for eid, vec, text in ((1, _e(0), "the gold memory"),
                           (2, _cos_vec(0.1), "distractor a"),
                           (3, _cos_vec(0.05), "distractor b")):
        index.add(eid, vec)
        reader.add(eid, text, trust="established")
    return index, reader


def _add_quarantined(qr: FakeQuarantineReader, reader: FakeEpisodeReader,
                     eid: int, *, cos: float, writer: str, ts: int = 0,
                     text: str | None = None) -> None:
    """Plant one live quarantined row in BOTH the scan source (qr) and the text
    resolver (reader, as an approved+quarantined autonomous-capture row)."""
    qr.add(eid, _cos_vec(cos), writer=writer, ts=ts)
    reader.add(eid, text or f"draft-{eid}", trust=QUARANTINED, ts=ts)


# ── default OFF ⇒ byte-inert (the draft_reader is never touched) ──────────────
def test_drafts_off_by_default_reader_never_called():
    index, reader = _confident_index()
    qr = FakeQuarantineReader()
    _add_quarantined(qr, reader, 10, cos=1.0, writer=AGENT)
    # drafts_enabled defaults False even though a draft_reader is wired
    r = _pipe(index=index, reader=reader, draft_reader=qr).recall(
        "q", agent_id=AGENT)
    assert r.state == CONFIDENT and r.drafts == ()
    assert qr.calls == 0                                   # never scanned


def test_drafts_inert_when_reader_none_even_if_enabled():
    index, reader = _confident_index()
    r = _pipe(index=index, reader=reader, drafts_enabled=True,
              draft_reader=None).recall("q", agent_id=AGENT)
    assert r.state == CONFIDENT and r.drafts == ()


# ── abstain + resurface + the miss STILL records (the key contract) ───────────
def test_abstain_resurfaces_drafts_and_still_records_miss():
    qr, ledger = FakeQuarantineReader(), FakeLedger()
    index, reader = _abstain_index()
    _add_quarantined(qr, reader, 10, cos=0.95, writer=AGENT, text="my own draft")
    r = _pipe(index=index, reader=reader, draft_reader=qr, drafts_enabled=True,
              ledger=ledger).recall("q", agent_id=AGENT)
    # trusted channel: refused exactly as today
    assert r.state == ABSTAIN and r.hits == ()
    # draft channel: the seat's own quarantined capture comes back, labeled
    assert [d.episode_id for d in r.drafts] == [10]
    assert r.drafts[0].text == "my own draft"
    assert r.drafts[0].trust == QUARANTINED
    # the miss is STILL recorded — demand keeps accruing toward promotion
    assert [m["miss_type"] for m in ledger.misses] == ["abstained"]


# ── seat-scoping: only the asking seat's OWN rows surface ─────────────────────
def test_seat_scoping_excludes_other_writers():
    qr = FakeQuarantineReader()
    index, reader = _abstain_index()
    _add_quarantined(qr, reader, 10, cos=0.95, writer=AGENT)          # mine
    _add_quarantined(qr, reader, 11, cos=0.99, writer="seat-B")       # teammate's
    r = _pipe(index=index, reader=reader, draft_reader=qr,
              drafts_enabled=True).recall("q", agent_id=AGENT)
    assert [d.episode_id for d in r.drafts] == [10]    # the teammate row is invisible


# ── relevance floor ───────────────────────────────────────────────────────────
def test_draft_tau_floor_drops_weak_matches():
    qr = FakeQuarantineReader()
    index, reader = _abstain_index()
    _add_quarantined(qr, reader, 10, cos=0.90, writer=AGENT)   # above tau
    _add_quarantined(qr, reader, 11, cos=0.50, writer=AGENT)   # below tau=0.6
    r = _pipe(index=index, reader=reader, draft_reader=qr, drafts_enabled=True,
              draft_tau=0.6).recall("q", agent_id=AGENT)
    assert [d.episode_id for d in r.drafts] == [10]


def test_undecidable_cosine_never_surfaces():
    # a non-finite stored vector ⇒ _cosine None ⇒ dropped (fail-closed), never a draft
    qr = FakeQuarantineReader()
    index, reader = _abstain_index()
    bad = _cos_vec(0.95)
    bad[2] = np.nan
    qr.add(20, bad, writer=AGENT)
    reader.add(20, "nan draft", trust=QUARANTINED)
    r = _pipe(index=index, reader=reader, draft_reader=qr,
              drafts_enabled=True).recall("q", agent_id=AGENT)
    assert r.drafts == ()


# ── cold empty-index path still resurfaces (only quarantined rows exist yet) ──
def test_cold_empty_index_resurfaces_drafts_and_records_miss():
    qr, ledger = FakeQuarantineReader(), FakeLedger()
    reader = FakeEpisodeReader()
    _add_quarantined(qr, reader, 10, cos=0.95, writer=AGENT, text="cold draft")
    r = _pipe(index=FakeIndex(), reader=reader, draft_reader=qr,
              drafts_enabled=True, ledger=ledger).recall(
        "q", agent_id=AGENT)
    assert r.state == EMPTY_NO_DATA and r.hits == ()
    assert [d.episode_id for d in r.drafts] == [10]
    assert [m["miss_type"] for m in ledger.misses] == ["no_match"]


# ── CONFIDENT carries hits AND drafts; hits are unchanged; drafts not exposed ─
def test_confident_carries_hits_and_drafts_without_exposing_drafts():
    qr, ledger = FakeQuarantineReader(), FakeLedger()
    index, reader = _confident_index()
    _add_quarantined(qr, reader, 10, cos=0.97, writer=AGENT, text="my draft")
    r = _pipe(index=index, reader=reader, draft_reader=qr, drafts_enabled=True,
              ledger=ledger).recall("q", agent_id=AGENT)
    assert r.state == CONFIDENT
    # the gold leads the trusted hits; the draft eid never enters the hits channel
    hit_ids = [h.episode_id for h in r.hits]
    assert hit_ids[0] == 1 and 10 not in hit_ids
    assert [d.episode_id for d in r.drafts] == [10]
    # exposure (liveness refresh) is recorded for the TRUSTED hits ONLY — the
    # quarantined draft must NOT have its clock refreshed (no learning suppression)
    exposed = {eid for ex in ledger.exposures for eid, _m in ex["items"]}
    assert 10 not in exposed and exposed == set(hit_ids)


def test_confident_hits_byte_identical_with_and_without_drafts():
    index, reader = _confident_index()
    base = _pipe(index=index, reader=reader).recall(
        "q", agent_id=AGENT)
    qr = FakeQuarantineReader()
    _add_quarantined(qr, reader, 10, cos=0.97, writer=AGENT)
    withd = _pipe(index=index, reader=reader, draft_reader=qr,
                  drafts_enabled=True).recall("q", agent_id=AGENT)
    assert base.hits == withd.hits                         # hits untouched by drafts
    assert base.drafts == () and withd.drafts != ()


# ── fail-open: a draft_reader fault never breaks the trusted recall ───────────
class _RaisingQReader:
    def quarantined_candidates(self, *, now, quarantine_ttl_s):
        raise RuntimeError("quarantine scan boom")


def test_draft_reader_raise_is_fail_open():
    index, reader = _confident_index()
    r = _pipe(index=index, reader=reader, draft_reader=_RaisingQReader(),
              drafts_enabled=True).recall("q", agent_id=AGENT)
    assert r.state == CONFIDENT and r.hits[0].episode_id == 1
    assert r.drafts == ()                                  # degraded to none, not a crash


# ── top-N cap (highest cosine first) ──────────────────────────────────────────
def test_drafts_capped_at_recall_top_n():
    qr = FakeQuarantineReader()
    index, reader = _abstain_index()
    # 5 matching rows, descending cosine; cap at 2 ⇒ the two strongest
    for eid, cos in ((10, 0.99), (11, 0.95), (12, 0.90), (13, 0.85), (14, 0.80)):
        _add_quarantined(qr, reader, eid, cos=cos, writer=AGENT)
    r = _pipe(index=index, reader=reader, draft_reader=qr, drafts_enabled=True,
              recall_top_n=2).recall("q", agent_id=AGENT)
    assert [d.episode_id for d in r.drafts] == [10, 11]


def test_drafts_sorted_cosine_desc():
    qr = FakeQuarantineReader()
    index, reader = _abstain_index()
    _add_quarantined(qr, reader, 10, cos=0.80, writer=AGENT)
    _add_quarantined(qr, reader, 11, cos=0.99, writer=AGENT)
    _add_quarantined(qr, reader, 12, cos=0.90, writer=AGENT)
    r = _pipe(index=index, reader=reader, draft_reader=qr,
              drafts_enabled=True).recall("q", agent_id=AGENT)
    assert [d.episode_id for d in r.drafts] == [11, 12, 10]


# ── carrier invariants (drafts ride any state; biconditional binds hits only) ─
def test_recalldraft_frozen_distinct_from_hit():
    d = RecallDraft(episode_id=7, text="draft", sim=0.7)
    assert d.trust == QUARANTINED and d.ts == 0
    assert not isinstance(d, RecallHit)                    # a different type by design
    with pytest.raises(Exception):
        d.sim = 0.9                                        # frozen


def test_drafts_constructable_on_every_state():
    dr = (RecallDraft(1, "d", 0.7),)
    assert RecallResult.empty("t", drafts=dr).drafts == dr
    assert RecallResult.abstain("t", 0.9, 0.0, drafts=dr).drafts == dr
    c = RecallResult(CONFIDENT, "t", (RecallHit(2, "h", 0.9),), 0.1, 0.5, drafts=dr)
    assert c.drafts == dr


def test_drafts_default_empty_on_classmethods():
    assert RecallResult.empty("t").drafts == ()
    assert RecallResult.abstain("t", 0.5, 0.0).drafts == ()


def test_hits_on_non_confident_still_raises_even_with_drafts():
    # drafts are deliberately unconstrained by state; the never-hallucinate
    # biconditional still binds HITS only — a non-CONFIDENT result carrying a hit
    # is unconstructable regardless of drafts.
    with pytest.raises(ValueError):
        RecallResult(ABSTAIN, "t", (RecallHit(1, "x", 0.5),), 0.9, 0.0,
                     drafts=(RecallDraft(2, "d", 0.7),))
