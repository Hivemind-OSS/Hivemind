"""B4: Mem0Backend over the Mem0Client swap seam. A deterministic FakeMem0Client (token-Jaccard,
no model/qdrant) pins the orchestrator-in-loop mapping: propose HOLDS (mem0 has no quarantine, so
an un-approved fact is never written), commit ADDs (infer=False), recall SEARCHes → RecallObs.
mem0 lacks honest abstention — it abstains ONLY on an empty store, never via a confidence gate —
so coverage stays ~1.0 and that asymmetry vs Hivemind is the whole point of the comparison.

A separate env-gated test exercises the REAL mem0 (cached MiniLM, local Qdrant) to prove the
RealMem0Client plumbing runs offline with zero API."""
from __future__ import annotations

import re

import pytest

from hive.research.bench.backends import MemoryBackend, RecallObs
from hive.research.bench.mem0_backend import Mem0Backend, Mem0Client, Mem0Hit


# ── a deterministic, model-free Mem0Client double ──────────────────────────────
def _toks(s: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", s.lower()))


class FakeMem0Client:
    """Faithful to mem0's flat shared store: add appends, search returns the top-k by a
    deterministic Jaccard score (NO threshold — like cosine top-k, it returns low-overlap hits
    too, so the store only 'abstains' when EMPTY), reset drops everything."""

    def __init__(self) -> None:
        self._mems: list[tuple[str, str]] = []   # (id, text), insertion order
        self._n = 0

    def add(self, text: str) -> str:
        self._n += 1
        mid = f"m{self._n}"
        self._mems.append((mid, text))
        return mid

    def search(self, query: str, *, limit: int) -> list[Mem0Hit]:
        q = _toks(query)
        scored = []
        for i, (mid, text) in enumerate(self._mems):
            t = _toks(text)
            jac = len(q & t) / len(q | t) if (q or t) else 0.0
            scored.append((jac, i, mid))
        scored.sort(key=lambda r: (-r[0], r[1]))          # score desc, stable by insertion
        return [Mem0Hit(id=mid, score=s) for s, _i, mid in scored[:limit]]

    def reset(self) -> None:
        self._mems.clear()
        self._n = 0


def _backend(**kw) -> Mem0Backend:
    return Mem0Backend(FakeMem0Client(), **kw)


# ── conformance + the read/propose/commit/reset contract ───────────────────────
def test_client_and_backend_conform():
    assert isinstance(FakeMem0Client(), Mem0Client)
    assert isinstance(_backend(), MemoryBackend)


def test_recall_on_empty_store_abstains():
    obs = _backend().recall("sub-a", "anything at all")
    assert obs.abstained is True and obs.ranked_ids == () and obs.confidence == 0.0


def test_propose_writes_nothing_until_commit():
    b = _backend()
    text = "staging reads config from /etc/app/staging.conf"
    p = b.propose("sub-a", text, source_id="s1")
    assert p.proposal_id and p.text == text and p.source_id == "s1"
    # mem0 has no quarantine: a held proposal is NOT in the store ⇒ recall still abstains
    assert b.recall("sub-a", text).abstained is True


def test_commit_makes_it_recallable_and_returns_the_mem_id():
    b = _backend()
    text = "the migration renamed users.email to users.email_address"
    mid = b.commit(b.propose("sub-a", text, source_id="s1"), approver="orchestrator")
    obs = b.recall("sub-b", "what did the migration rename on users")   # different seat, shared store
    assert obs.abstained is False and mid in obs.ranked_ids and obs.confidence > 0.0


def test_denied_proposal_never_serves():
    b = _backend()
    b.propose("sub-a", "a candidate the orchestrator rejects", source_id="s1")   # never committed
    assert b.recall("sub-a", "a candidate the orchestrator rejects").abstained is True


def test_seat_is_ignored_flat_shared_store():
    b = _backend()
    mid = b.commit(b.propose("sub-a", "redis runs on port 6379", source_id="s1"),
                   approver="orchestrator")
    # a wholly different subagent seat sees the same committed memory
    assert mid in b.recall("sub-z", "what port does redis run on").ranked_ids


def test_top_k_bounds_the_ranked_window():
    b = _backend(top_k=2)
    for i in range(4):
        b.commit(b.propose("sub-a", f"server number {i} listens on a port", source_id="s"),
                 approver="orchestrator")
    obs = b.recall("sub-a", "which server listens on a port")
    assert obs.abstained is False and len(obs.ranked_ids) <= 2


def test_reset_clears_the_store():
    b = _backend()
    b.commit(b.propose("sub-a", "use BEGIN IMMEDIATE for the writer lane", source_id="s1"),
             approver="orchestrator")
    assert b.recall("sub-a", "writer lane transaction").abstained is False
    b.reset()
    assert b.recall("sub-a", "writer lane transaction").abstained is True


# ── confidence = clamped top-score (mem0's native proxy; cosine can stray outside [0,1]) ──
class _ScoreStub:
    """A Mem0Client returning one hit at a fixed (possibly out-of-range) score."""
    def __init__(self, score: float) -> None:
        self._score = score

    def add(self, text: str) -> str:
        return "m1"

    def search(self, query: str, *, limit: int) -> list[Mem0Hit]:
        return [Mem0Hit(id="m1", score=self._score)]

    def reset(self) -> None:
        pass


@pytest.mark.parametrize("raw, expected", [(1.7, 1.0), (-0.3, 0.0), (0.42, 0.42)])
def test_confidence_clamped_into_unit_interval(raw, expected):
    obs = Mem0Backend(_ScoreStub(raw)).recall("sub-a", "q")
    assert obs.confidence == pytest.approx(expected)
    assert obs.top_score == pytest.approx(raw)          # raw native score preserved for provenance


# ── the REAL mem0 offline (skips unless mem0 + a cached ST model are present) ───
def _cached_minilm():
    """A small ST model already in the HF cache, so this never downloads. None ⇒ skip."""
    try:
        from huggingface_hub import try_to_load_from_cache
        name = "sentence-transformers/all-MiniLM-L6-v2"
        hit = try_to_load_from_cache(name, "config.json")
        return name if isinstance(hit, str) else None
    except Exception:
        return None


@pytest.mark.skipif(_cached_minilm() is None, reason="no cached ST model / mem0 for the real client")
def test_real_mem0_client_runs_offline(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    from hive.research.bench.mem0_backend import RealMem0Client
    client = RealMem0Client(model=_cached_minilm(), dims=384, path=str(tmp_path))  # MiniLM native dim (384)
    b = Mem0Backend(client)
    mid = b.commit(b.propose("sub-a", "the dev server runs on port 8080", source_id="s1"),
                   approver="orchestrator")
    obs = b.recall("sub-b", "what port does the dev server use")
    assert obs.abstained is False and mid in obs.ranked_ids and 0.0 <= obs.confidence <= 1.0
    b.reset()
    assert b.recall("sub-a", "what port does the dev server use").abstained is True
