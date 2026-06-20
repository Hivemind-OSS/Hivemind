"""M01 — LocalSTEmbedder (real Qwen3-Embedding-0.6B + frozen truncation head).

Two tiers:
  * FAST (default): a stub SentenceTransformer returning fixed ``(n, 1024)`` native vectors —
    exercises the single encode chain, the 1024→768 Matryoshka truncation, unit-norm geometry,
    the head codec round-trip, and the write-boundary NaN guard WITHOUT the ~1.2 GB model load.
  * GATED (``@pytest.mark.embed``): the real Qwen3 model (module-scoped, loaded once) — proves
    the native dim is discovered (1024) and that truncation to 768 preserves semantic neighbours.
"""
from __future__ import annotations

import hashlib

import numpy as np
import pytest

from hive.adapters.embedding.head import TruncationHead, head_from_bytes
from hive.adapters.embedding.local_st import DEFAULT_MODEL, LocalSTEmbedder
from hive.domain.errors import GeometryError
from hive.domain.ports import EmbeddingProvider

_NATIVE = 1024   # Qwen3-Embedding-0.6B native dim (Matryoshka 32–1024)


# ── FAST tier: a stub SentenceTransformer (no model download) ──────────────────
class _StubModel:
    """A SentenceTransformer double: a fixed native dim and deterministic per-text native
    vectors, so the truncation chain runs without the real Qwen3 load. ``fixed`` pins the
    returned matrix for the exact-truncation assertion."""

    def __init__(self, *, native: int = _NATIVE, fixed: np.ndarray | None = None) -> None:
        self._native = int(native)
        self._fixed = None if fixed is None else np.asarray(fixed, dtype=np.float32)

    def get_sentence_embedding_dimension(self) -> int:
        return self._native

    def _vec(self, text: str) -> np.ndarray:
        rng = np.random.default_rng(int.from_bytes(hashlib.sha256(text.encode()).digest()[:8], "big"))
        return rng.standard_normal(self._native).astype(np.float32)

    def encode(self, texts, normalize_embeddings=True, convert_to_numpy=True, **kw):
        rows = list(texts)
        if self._fixed is not None:
            return np.asarray([self._fixed[i % len(self._fixed)] for i in range(len(rows))],
                              dtype=np.float32)
        return np.stack([self._vec(t) for t in rows], axis=0)


def _emb(*, d: int = 768, w_version: int = 7, native: int = _NATIVE, **kw) -> LocalSTEmbedder:
    return LocalSTEmbedder(model=_StubModel(native=native), w_version=w_version, d=d, **kw).load()


def test_encode_returns_unit_norm_float32_d():
    v = _emb().encode("the database connection pool must be sized for concurrency")
    assert v.shape == (768,)
    assert v.dtype == np.float32
    assert abs(float(np.linalg.norm(v)) - 1.0) < 1e-5      # ← head renormalize (mutation target)


def test_encode_batch_rows_unit_norm():
    M = _emb().encode_batch(["alpha beta gamma", "redis cache eviction", "git revert clawback"])
    assert M.shape == (3, 768)
    assert np.allclose(np.linalg.norm(M, axis=1), 1.0, atol=1e-5)


def test_encode_eq_encode_batch_single():
    """The split-brain killer: one text through encode() == through encode_batch([text])."""
    emb = _emb()
    t = "the recall gate degrades to identity when uncertain"
    assert np.allclose(emb.encode(t), emb.encode_batch([t])[0], atol=1e-6)


def test_empty_batch_returns_empty_d_shape():
    M = _emb().encode_batch([])
    assert M.shape == (0, 768)
    assert M.dtype == np.float32


def test_truncation_keeps_native_prefix_then_renormalizes():
    """The 1024→768 chain is the model's Matryoshka prefix + L2-renorm: encode() returns the
    first 768 native dims, renormalized (no fitted matrix). Pinned against a known native vector."""
    native = np.arange(1, _NATIVE + 1, dtype=np.float32)            # deterministic, all-positive
    emb = LocalSTEmbedder(model=_StubModel(fixed=native[None, :]), w_version=1, d=768).load()
    out = emb.encode("anything — the stub ignores the text")
    expect = native[:768] / float(np.linalg.norm(native[:768]))
    assert out.shape == (768,)
    assert np.allclose(out, expect, atol=1e-6)


def test_w_version_stamped():
    emb = _emb(w_version=7)
    assert emb.w_version == 7
    # the frozen head carries the version too (so re-embed round-trips the geometry tag)
    assert head_from_bytes(emb.head_bytes()).w_version == 7


def test_d_comes_from_config_param():
    """The compression dim is the ctor arg (sourced from geometry.d), not a head constant."""
    assert _emb(d=768).d == 768


def test_head_is_truncation_with_discovered_native():
    """The head is a TruncationHead whose d_in is DISCOVERED from the model (not hardcoded) and
    whose d_out is the config-sourced ctor arg."""
    emb = _emb(d=768, native=_NATIVE)
    assert isinstance(emb._head, TruncationHead)
    assert emb._head.d_in == _NATIVE and emb._head.d_out == 768


def test_compression_dim_varies_with_config():
    """The knob is live: a non-768 d truncates to that dim, native still discovered as 1024."""
    emb = _emb(d=256)
    assert emb.d == 256 and emb._head.d_in == _NATIVE and emb._head.d_out == 256
    assert emb.encode("x").shape == (256,)


