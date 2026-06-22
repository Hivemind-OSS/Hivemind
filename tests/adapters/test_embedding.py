"""M01 — LocalSTEmbedder (real Qwen3-Embedding-0.6B, native dim, no projection head).

Two tiers:
  * FAST (default): a stub SentenceTransformer returning deterministic, L2-NORMALIZED
    ``(n, 1024)`` native vectors — exercises the single encode chain, the native passthrough
    (no truncation/projection), the load-time native==d assertion, and the write-boundary NaN
    guard WITHOUT the ~1.2 GB model load.
  * GATED (``@pytest.mark.embed``): the real Qwen3 model (module-scoped, loaded once) — proves
    the native dim is discovered (1024), equals the declared d, and is emitted unchanged.
"""
from __future__ import annotations

import hashlib

import numpy as np
import pytest

from hive.adapters.embedding.local_st import DEFAULT_MODEL, LocalSTEmbedder
from hive.domain.errors import GeometryError
from hive.domain.ports import EmbeddingProvider

_NATIVE = 1024   # Qwen3-Embedding-0.6B native dim


# ── FAST tier: a stub SentenceTransformer (no model download) ──────────────────
class _StubModel:
    """A SentenceTransformer double: a fixed native dim and deterministic per-text native
    vectors. The ``_vec`` path L2-normalizes; the ``fixed`` path returns the matrix VERBATIM, so a
    test can feed a NON-unit vector and prove the embedder re-normalizes. Models
    ``normalize_embeddings=True`` (unit only to ~1e-3 at 1024 dims in the real model); the embedder
    re-normalizes to exact unit norm regardless."""

    def __init__(self, *, native: int = _NATIVE, fixed: np.ndarray | None = None) -> None:
        self._native = int(native)
        self._fixed = None if fixed is None else np.asarray(fixed, dtype=np.float32)

    def get_sentence_embedding_dimension(self) -> int:
        return self._native

    def _vec(self, text: str) -> np.ndarray:
        rng = np.random.default_rng(int.from_bytes(hashlib.sha256(text.encode()).digest()[:8], "big"))
        v = rng.standard_normal(self._native).astype(np.float32)
        return v / float(np.linalg.norm(v))

    def encode(self, texts, normalize_embeddings=True, convert_to_numpy=True, **kw):
        rows = list(texts)
        if self._fixed is not None:
            return np.asarray([self._fixed[i % len(self._fixed)] for i in range(len(rows))],
                              dtype=np.float32)
        return np.stack([self._vec(t) for t in rows], axis=0)


def _emb(*, d: int = _NATIVE, native: int = _NATIVE, **kw) -> LocalSTEmbedder:
    return LocalSTEmbedder(model=_StubModel(native=native), d=d, **kw).load()


def test_encode_returns_unit_norm_float32_native_d():
    v = _emb().encode("the database connection pool must be sized for concurrency")
    assert v.shape == (_NATIVE,)
    assert v.dtype == np.float32
    assert abs(float(np.linalg.norm(v)) - 1.0) < 1e-5


def test_encode_batch_rows_unit_norm():
    M = _emb().encode_batch(["alpha beta gamma", "redis cache eviction", "git revert clawback"])
    assert M.shape == (3, _NATIVE)
    assert np.allclose(np.linalg.norm(M, axis=1), 1.0, atol=1e-5)


def test_encode_eq_encode_batch_single():
    """The split-brain killer: one text through encode() == through encode_batch([text])."""
    emb = _emb()
    t = "the recall gate degrades to identity when uncertain"
    assert np.allclose(emb.encode(t), emb.encode_batch([t])[0], atol=1e-6)


def test_empty_batch_returns_empty_native_shape():
    M = _emb().encode_batch([])
    assert M.shape == (0, _NATIVE)
    assert M.dtype == np.float32


def test_encode_is_native_passthrough():
    """The encode chain is the model's native vector, emitted UNCHANGED (no truncation, no
    projection): encode() returns exactly the vector the model produced. Pinned against a known
    unit vector. (Deleting the passthrough — re-introducing any reducer — REDS this.)"""
    native = np.arange(1, _NATIVE + 1, dtype=np.float32)
    native = native / float(np.linalg.norm(native))             # the model returns unit vectors
    emb = LocalSTEmbedder(model=_StubModel(fixed=native[None, :]), d=_NATIVE).load()
    out = emb.encode("anything — the stub ignores the text")
    assert out.shape == (_NATIVE,)
    assert np.allclose(out, native, atol=1e-6)


