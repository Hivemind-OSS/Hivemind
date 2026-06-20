"""M01 — LocalSTEmbedder: the real, shipping ``EmbeddingProvider`` (Qwen3-Embedding-0.6B,
baked, CPU) behind the embedding swap seam [B2/D2].

The SINGLE encode chain for BOTH capture and recall (no encode/encode_batch split-brain —
the reference's lazy-fit / random-fallback fork is DELETED): every text goes Qwen3-Embedding
(native 1024, L2-normalized) → a FROZEN truncation head (1024→768, the model's own Matryoshka
prefix) → L2-normalize. Encoding is SYMMETRIC and prompt-free — the same text always yields the
same vector, which the trust lifecycle's demand_tau/competitor_tau cosines depend on (the model's
asymmetric query instruction is deferred; see TODOS.md TODO 7).

The truncation head is FROZEN at construction, never lazily fit on the hot path:
  * ``head_bytes`` given  → the head is deserialized from the HVH1 codec (the persisted,
    W_version'd geometry — the production path: build_container reads it from the store meta so
    every restart reuses the SAME basis and recall is byte-stable).
  * no head bytes         → the head is built ONCE, at ``load()`` (warm) time, from the model's
    DISCOVERED native dim: a ``TruncationHead`` keeping the leading ``d`` dims. There is no corpus
    and no fit — an MRL model's prefix IS itself a trained lower-dim embedding — so the head is
    identical across processes for a given (model, d).

``load()`` is the warm step the container's ``warm_embedder`` drives: it loads the
SentenceTransformer (the ~one-time model resident cost) and builds/installs the head. Until
then ``loaded`` is False, so the M12 healthcheck refuses to go green (healthy ≡ resident).
"""
from __future__ import annotations

import logging
from typing import Optional, Sequence

import numpy as np

from hive.adapters.embedding.head import TruncationHead, head_from_bytes
from hive.domain.errors import GeometryError

_log = logging.getLogger("hive.embedding")

DEFAULT_MODEL = "Qwen/Qwen3-Embedding-0.6B"


def _require_finite(v: np.ndarray) -> np.ndarray:
    """Refuse a non-finite embedding at the value WRITE boundary. The embedder is the single
    producer of every stored/searched value; a NaN/Inf here would be written to episodes.value
    (then crash the index rebuild's finiteness guard, stranding the store — the
    reembed-nan-corruption class). Catch it AT the producer, loudly. // O(d)."""
    if not bool(np.all(np.isfinite(v))):
        raise GeometryError(
            "embedder produced a non-finite (NaN/Inf) vector — refused at the write boundary")
    return v


class LocalSTEmbedder:
    """``text → value[768]`` (Qwen3-Embedding → frozen truncation head → unit-norm). The
    shipping embedder; satisfies the ``EmbeddingProvider`` Protocol (``d``, ``w_version``,
    ``encode``, ``encode_batch``) plus the M12 warm contract (``load``, ``loaded``,
    ``name``)."""

    def __init__(
        self, *, d: int, model_name: str = DEFAULT_MODEL, w_version: int = 1,
        head: Optional[TruncationHead] = None, head_bytes: Optional[bytes] = None,
        model=None,
    ) -> None:
        self.name = str(model_name)
        self.w_version = int(w_version)
        self.d = int(d)                    # the compression dim, sourced from geometry.d
        self._model = model                # an injected, already-loaded ST (tests) or None
        if head is not None:
            self._head: Optional[TruncationHead] = head
        elif head_bytes is not None:
            self._head = head_from_bytes(head_bytes)   # persisted geometry (kind-dispatched)
        else:
            self._head = None              # built at load() from the model's discovered native dim
        # A supplied head's geometry tag MUST match this embedder's declared w_version — a
        # persisted-head/declared-version mismatch would silently corrupt the geometry re-embed
        # round-trip (the head says one W_version, the stamp says another). Fail fast, loud.
        if self._head is not None and int(self._head.w_version) != self.w_version:
            raise GeometryError(
                f"head w_version {self._head.w_version} != embedder w_version {self.w_version} "
                "— a persisted head from a different geometry cannot be loaded under this stamp")
        # A supplied head's compression dim MUST match this embedder's declared d — a persisted
        # head from a DIFFERENT projection dim would disagree with the index/store vector dim.
        # The out-of-scope live geometry.d swap fails loud HERE, never corrupts silently.
        if self._head is not None and int(self._head.d_out) != self.d:
            raise GeometryError(
                f"head d_out {self._head.d_out} != embedder d {self.d} "
                "— a persisted head from a different projection dim cannot be loaded")
        # resident iff BOTH the model and the frozen head are in place
        self.loaded = self._model is not None and self._head is not None

    # ── warm: load the model + build the head (the M12 boot.embedder_warm step) ──
    def load(self) -> "LocalSTEmbedder":
        """Bring the embedder RESIDENT: load the SentenceTransformer (CPU) and, if no head was
        supplied, build + freeze the truncation head from the model's native dim. Idempotent.
        Raises on a genuine load failure / an impossible d>native (the entrypoint maps that to
        EX_UNAVAILABLE=69)."""
        if self._model is None:
            from sentence_transformers import SentenceTransformer  # noqa: PLC0415 — lazy/heavy
            _log.info("embedding.model_load name=%s", self.name)
            self._model = SentenceTransformer(self.name, device="cpu")
        if self._head is None:
            self._head = self._build_truncation_head()
        self.loaded = True
        return self

    def head_bytes(self) -> bytes:
        """The frozen head serialized via the HVH1 codec (for persistence to the store meta so
        a restart reuses the SAME geometry). Requires the head to be resident."""
        if self._head is None:
            raise RuntimeError("head not resident — call load() first")
        return self._head.to_bytes()

    # ── encode (the single chain; native Qwen3 → head → unit) ─────────────────────
    def encode(self, text: str) -> np.ndarray:
        if not self.loaded:
            self.load()
        native = self._encode_native([text])[0]
        out = self._head(native)           # 1024 → 768, re-normalized in the head
        return _require_finite(out)        # the value WRITE boundary — never emit a NaN/Inf

    def encode_batch(self, texts: Sequence[str]) -> np.ndarray:
        if not self.loaded:
            self.load()
        rows = list(texts)
        if not rows:
            return np.zeros((0, self.d), dtype=np.float32)
        native = self._encode_native(rows)
        return _require_finite(self._head.batch(native))

    # ── internals ────────────────────────────────────────────────────────────────
    def _encode_native(self, texts: list[str]) -> np.ndarray:
        """Qwen3-Embedding native embeddings (L2-normalized, float32, (n, 1024)). No prompt is
        passed, so capture and recall encode symmetrically (same text → same vector)."""
        vecs = self._model.encode(
            texts, normalize_embeddings=True, convert_to_numpy=True)
        return np.ascontiguousarray(np.asarray(vecs, dtype=np.float32))

    def _build_truncation_head(self) -> TruncationHead:
        """Freeze the 1024→d Matryoshka truncation head. The native dim is DISCOVERED from the
        model (never a constant); d>native is impossible and fails loud in the head ctor."""
        native = int(self._model.get_sentence_embedding_dimension())
        _log.info("embedding.head_build w_version=%d native_dim=%d d_out=%d",
                  self.w_version, native, self.d)
        return TruncationHead(d_in=native, d_out=self.d, w_version=self.w_version)
