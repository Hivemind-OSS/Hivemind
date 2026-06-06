"""P1.2 — FrozenPcaHead + the HVH1 codec [B2]: W_version survives serialization."""
from __future__ import annotations

import numpy as np
import pytest

from hive.adapters.embedding.head import FrozenPcaHead, NATIVE_DIM, PROJECTED_DIM
from hive.domain.errors import GeometryError, HeadCodecError


def _head(w_version=7, seed=0):
    rng = np.random.default_rng(seed)
    W = rng.standard_normal((PROJECTED_DIM, NATIVE_DIM)).astype(np.float32)
    return FrozenPcaHead(W=W, w_version=w_version)


def test_call_returns_unit_norm_projected():
    h = _head()
    out = h(np.random.default_rng(1).standard_normal(NATIVE_DIM).astype(np.float32))
    assert out.shape == (PROJECTED_DIM,) and out.dtype == np.float32
    assert abs(np.linalg.norm(out) - 1.0) < 1e-5


def test_call_eq_batch_single():
    h = _head()
    e = np.random.default_rng(2).standard_normal(NATIVE_DIM).astype(np.float32)
    assert np.allclose(h(e), h.batch(e[None, :])[0], atol=1e-6)   # split-brain killer


def test_geometry_assert_on_construct():
    with pytest.raises(GeometryError):
        FrozenPcaHead(W=np.zeros((PROJECTED_DIM, 100), dtype=np.float32), w_version=1)  # bad d_in
    with pytest.raises(GeometryError):
        FrozenPcaHead(W=np.zeros((10, NATIVE_DIM), dtype=np.float32), w_version=1)       # bad d_out


def test_bytes_roundtrip_preserves_w_version():
    h = _head(w_version=7)
    h2 = FrozenPcaHead.from_bytes(h.to_bytes())
    assert h2.w_version == 7
    assert h2.d_in == NATIVE_DIM and h2.d_out == PROJECTED_DIM
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


def test_pca_fit_preserves_rank_and_needs_enough_samples():
    rng = np.random.default_rng(3)
    samples = rng.standard_normal((NATIVE_DIM + 50, NATIVE_DIM)).astype(np.float32)
    h = FrozenPcaHead.fit_pca(samples, w_version=2)
    assert h.W.shape == (PROJECTED_DIM, NATIVE_DIM) and h.w_version == 2
    with pytest.raises(GeometryError):
        FrozenPcaHead.fit_pca(rng.standard_normal((10, NATIVE_DIM)).astype(np.float32), w_version=2)
