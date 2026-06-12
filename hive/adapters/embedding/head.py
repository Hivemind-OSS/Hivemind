"""FrozenPcaHead + the BUILD-NEW HVH1 byte codec [B2].

The variance-preserving native→d projection head, FROZEN at construction (no lazy
fit, no random fallback — the reference's encode/encode_batch split-brain is DELETED).
The codec serializes the head so a re-embed migration round-trips W_version
bit-for-bit — the field the reference's base64+JSON codec silently dropped, which is
why the geometry re-embed could corrupt geometry. Endianness is pinned little-endian.

  24-byte header (struct '<4sHHIII I'):
   off 0  4  MAGIC b"HVH1"      off 4 2 FMT_VERSION u16=1    off 6 2 DTYPE u16=1(float32)
   off 8  4  W_VERSION u32      off 12 4 D_OUT u32           off 16 4 D_IN u32   off 20 4 reserved=0
   off 24 N  W float32 LE C-order, N == d_out*d_in*4
"""
from __future__ import annotations

import struct

import numpy as np

from hive.domain.errors import GeometryError, HeadCodecError

NATIVE_DIM = 384          # bge-small-en-v1.5
PROJECTED_DIM = 256       # the dense value dim
_MAGIC = b"HVH1"
_FMT_VERSION = 1
_DTYPE_F32 = 1
_HEADER = struct.Struct("<4sHHIII I")   # 24 bytes
assert _HEADER.size == 24


class FrozenPcaHead:
    def __init__(self, W: np.ndarray, w_version: int) -> None:
        W = np.ascontiguousarray(np.asarray(W, dtype=np.float32))
        if W.ndim != 2:
            raise GeometryError(f"head W must be 2-D; got shape {W.shape}")
        d_out, d_in = W.shape
        if d_in != NATIVE_DIM or d_out != PROJECTED_DIM:
            raise GeometryError(
                f"head must be ({PROJECTED_DIM},{NATIVE_DIM}); got {(d_out, d_in)}")
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
    def fit_pca(cls, samples: np.ndarray, w_version: int) -> "FrozenPcaHead":
        """Top-PROJECTED_DIM principal directions of the native sample batch. Needs
        n >= NATIVE_DIM, else the covariance is rank-deficient (underpowered) and we
        fail loud rather than ship a degenerate head."""
        X = np.asarray(samples, dtype=np.float32)
        if X.ndim != 2 or X.shape[1] != NATIVE_DIM:
            raise GeometryError(f"fit samples must be (n,{NATIVE_DIM}); got {X.shape}")
        if X.shape[0] < NATIVE_DIM:
            raise GeometryError(
                f"pca_fit_underpowered: n={X.shape[0]} < native_dim={NATIVE_DIM}")
        Xc = X - X.mean(axis=0, keepdims=True)
        cov = (Xc.T @ Xc) / float(X.shape[0])
        _evals, evecs = np.linalg.eigh(cov)               # ascending
        top = evecs[:, ::-1][:, :PROJECTED_DIM]            # (d_in, d_out) leading dirs
        return cls(W=top.T.astype(np.float32), w_version=w_version)

    # ── codec [B2] ────────────────────────────────────────────────────────────
    def to_bytes(self) -> bytes:
        header = _HEADER.pack(_MAGIC, _FMT_VERSION, _DTYPE_F32,
                              self.w_version, self.d_out, self.d_in, 0)
        body = np.ascontiguousarray(self.W, dtype="<f4").tobytes()
        return header + body

    @classmethod
    def from_bytes(cls, raw: bytes) -> "FrozenPcaHead":
        if len(raw) < _HEADER.size:
            raise HeadCodecError("head payload shorter than the 24-byte header")
        magic, fmt, dtype, w_version, d_out, d_in, _res = _HEADER.unpack(raw[:_HEADER.size])
        if magic != _MAGIC:
            raise HeadCodecError(f"bad magic {magic!r}; expected {_MAGIC!r}")
        if fmt != _FMT_VERSION:
            raise HeadCodecError(f"unsupported FMT_VERSION {fmt}")
        if dtype != _DTYPE_F32:
            raise HeadCodecError(f"unsupported DTYPE {dtype}")
        expected = _HEADER.size + d_out * d_in * 4
        if len(raw) != expected:
            raise HeadCodecError(f"truncated/overlong payload: {len(raw)} != {expected}")
        W = np.frombuffer(raw[_HEADER.size:], dtype="<f4").reshape(d_out, d_in).copy()
        return cls(W=W, w_version=w_version)
