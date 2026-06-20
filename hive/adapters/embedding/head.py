"""Projection heads (``FrozenPcaHead`` / ``TruncationHead``) + the BUILD-NEW HVH1 byte
codec [B2].

Two frozen native→d reducers behind one byte format, discriminated by a ``kind`` field:
  * ``FrozenPcaHead`` (KIND_LINEAR) — a variance-preserving fitted ``d_in→d_out`` matrix,
    for a model with no native lower-dim (the reference's encode/encode_batch split-brain
    is DELETED). The native dim (d_in) is discovered from the sample matrix at fit time; the
    compression dim (d_out) comes from config. Both serialize, so it is a general reducer.
  * ``TruncationHead`` (KIND_TRUNCATION) — a Matryoshka prefix slice for an MRL-trained model
    (Qwen3-Embedding): the leading d_out dims are a TRAINED-native lower-dim embedding, so the
    compression is the model's own, not a fitted/lossy basis. It carries no matrix — empty body.

The codec serializes the head so a re-embed migration round-trips W_version
bit-for-bit — the field the reference's base64+JSON codec silently dropped, which is
why the geometry re-embed could corrupt geometry. Endianness is pinned little-endian.

  24-byte header (struct '<4sHHIII I'):
   off 0  4  MAGIC b"HVH1"      off 4 2 FMT_VERSION u16=1    off 6 2 DTYPE u16=1(float32)
   off 8  4  W_VERSION u32      off 12 4 D_OUT u32           off 16 4 D_IN u32
   off 20 4  KIND u32 (0=linear PCA matrix body, 1=truncation / empty body)
   off 24 N  W float32 LE C-order, N == d_out*d_in*4 (KIND_LINEAR); N == 0 (KIND_TRUNCATION)
"""
from __future__ import annotations

import struct

import numpy as np

from hive.domain.errors import GeometryError, HeadCodecError

_MAGIC = b"HVH1"
_FMT_VERSION = 1
_DTYPE_F32 = 1
_HEADER = struct.Struct("<4sHHIII I")   # 24 bytes
assert _HEADER.size == 24

# Head kind, carried in the header's last u32 (repurposed from the old reserved=0 slot, so a
# legacy linear head — reserved=0 — decodes as KIND_LINEAR, backward-compatible).
KIND_LINEAR = 0        # FrozenPcaHead: a fitted d_in→d_out matrix in the body
KIND_TRUNCATION = 1    # TruncationHead: Matryoshka prefix slice, empty body


