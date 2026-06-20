"""P1.2 — TruncationHead + the HVH1 codec [B2]: a geometry-agnostic frozen reducer whose
W_version + dims survive serialization, plus the recognized-but-refused legacy KIND_LINEAR
arm. Pure-numpy (torch-free): proves the projection math is unchanged and dim-general, which
is the byte-stable-recall guard the model itself can't be asked to prove without loading the
embedding model."""
from __future__ import annotations

import struct

import numpy as np
import pytest

from hive.adapters.embedding.head import (
    _FMT_VERSION, _HEADER, _MAGIC, KIND_LINEAR, TruncationHead, head_from_bytes,
)
from hive.domain.errors import GeometryError, HeadCodecError


def _legacy_linear_bytes(*, d_in=8, d_out=4, w_version=1) -> bytes:
    """Hand-pack a header tagged KIND_LINEAR (the removed fitted-matrix head). Its body would
    have been a d_out×d_in float32 matrix; the dispatcher refuses on the kind byte before the
    body is ever read, so a header alone is enough to exercise the legacy-refuse contract."""
    header = _HEADER.pack(_MAGIC, _FMT_VERSION, 1, w_version, d_out, d_in, KIND_LINEAR)
    body = np.zeros((d_out, d_in), dtype="<f4").tobytes()
    return header + body


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


def test_truncation_bytes_bad_magic_raises():
    raw = bytearray(TruncationHead(d_in=8, d_out=4, w_version=1).to_bytes())
    raw[0] = ord("X")                                      # corrupt magic
    with pytest.raises(HeadCodecError):
        head_from_bytes(bytes(raw))


def test_truncation_bytes_trailing_body_raises():
    """A truncation head carries no body; extra trailing bytes are a corrupt/overlong payload."""
    raw = TruncationHead(d_in=8, d_out=4, w_version=1).to_bytes()
    with pytest.raises(HeadCodecError):
        TruncationHead.from_bytes(raw + b"\x00\x00\x00\x00")


def test_head_from_bytes_dispatches_truncation_kind():
    """The module dispatcher routes by the header KIND byte: a truncation head's bytes decode
    to a TruncationHead (mutation: writing the wrong kind for a truncation head misroutes it)."""
    trunc = TruncationHead(d_in=8, d_out=4, w_version=1)
    assert isinstance(head_from_bytes(trunc.to_bytes()), TruncationHead)


def test_head_from_bytes_refuses_legacy_linear():
    """KIND_LINEAR is the removed fitted-matrix PCA head: the dispatcher recognizes it and
    fails loud with the legacy message, never silently reconstructs a head from the blob."""
    with pytest.raises(HeadCodecError, match="legacy PCA"):
        head_from_bytes(_legacy_linear_bytes())


def test_truncation_dout_gt_din_raises():
    """A truncation head must REDUCE (1 <= d_out <= d_in). d_out > d_in (you cannot keep more
    prefix dims than exist) and an empty output dim both fail loud."""
    with pytest.raises(GeometryError):
        TruncationHead(d_in=3, d_out=5, w_version=1)
    with pytest.raises(GeometryError):
        TruncationHead(d_in=8, d_out=0, w_version=1)      # empty output dim
