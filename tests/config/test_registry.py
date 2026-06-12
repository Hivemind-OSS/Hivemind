"""P1.9 — M11 provider registries + build_* + build_gate (floor-by-identity).

The three swap seams are plain greppable dicts; build_* fail fast on a typo'd seam;
build_gate is the ONE located owner that passes the frozen cfg.recall object BY IDENTITY
into the abstention gate (CONFIG_DRIFT killed structurally — the gate-holds-config-by-identity must-fix).
"""
from __future__ import annotations

import numpy as np
import pytest

from hive.app.config import Config
from hive.app import registry
from hive.domain.recall import NormalizedEntropyGate


# ── build_* defaults satisfy their port ───────────────────────────────────────
def test_build_index_default_and_authoritative():
    cfg = Config.load(db_path=":memory:")
    idx = registry.build_index(cfg)
    # exhaustive is AUTHORITATIVE, never silently ANN (the silent-ANN-fallback trap) — build-time postcondition
    assert idx.is_authoritative() is True


def test_build_index_authoritative_true():
    cfg = Config.load(db_path=":memory:", index={"backend": "exhaustive"})
    assert registry.build_index(cfg).is_authoritative() is True


# ── Config itself rejects a typo'd seam at construction (first line of defence) ─
def test_config_rejects_unknown_embedding_provider():
    with pytest.raises(ValueError, match=r"provider|valid"):
        Config.load(embedding={"provider": "ghost-provider"})


# ── build_* keep their OWN fail-fast (belt) — proven by popping a live key ─────
def test_build_embedder_unknown_provider_fails_fast():
    # a Config built while the key was present, then the registry loses it:
    cfg = Config.load(db_path=":memory:")
    saved = registry.EMBEDDING_PROVIDERS.pop(cfg.embedding.provider)
    try:
        with pytest.raises(ValueError, match=r"valid|provider"):
            registry.build_embedder(cfg)
    finally:
        registry.EMBEDDING_PROVIDERS[cfg.embedding.provider] = saved


# ── the SWAP MANDATE proof: a new adapter, zero core change ───────────────────
class _LoopbackEmbedder:
    """A second TextEmbedder registered at runtime — proves a swap needs only
    registry.py + this adapter, no core edit."""
    w_version = 1

    def encode(self, text: str) -> "np.ndarray":
        return np.ones(256, dtype=np.float32) / np.sqrt(256.0)

    def encode_batch(self, texts):
        return np.stack([self.encode(t) for t in texts])


def test_second_adapter_swaps_with_no_core_change():
    registry.EMBEDDING_PROVIDERS["remote_loopback"] = lambda cfg: _LoopbackEmbedder()
    try:
        cfg = Config.load(db_path=":memory:", embedding={"provider": "remote_loopback"})
        emb = registry.build_embedder(cfg)
        assert isinstance(emb, _LoopbackEmbedder)
        assert emb.w_version == 1
    finally:
        registry.EMBEDDING_PROVIDERS.pop("remote_loopback", None)


# ── floor-by-identity: the gate holds the SAME frozen recall object ───────────
def test_gate_reads_same_frozen_recall_object():
    cfg = Config.load(db_path=":memory:")
    gate = registry.build_gate(cfg)
    assert isinstance(gate, NormalizedEntropyGate)
    assert gate._recall is cfg.recall          # IDENTITY, not a float copy (CONFIG_DRIFT killed)
    assert gate.h_frac_max == cfg.recall.H_frac_max


def test_gate_floor_tracks_config_value():
    cfg = Config.load(db_path=":memory:", recall={"H_frac_max": 0.3})
    gate = registry.build_gate(cfg)
    assert gate.h_frac_max == 0.3
    assert gate._recall is cfg.recall
