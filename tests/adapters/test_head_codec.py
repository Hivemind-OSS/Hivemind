"""P1.2 — FrozenPcaHead + the HVH1 codec [B2]: a geometry-agnostic frozen reducer whose
W_version + dims survive serialization. Pure-numpy (torch-free): proves the projection math
is unchanged and dim-general, which is the byte-stable-recall guard the model itself can't be
asked to prove without loading bge."""
from __future__ import annotations

import numpy as np
import pytest

from hive.adapters.embedding.head import (
    FrozenPcaHead, TruncationHead, head_from_bytes,
)
from hive.domain.errors import GeometryError, HeadCodecError


def _head(d_in=384, d_out=256, w_version=7, seed=0):
    rng = np.random.default_rng(seed)
    W = rng.standard_normal((d_out, d_in)).astype(np.float32)
    return FrozenPcaHead(W=W, w_version=w_version)


@pytest.mark.parametrize("d_in,d_out", [(384, 256), (128, 32)])
def test_call_returns_unit_norm_projected(d_in, d_out):
    """The head is geometry-agnostic: any genuine d_in→d_out reduction projects to a
    unit-norm d_out vector (bge's 384→256 is just one instance)."""
    h = _head(d_in=d_in, d_out=d_out)
    out = h(np.random.default_rng(1).standard_normal(d_in).astype(np.float32))
    assert out.shape == (d_out,) and out.dtype == np.float32
    assert abs(np.linalg.norm(out) - 1.0) < 1e-5


def test_call_eq_batch_single():
    h = _head()
    e = np.random.default_rng(2).standard_normal(384).astype(np.float32)
    assert np.allclose(h(e), h.batch(e[None, :])[0], atol=1e-6)   # split-brain killer


def test_geometry_rejects_expansion_and_empty():
    """Structural validation: a head must REDUCE (1 <= d_out <= d_in). Expansion and an
    empty output dim are rejected — the only shape constraint now that the model constant
    is gone."""
    with pytest.raises(GeometryError):
        FrozenPcaHead(W=np.zeros((400, 384), dtype=np.float32), w_version=1)   # expansion
    with pytest.raises(GeometryError):
        FrozenPcaHead(W=np.zeros((0, 384), dtype=np.float32), w_version=1)     # empty d_out


def test_geometry_accepts_arbitrary_reduction():
    """Any genuine reduction constructs — the old exact-(256,384) weld is gone."""
    h = FrozenPcaHead(W=np.zeros((64, 384), dtype=np.float32), w_version=1)
    assert h.d_out == 64 and h.d_in == 384


def test_bytes_roundtrip_preserves_dims_and_wversion():
    """The codec is dim-general: a non-default (128,32) head round-trips d_in/d_out/w_version
    bit-identically — it never carried the model constant, only per-instance dims."""
    h = _head(d_in=128, d_out=32, w_version=7)
    h2 = FrozenPcaHead.from_bytes(h.to_bytes())
    assert h2.w_version == 7
    assert h2.d_in == 128 and h2.d_out == 32
    assert np.array_equal(h2.W, h.W)            # bit-identical


def test_bytes_bad_magic_raises():
    raw = bytearray(_head().to_bytes())
    raw[0] = ord("X")                            # corrupt magic
    with pytest.raises(HeadCodecError):
        FrozenPcaHead.from_bytes(bytes(raw))


def test_bytes_truncated_raises():
    raw = _head().to_bytes()
    with pytest.raises(HeadCodecError):
        FrozenPcaHead.from_bytes(raw[:-4])       # drop 4 bytes of W


def test_fit_pca_infers_native_keeps_d_out():
    """fit_pca infers native from the sample matrix (X.shape[1]) and keeps the top d_out
    directions — no model constant. d_out > native and n < native both fail loud."""
    rng = np.random.default_rng(3)
    samples = rng.standard_normal((250, 200)).astype(np.float32)   # native inferred = 200
    h = FrozenPcaHead.fit_pca(samples, w_version=2, d_out=50)
    assert h.W.shape == (50, 200) and h.d_in == 200 and h.d_out == 50 and h.w_version == 2
    with pytest.raises(GeometryError):                              # d_out > native(200)
        FrozenPcaHead.fit_pca(samples, w_version=2, d_out=201)
    with pytest.raises(GeometryError):                             # n(10) < native(200): underpowered
        FrozenPcaHead.fit_pca(rng.standard_normal((10, 200)).astype(np.float32),
                              w_version=2, d_out=50)


# ── TruncationHead: the Matryoshka prefix reducer (MRL model, no fitted basis) ──
def test_truncation_head_selects_prefix_and_renormalizes():
    """A TruncationHead keeps the first d_out dims (the model's trained-native Matryoshka
    prefix) and L2-renormalizes — no learned matrix. First-3 of a known vector whose prefix
    has norm 13 (3,4,12) renormalizes to (3/13, 4/13, 12/13)."""
    h = TruncationHead(d_in=8, d_out=3, w_version=1)
    e = np.array([3, 4, 12, 9, 9, 9, 9, 9], dtype=np.float32)
    out = h(e)
    assert out.shape == (3,) and out.dtype == np.float32
    assert np.allclose(out, np.array([3, 4, 12]) / 13.0, atol=1e-6)   # prefix + renorm (mutation target)
    assert abs(float(np.linalg.norm(out)) - 1.0) < 1e-6


def test_truncation_call_eq_batch_single():
    """Split-brain killer for truncation: one vector through __call__ == through batch([v])."""
    h = TruncationHead(d_in=10, d_out=4, w_version=3)
    e = np.random.default_rng(5).standard_normal(10).astype(np.float32)
    assert np.allclose(h(e), h.batch(e[None, :])[0], atol=1e-6)


def test_truncation_head_codec_roundtrip():
    """The codec round-trips (kind, d_in, d_out, w_version) with an EMPTY body — a
    TruncationHead carries no weight matrix, only its dims + version."""
    h = TruncationHead(d_in=1024, d_out=768, w_version=2)
    raw = h.to_bytes()
    assert len(raw) == 24                                  # header only: zero-length body
    h2 = head_from_bytes(raw)
    assert isinstance(h2, TruncationHead)
    assert (h2.d_in, h2.d_out, h2.w_version) == (1024, 768, 2)


def test_head_from_bytes_dispatches_kind():
    """The module dispatcher routes by the header KIND byte: a linear head's bytes decode to
    FrozenPcaHead, a truncation head's to TruncationHead (mutation: writing kind=0 for a
    truncation head misroutes it to the linear codec, which then fails on the empty body)."""
    linear = FrozenPcaHead(W=np.zeros((4, 8), dtype=np.float32), w_version=1)
    trunc = TruncationHead(d_in=8, d_out=4, w_version=1)
    assert isinstance(head_from_bytes(linear.to_bytes()), FrozenPcaHead)
    assert isinstance(head_from_bytes(trunc.to_bytes()), TruncationHead)


def test_truncation_dout_gt_din_raises():
    """A truncation head must REDUCE (1 <= d_out <= d_in) — the same invariant as the PCA
    head. d_out > d_in (you cannot keep more prefix dims than exist) fails loud."""
    with pytest.raises(GeometryError):
        TruncationHead(d_in=3, d_out=5, w_version=1)
    with pytest.raises(GeometryError):
        TruncationHead(d_in=8, d_out=0, w_version=1)      # empty output dim
