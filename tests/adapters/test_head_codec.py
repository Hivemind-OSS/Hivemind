"""P1.2 — FrozenPcaHead + the HVH1 codec [B2]: a geometry-agnostic frozen reducer whose
W_version + dims survive serialization. Pure-numpy (torch-free): proves the projection math
is unchanged and dim-general, which is the byte-stable-recall guard the model itself can't be
asked to prove without loading bge."""
from __future__ import annotations

import numpy as np
import pytest

from hive.adapters.embedding.head import FrozenPcaHead
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
