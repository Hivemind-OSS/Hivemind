"""M01 — LocalSTEmbedder (real bge-small + frozen PCA head). These load the real model
once (module-scoped) so the single encode chain, unit-norm geometry, frozen-head codec
round-trip, and semantic-neighbour preservation are exercised against the shipping adapter.
"""
from __future__ import annotations

import numpy as np
import pytest

from hive.adapters.embedding.local_st import DEFAULT_MODEL, LocalSTEmbedder
from hive.domain.errors import GeometryError
from hive.domain.ports import EmbeddingProvider


@pytest.fixture(scope="module")
def st_model():
    from sentence_transformers import SentenceTransformer
    return SentenceTransformer(DEFAULT_MODEL, device="cpu")


@pytest.fixture(scope="module")
def emb(st_model):
    # w_version=7 so the stamp assertions are non-default; bootstrap_n small-but-valid.
    return LocalSTEmbedder(model=st_model, w_version=7, bootstrap_n=448).load()


def test_encode_returns_unit_norm_float32_d(emb):
    v = emb.encode("the database connection pool must be sized for concurrency")
    assert v.shape == (256,)
    assert v.dtype == np.float32
    assert abs(float(np.linalg.norm(v)) - 1.0) < 1e-5      # ← head normalize (mutation target)


def test_encode_batch_rows_unit_norm(emb):
    M = emb.encode_batch(["alpha beta gamma", "redis cache eviction", "git revert clawback"])
    assert M.shape == (3, 256)
    norms = np.linalg.norm(M, axis=1)
    assert np.allclose(norms, 1.0, atol=1e-5)


def test_encode_eq_encode_batch_single(emb):
    """The split-brain killer: one text through encode() == through encode_batch([text])."""
    t = "the recall gate degrades to identity when uncertain"
    a = emb.encode(t)
    b = emb.encode_batch([t])[0]
    assert np.allclose(a, b, atol=1e-6)


def test_empty_batch_returns_empty_d_shape(emb):
    M = emb.encode_batch([])
    assert M.shape == (0, 256)
    assert M.dtype == np.float32


def test_w_version_stamped(emb):
    assert emb.w_version == 7
    # the frozen head also carries the version (so re-embed round-trips the geometry tag)
    from hive.adapters.embedding.head import FrozenPcaHead
    assert FrozenPcaHead.from_bytes(emb.head_bytes()).w_version == 7


def test_d_matches_projected_dim(emb):
    assert emb.d == 256


def test_semantic_neighbours_preserved_in_projection(emb):
    """The load-bearing recall property: the 384→256 PCA head PRESERVES bge semantics —
    a related pair stays closer than an unrelated pair in the PROJECTED space (so the
    recall@5 floor is reachable, not destroyed by the dim reduction)."""
    q = emb.encode("the redis cache must not drop queue keys under memory pressure")
    near = emb.encode("redis maxmemory-policy and queue key persistence")
    far = emb.encode("a yellow banana is a sweet tropical fruit")
    assert float(q @ near) > float(q @ far) + 0.05


def test_head_frozen_roundtrip_via_bytes(st_model, emb):
    """A second embedder built from the persisted head bytes encodes identically — the
    geometry is frozen + serializable (the production restart / re-embed path)."""
    emb2 = LocalSTEmbedder(model=st_model, w_version=7, head_bytes=emb.head_bytes())
    assert emb2.loaded is True                              # model + head both present ⇒ resident
    t = "the migration script is idempotent across a restart"
    assert np.allclose(emb.encode(t), emb2.encode(t), atol=1e-6)


def test_loaded_is_false_until_load_then_idempotent(st_model):
    e = LocalSTEmbedder(model=st_model, w_version=1, head=None)
    assert e.loaded is False                                # head not yet fit ⇒ not resident
    e.load()
    assert e.loaded is True
    e.load()                                                # idempotent — no re-fit error
    assert e.loaded is True


def test_pca_fit_too_few_samples_raises(st_model):
    """A bootstrap smaller than the native dim is rank-deficient ⇒ fail loud, never ship a
    degenerate head (head.fit_pca's underpowered guard, surfaced through load())."""
    e = LocalSTEmbedder(model=st_model, w_version=1, bootstrap_n=100)
    with pytest.raises(GeometryError):
        e.load()


def test_lazy_encode_auto_loads(st_model):
    e = LocalSTEmbedder(model=st_model, w_version=1)
    v = e.encode("auto load on first encode")              # not explicitly loaded
    assert e.loaded is True and v.shape == (256,)


def test_conforms_to_embedding_provider(emb):
    assert isinstance(emb, EmbeddingProvider)


class _NanModel:
    """A model double whose encode() returns NaN — to exercise the write-boundary guard."""
    def encode(self, texts, **kw):
        return np.full((len(list(texts)), 384), np.nan, dtype=np.float32)


def test_encode_rejects_non_finite_vector(emb):
    """The value WRITE boundary: a non-finite embedding is refused at the producer (never
    written to episodes.value, never crashing the index rebuild) — the reembed-nan class.
    Deleting `_require_finite` from encode (deliberate mutation) makes this go red."""
    bad = LocalSTEmbedder(model=_NanModel(), w_version=7, head=emb._head)   # real head, NaN model
    with pytest.raises(GeometryError):
        bad.encode("anything")
    with pytest.raises(GeometryError):
        bad.encode_batch(["a", "b"])


def test_head_wversion_must_match_embedder(st_model, emb):
    """A persisted head from a DIFFERENT geometry cannot be loaded under a mismatched stamp —
    that would silently corrupt the geometry re-embed round-trip. Fail fast at construction."""
    with pytest.raises(GeometryError):
        LocalSTEmbedder(model=st_model, w_version=2, head_bytes=emb.head_bytes())   # head is w_version=7