class FrozenPcaHead:
    def __init__(self, W: np.ndarray, w_version: int) -> None:
        W = np.ascontiguousarray(np.asarray(W, dtype=np.float32))
        if W.ndim != 2:
            raise GeometryError(f"head W must be 2-D; got shape {W.shape}")
        d_out, d_in = W.shape
        if not (1 <= d_out <= d_in):
            raise GeometryError(
                f"projection head must reduce dim: need 1 <= d_out <= d_in; got {(d_out, d_in)}")
        self.W = W
        self.w_version = int(w_version)
        self.d_in = d_in
        self.d_out = d_out

    def __call__(self, e: np.ndarray) -> np.ndarray:
        out = self.W @ np.asarray(e, dtype=np.float32)
        n = float(np.linalg.norm(out))
        if n > 0:
            out = out / n
        return out.astype(np.float32)

    def batch(self, E: np.ndarray) -> np.ndarray:
        out = (self.W @ np.asarray(E, dtype=np.float32).T).T
        norms = np.linalg.norm(out, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return (out / norms).astype(np.float32)

    @classmethod
    def fit_pca(cls, samples: np.ndarray, w_version: int, *, d_out: int) -> "FrozenPcaHead":
        """Top-``d_out`` principal directions of the native sample batch. The native dim is
        INFERRED from the samples (``X.shape[1]``) — the head carries no model constant. Needs
        n >= native, else the covariance is rank-deficient (underpowered) and we fail loud
        rather than ship a degenerate head."""
        X = np.asarray(samples, dtype=np.float32)
        if X.ndim != 2:
            raise GeometryError(f"fit samples must be 2-D; got shape {X.shape}")
        native = X.shape[1]
        if not (1 <= d_out <= native):
            raise GeometryError(f"d_out must satisfy 1 <= d_out <= native({native}); got {d_out}")
        if X.shape[0] < native:
            raise GeometryError(
                f"pca_fit_underpowered: n={X.shape[0]} < native_dim={native}")
        Xc = X - X.mean(axis=0, keepdims=True)
        cov = (Xc.T @ Xc) / float(X.shape[0])
        _evals, evecs = np.linalg.eigh(cov)               # ascending
        top = evecs[:, ::-1][:, :d_out]                    # (d_in, d_out) leading dirs
        return cls(W=top.T.astype(np.float32), w_version=w_version)

    # ── codec [B2] ────────────────────────────────────────────────────────────
    def to_bytes(self) -> bytes:
        header = _HEADER.pack(_MAGIC, _FMT_VERSION, _DTYPE_F32,
                              self.w_version, self.d_out, self.d_in, KIND_LINEAR)
        body = np.ascontiguousarray(self.W, dtype="<f4").tobytes()
        return header + body

    @classmethod
    def from_bytes(cls, raw: bytes) -> "FrozenPcaHead":
        if len(raw) < _HEADER.size:
            raise HeadCodecError("head payload shorter than the 24-byte header")
        magic, fmt, dtype, w_version, d_out, d_in, kind = _HEADER.unpack(raw[:_HEADER.size])
        if magic != _MAGIC:
            raise HeadCodecError(f"bad magic {magic!r}; expected {_MAGIC!r}")
        if fmt != _FMT_VERSION:
            raise HeadCodecError(f"unsupported FMT_VERSION {fmt}")
        if dtype != _DTYPE_F32:
            raise HeadCodecError(f"unsupported DTYPE {dtype}")
        if kind != KIND_LINEAR:
            raise HeadCodecError(f"not a linear PCA head: kind={kind}")
        expected = _HEADER.size + d_out * d_in * 4
        if len(raw) != expected:
            raise HeadCodecError(f"truncated/overlong payload: {len(raw)} != {expected}")
        W = np.frombuffer(raw[_HEADER.size:], dtype="<f4").reshape(d_out, d_in).copy()
        return cls(W=W, w_version=w_version)


class TruncationHead:
    """Matryoshka prefix reducer: keep the first ``d_out`` dims of a ``d_in``-native embedding
    and L2-renormalize. For an MRL-trained model the leading dims are themselves a trained
    lower-dim embedding, so this is the model's OWN compression — no fitted matrix, no lossy
    generic-corpus basis. Shares the head interface (``__call__``/``batch``/``to_bytes``/
    ``w_version``/``d_in``/``d_out``) so the embedder + codec are agnostic to which kind it holds."""

    def __init__(self, *, d_in: int, d_out: int, w_version: int) -> None:
        d_in, d_out = int(d_in), int(d_out)
        if not (1 <= d_out <= d_in):
            raise GeometryError(
                f"truncation head must reduce dim: need 1 <= d_out <= d_in; got {(d_out, d_in)}")
        self.d_in = d_in
        self.d_out = d_out
        self.w_version = int(w_version)

    def __call__(self, e: np.ndarray) -> np.ndarray:
        out = np.asarray(e, dtype=np.float32)[:self.d_out]
        n = float(np.linalg.norm(out))
        if n > 0:
            out = out / n
        return np.ascontiguousarray(out, dtype=np.float32)

    def batch(self, E: np.ndarray) -> np.ndarray:
        out = np.asarray(E, dtype=np.float32)[:, :self.d_out]
        norms = np.linalg.norm(out, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return np.ascontiguousarray(out / norms, dtype=np.float32)

    # ── codec [B2]: header-only (empty body) ──────────────────────────────────────
    def to_bytes(self) -> bytes:
        return _HEADER.pack(_MAGIC, _FMT_VERSION, _DTYPE_F32,
                            self.w_version, self.d_out, self.d_in, KIND_TRUNCATION)

    @classmethod
    def from_bytes(cls, raw: bytes) -> "TruncationHead":
        if len(raw) < _HEADER.size:
            raise HeadCodecError("head payload shorter than the 24-byte header")
        magic, fmt, _dtype, w_version, d_out, d_in, kind = _HEADER.unpack(raw[:_HEADER.size])
        if magic != _MAGIC:
            raise HeadCodecError(f"bad magic {magic!r}; expected {_MAGIC!r}")
        if fmt != _FMT_VERSION:
            raise HeadCodecError(f"unsupported FMT_VERSION {fmt}")
        if kind != KIND_TRUNCATION:
            raise HeadCodecError(f"not a truncation head: kind={kind}")
        if len(raw) != _HEADER.size:
            raise HeadCodecError(
                f"truncation head carries no body; got {len(raw) - _HEADER.size} extra byte(s)")
        return cls(d_in=d_in, d_out=d_out, w_version=w_version)


def head_from_bytes(raw: bytes):
    """Decode a serialized head to its concrete type by the header KIND byte — the ONE decode
    entry the embedder/container use, so a persisted head reconstructs as whatever kind wrote
    it. Backward-compatible: a legacy head wrote reserved=0 == KIND_LINEAR ⇒ FrozenPcaHead."""
    if len(raw) < _HEADER.size:
        raise HeadCodecError("head payload shorter than the 24-byte header")
    magic, _fmt, _dtype, _wver, _dout, _din, kind = _HEADER.unpack(raw[:_HEADER.size])
    if magic != _MAGIC:
        raise HeadCodecError(f"bad magic {magic!r}; expected {_MAGIC!r}")
    if kind == KIND_LINEAR:
        return FrozenPcaHead.from_bytes(raw)
    if kind == KIND_TRUNCATION:
        return TruncationHead.from_bytes(raw)
    raise HeadCodecError(f"unknown head kind {kind}")
