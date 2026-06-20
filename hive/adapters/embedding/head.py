"""Projection head (``TruncationHead``) + the BUILD-NEW HVH1 byte codec [B2].

The only producible head is a frozen native→d reducer carried behind a byte format that
reserves a ``kind`` discriminator:
  * ``TruncationHead`` (KIND_TRUNCATION) — a Matryoshka prefix slice for an MRL-trained model
    (Qwen3-Embedding): the leading d_out dims are a TRAINED-native lower-dim embedding, so the
    compression is the model's own, not a fitted/lossy basis. It carries no matrix — empty body.

``KIND_LINEAR`` is a recognized-but-refused legacy kind: a removed fitted ``d_in→d_out`` PCA
matrix head. The codec still names the value so a legacy blob is recognized and rejected with
a clear message rather than a cryptic decode error; nothing in this module produces it.

The codec serializes the head so a re-embed migration round-trips W_version
bit-for-bit — the field the reference's base64+JSON codec silently dropped, which is
why the geometry re-embed could corrupt geometry. Endianness is pinned little-endian.

  24-byte header (struct '<4sHHIII I'):
   off 0  4  MAGIC b"HVH1"      off 4 2 FMT_VERSION u16=1    off 6 2 DTYPE u16=1(float32)
   off 8  4  W_VERSION u32      off 12 4 D_OUT u32           off 16 4 D_IN u32
   off 20 4  KIND u32 (0=legacy linear matrix body — refused; 1=truncation / empty body)
   off 24 N  W float32 LE C-order, N == 0 (KIND_TRUNCATION is the only kind written)
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
# legacy linear head — reserved=0 — is recognized as KIND_LINEAR and refused at decode).
KIND_LINEAR = 0        # legacy fitted d_in→d_out matrix head — removed; recognized-but-refused
KIND_TRUNCATION = 1    # TruncationHead: Matryoshka prefix slice, empty body


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
    it. KIND_LINEAR is a legacy fitted-matrix head: recognized so the blob is refused with a
    clear message rather than a cryptic codec error, never silently reconstructed."""
    if len(raw) < _HEADER.size:
        raise HeadCodecError("head payload shorter than the 24-byte header")
    magic, _fmt, _dtype, _wver, _dout, _din, kind = _HEADER.unpack(raw[:_HEADER.size])
    if magic != _MAGIC:
        raise HeadCodecError(f"bad magic {magic!r}; expected {_MAGIC!r}")
    if kind == KIND_LINEAR:
        raise HeadCodecError(
            "legacy PCA (KIND_LINEAR) projection head is no longer supported; this store "
            "predates the truncation-only geometry")
    if kind == KIND_TRUNCATION:
        return TruncationHead.from_bytes(raw)
    raise HeadCodecError(f"unknown head kind {kind}")
