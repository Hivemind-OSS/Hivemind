"""P1.9 — M11 provider registries + build_* + build_gate (floor-by-identity).

The three swap seams are plain greppable dicts; build_* fail fast on a typo'd seam;
build_gate is the ONE located owner that passes the frozen cfg.recall object BY IDENTITY
into the abstention gate (CONFIG_DRIFT killed structurally — the gate-holds-config-by-identity must-fix).
"""

from __future__ import annotations

import pytest

from hive.adapters import index_exhaustive
from hive.app.config import Config
from hive.app import registry
from hive.domain.recall import AbsoluteRelevanceGate


# ── build_* defaults satisfy their port ───────────────────────────────────────
def test_build_index_default_and_authoritative():
    idx = registry.build_index(dim=768)
    # exhaustive is AUTHORITATIVE, never silently ANN (the silent-ANN-fallback trap) — build-time postcondition
    assert idx.is_authoritative() is True


# ── Config itself rejects a typo'd seam at construction (first line of defence) ─
def test_config_rejects_unknown_embedding_provider():
    with pytest.raises(ValueError, match=r"provider|valid"):
        Config.load(embedding={"provider": "ghost-provider"})


# ── build_embedder keeps its OWN fail-fast (belt), independent of Config ───────
class _StubEmbeddingCfg:
    """A thin cfg whose embedding.provider bypasses Config.__post_init__ — proves the
    belt-level fail-fast in build_embedder survives the registry collapse on its own."""

    def __init__(self, provider: str) -> None:
        self.embedding = type("E", (), {"provider": provider, "model": "m"})()


def test_build_embedder_rejects_unknown_provider(monkeypatch):
    # neuter the local_st ctor so the ONLY source of a ValueError is the provider guard
    # itself — dropping the `provider != 'local_st'` raise turns this into a DID-NOT-RAISE
    # red, not an incidental downstream crash.
    monkeypatch.setattr(registry, "_build_local_st", lambda *_a, **_k: object())
    with pytest.raises(ValueError, match=r"valid|provider|ghost"):
        registry.build_embedder(_StubEmbeddingCfg("ghost"))


# ── the Law-5 never-flip postcondition fires if the index declares non-authoritative ─
def test_build_index_postcondition_fires(monkeypatch):
    class _FakeIdx:
        def is_authoritative(self):
            return False

    monkeypatch.setattr(index_exhaustive, "build_index", lambda *_a, **_k: _FakeIdx())
    with pytest.raises(AssertionError, match=r"authoritative"):
        registry.build_index(dim=4)


# ── floor-by-identity: the gate holds the SAME frozen recall object ───────────
def test_gate_reads_same_frozen_recall_object():
    cfg = Config.load(db_path=":memory:")
    gate = registry.build_gate(cfg)
    assert isinstance(gate, AbsoluteRelevanceGate)
    assert (
        gate._recall is cfg.recall
    )  # IDENTITY, not a float copy (CONFIG_DRIFT killed)
    assert gate.tau_serve == cfg.recall.tau_serve


def test_gate_floor_tracks_config_value():
    cfg = Config.load(db_path=":memory:", recall={"tau_serve": 0.55})
    gate = registry.build_gate(cfg)
    assert gate.tau_serve == 0.55
    assert gate._recall is cfg.recall


def test_gate_carries_k_min_and_keeps_identity():
    # build_gate threads k_min off the frozen cfg.recall (by from_recall's getattr) AND
    # still holds the recall object by identity — the floor and the gate share one source.
    cfg = Config.load(db_path=":memory:", recall={"k_min": 2})
    gate = registry.build_gate(cfg)
    assert gate.k_min == 2
    assert gate._recall is cfg.recall