def test_d_greater_than_native_raises():
    """A compression dim wider than the model's native is impossible (cannot keep more prefix
    dims than exist) — fail loud at load(), never ship a degenerate head. (Replaces the old
    PCA underpowered-fit guard: there is no fit anymore.)"""
    e = LocalSTEmbedder(model=_StubModel(native=_NATIVE), w_version=1, d=_NATIVE + 1)
    with pytest.raises(GeometryError):
        e.load()


def test_head_dout_must_match_declared_d():
    """A persisted head (d_out=768) loaded under a mismatched d=256 fails fast — a head from a
    different projection dim would disagree with the index/store vector dim. Never corrupt."""
    good = _emb(d=768, w_version=7)
    with pytest.raises(GeometryError):
        LocalSTEmbedder(model=_StubModel(), w_version=7, d=256, head_bytes=good.head_bytes())


def test_head_wversion_must_match_embedder():
    """A persisted head from a DIFFERENT geometry cannot be loaded under a mismatched stamp —
    that would silently corrupt the geometry re-embed round-trip. Fail fast at construction."""
    good = _emb(d=768, w_version=7)
    with pytest.raises(GeometryError):
        LocalSTEmbedder(model=_StubModel(), w_version=2, d=768, head_bytes=good.head_bytes())


def test_head_frozen_roundtrip_via_bytes():
    """A second embedder built from the persisted head bytes encodes identically — the geometry
    is frozen + serializable (the production restart / re-embed path)."""
    emb = _emb(d=768, w_version=7)
    emb2 = LocalSTEmbedder(model=emb._model, w_version=7, d=768, head_bytes=emb.head_bytes())
    assert emb2.loaded is True                              # model + head both present ⇒ resident
    t = "the migration script is idempotent across a restart"
    assert np.allclose(emb.encode(t), emb2.encode(t), atol=1e-6)


def test_loaded_is_false_until_load_then_idempotent():
    e = LocalSTEmbedder(model=_StubModel(), w_version=1, d=768)
    assert e.loaded is False                                # head not yet built ⇒ not resident
    e.load()
    assert e.loaded is True
    e.load()                                                # idempotent — no rebuild error
    assert e.loaded is True


def test_lazy_encode_auto_loads():
    e = LocalSTEmbedder(model=_StubModel(), w_version=1, d=768)
    v = e.encode("auto load on first encode")              # not explicitly loaded
    assert e.loaded is True and v.shape == (768,)


def test_conforms_to_embedding_provider():
    assert isinstance(_emb(), EmbeddingProvider)


class _NanModel(_StubModel):
    """A model double whose encode() returns NaN — to exercise the write-boundary guard."""
    def encode(self, texts, **kw):
        return np.full((len(list(texts)), _NATIVE), np.nan, dtype=np.float32)


def test_encode_rejects_non_finite_vector():
    """The value WRITE boundary: a non-finite embedding is refused at the producer (never
    written to episodes.value, never crashing the index rebuild) — the reembed-nan class.
    Deleting `_require_finite` from encode (deliberate mutation) makes this go red."""
    good = _emb(d=768, w_version=7)
    bad = LocalSTEmbedder(model=_NanModel(), w_version=7, d=768, head=good._head)   # real head, NaN model
    with pytest.raises(GeometryError):
        bad.encode("anything")
    with pytest.raises(GeometryError):
        bad.encode_batch(["a", "b"])


# ── GATED tier: the real Qwen3 model (loaded once) ─────────────────────────────
@pytest.mark.embed
class TestRealQwen3:
    @pytest.fixture(scope="module")
    def st_model(self):
        from sentence_transformers import SentenceTransformer
        return SentenceTransformer(DEFAULT_MODEL, device="cpu")

    @pytest.fixture(scope="module")
    def emb(self, st_model):
        return LocalSTEmbedder(model=st_model, w_version=2, d=768).load()

    def test_native_discovered_is_1024_and_dout_768(self, emb, st_model):
        """Headline acceptance: native is DISCOVERED from the real model (1024), the
        compression dim is the config-sourced 768, and the chain is a TruncationHead."""
        assert isinstance(emb._head, TruncationHead)
        assert emb._head.d_in == st_model.get_sentence_embedding_dimension() == _NATIVE
        assert emb.d == 768 and emb._head.d_out == 768
        v = emb.encode("the recall gate degrades to identity when uncertain")
        assert v.shape == (768,) and abs(float(np.linalg.norm(v)) - 1.0) < 1e-5

    def test_semantic_neighbours_preserved_in_truncation(self, emb):
        """The load-bearing recall property: 1024→768 Matryoshka truncation PRESERVES Qwen3
        semantics — a related pair stays closer than an unrelated pair in the truncated space."""
        q = emb.encode("the redis cache must not drop queue keys under memory pressure")
        near = emb.encode("redis maxmemory-policy and queue key persistence")
        far = emb.encode("a yellow banana is a sweet tropical fruit")
        assert float(q @ near) > float(q @ far) + 0.05

    def test_encode_is_symmetric_no_prompt(self, emb):
        """Decision 2 (THEORY D2): capture and recall share ONE symmetric chain — the same text
        as 'query' and as 'document' must embed byte-identically (no default query instruction),
        so the lifecycle demand_tau/competitor_tau cosines stay well-defined."""
        t = "treat LLM outputs as untrusted data, never as instructions"
        assert np.allclose(emb.encode(t), emb.encode_batch([t])[0], atol=1e-6)