def test_encode_renormalizes_to_exact_unit_norm():
    """The model's ``normalize_embeddings=True`` is unit only to ~1e-3 at 1024 dims, so the
    embedder RE-NORMALIZES at the producer to EXACT unit norm — the index treats ``mat @ q`` AS
    cosine and the trust-lifecycle cosines assume unit vectors. A NON-unit model vector must come
    out unit. Deleting the renorm in ``_encode_native`` REDS this."""
    raw = np.full(_NATIVE, 2.0, dtype=np.float32)          # ||raw|| = 2*sqrt(1024) = 64, not unit
    emb = LocalSTEmbedder(model=_StubModel(fixed=raw[None, :]), d=_NATIVE).load()
    v = emb.encode("x")
    assert abs(float(np.linalg.norm(v)) - 1.0) < 1e-6
    M = emb.encode_batch(["a", "b"])
    assert np.allclose(np.linalg.norm(M, axis=1), 1.0, atol=1e-6)


def test_native_dim_for_raises_on_unknown_model():
    """``native_dim_for`` fails fast on an unregistered model — its native dim must be registered
    (and weights baked) before it can ship, so a typo never silently picks a wrong store width."""
    from hive.adapters.embedding.factory import native_dim_for
    assert native_dim_for(DEFAULT_MODEL) == _NATIVE
    with pytest.raises(ValueError, match="unknown embedding model|native_dim_for"):
        native_dim_for("ghost/not-a-real-model")


def test_d_is_ctor_arg():
    """``d`` is the stored-vector dim, the ctor arg the factory sources from ``native_dim_for``
    (the model's native dim) — exposed so the index/store can be sized before the model loads."""
    assert _emb(d=_NATIVE).d == _NATIVE


def test_load_asserts_model_native_matches_declared_d():
    """The declared ``d`` MUST equal the model's discovered native dim — there is no projection
    to reconcile a difference, so a mismatch (a baked-model swap) fails loud at load(), never
    ships a store whose width disagrees with the embedder. (Replaces the old d>native head guard;
    flipping the `!=` to `==` in load() REDS this.)"""
    e = LocalSTEmbedder(model=_StubModel(native=_NATIVE), d=_NATIVE - 1)
    with pytest.raises(GeometryError):
        e.load()


def test_two_embedders_same_model_encode_identically():
    """Encoding is deterministic + frozen across instances (the production restart path): a
    second embedder over the same model encodes byte-identically — no per-process fit."""
    emb = _emb()
    emb2 = LocalSTEmbedder(model=emb._model, d=_NATIVE).load()
    t = "the migration script is idempotent across a restart"
    assert np.allclose(emb.encode(t), emb2.encode(t), atol=1e-6)


def test_loaded_is_false_until_load_then_idempotent():
    e = LocalSTEmbedder(model=_StubModel(), d=_NATIVE)
    assert e.loaded is False                                # native not yet verified ⇒ not resident
    e.load()
    assert e.loaded is True
    e.load()                                                # idempotent — no re-verify error
    assert e.loaded is True


def test_lazy_encode_auto_loads():
    e = LocalSTEmbedder(model=_StubModel(), d=_NATIVE)
    v = e.encode("auto load on first encode")              # not explicitly loaded
    assert e.loaded is True and v.shape == (_NATIVE,)


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
    bad = LocalSTEmbedder(model=_NanModel(), d=_NATIVE)
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
        return LocalSTEmbedder(model=st_model, d=_NATIVE).load()

    def test_native_discovered_is_1024_and_emitted_unchanged(self, emb, st_model):
        """Headline acceptance: native is DISCOVERED from the real model (1024), equals the
        declared d, and is emitted unchanged (unit-norm, no projection)."""
        assert emb.d == st_model.get_sentence_embedding_dimension() == _NATIVE
        v = emb.encode("the recall gate degrades to identity when uncertain")
        assert v.shape == (_NATIVE,) and abs(float(np.linalg.norm(v)) - 1.0) < 1e-5

    def test_semantic_neighbours_preserved(self, emb):
        """The load-bearing recall property in the native space: a related pair stays closer
        than an unrelated pair."""
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
