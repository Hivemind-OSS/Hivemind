"""M01 — LocalSTEmbedder: the real, shipping ``EmbeddingProvider`` (Qwen3-Embedding-0.6B,
baked, CPU) behind the embedding swap seam [B2/D2].

The SINGLE encode chain for BOTH capture and recall (no encode/encode_batch split-brain):
every text goes Qwen3-Embedding → the model's NATIVE vector → L2-renormalized to exact unit
norm. There is no projection head and no dimension reduction — the stored-vector dim IS the
model's native dim; the only transform is the unit-norm tightening the index cosine relies on. Encoding is SYMMETRIC and prompt-free — the same text always yields the
same vector, which the trust lifecycle's demand_tau/competitor_tau cosines depend on (the model's
asymmetric query instruction is deferred; see TODOS.md TODO 7).

``d`` is the model's native dim, DECLARED at construction (the factory sources it from
``native_dim_for(model)`` — known torch-free, so the index/store can be sized before the heavy
model load). ``load()`` then DISCOVERS the model's real native dim and ASSERTS it equals ``d``:
there is no projection to reconcile a difference, so a mismatch (a baked-model swap without a
re-init) fails loud rather than shipping a store whose width disagrees with the embedder.

``load()`` is the warm step the container's ``warm_embedder`` drives: it loads the
SentenceTransformer (the ~one-time model resident cost) and verifies the native dim. Until
then ``loaded`` is False, so the M12 healthcheck refuses to go green (healthy ≡ resident).
"""
from __future__ import annotations

import logging
from typing import Sequence

import numpy as np

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
    """``text → value[d]`` (Qwen3-Embedding native vector, L2-renormalized to unit norm). The
    shipping embedder; satisfies the ``EmbeddingProvider`` Protocol (``d``, ``encode``,
    ``encode_batch``) plus the M12 warm contract (``load``, ``loaded``, ``name``)."""

    def __init__(self, *, d: int, model_name: str = DEFAULT_MODEL, model=None) -> None:
        self.name = str(model_name)
        self.d = int(d)                    # the native (stored-vector) dim, sourced from native_dim_for
        self._model = model                # an injected, already-loaded ST (tests) or None
        self.loaded = False                # resident only after load() verifies the native dim

    # ── warm: load the model + verify the native dim (the M12 boot.embedder_warm step) ──
    def load(self) -> "LocalSTEmbedder":
        """Bring the embedder RESIDENT: load the SentenceTransformer (CPU) and assert its native
        dim equals the declared ``d``. Idempotent. Raises GeometryError on a native/declared-d
        mismatch (the entrypoint maps that to EX_UNAVAILABLE=69)."""
        if self._model is None:
            from sentence_transformers import SentenceTransformer  # noqa: PLC0415 — lazy/heavy
            _log.info("embedding.model_load name=%s", self.name)
            self._model = SentenceTransformer(self.name, device="cpu")
        # sentence-transformers renamed get_sentence_embedding_dimension → get_embedding_dimension;
        # prefer the new name, fall back to the old, so we span versions without a FutureWarning.
        get_dim = (getattr(self._model, "get_embedding_dimension", None)
                   or self._model.get_sentence_embedding_dimension)
        native = int(get_dim())
        if native != self.d:
            raise GeometryError(
                f"model native dim {native} != declared d {self.d} — the embedder emits the "
                "model's native vector unchanged; re-initialise the store (hive nuke) after a "
                "model/dim change")
        _log.info("embedding.warm name=%s native_dim=%d", self.name, native)
        self.loaded = True
        return self

    # ── encode (the single chain; native Qwen3, emitted unchanged) ────────────────
    def encode(self, text: str) -> np.ndarray:
        if not self.loaded:
            self.load()
        return _require_finite(self._encode_native([text])[0])     # the value WRITE boundary

    def encode_batch(self, texts: Sequence[str]) -> np.ndarray:
        if not self.loaded:
            self.load()
        rows = list(texts)
        if not rows:
            return np.zeros((0, self.d), dtype=np.float32)
        return _require_finite(self._encode_native(rows))

    # ── internals ────────────────────────────────────────────────────────────────
    def _encode_native(self, texts: list[str]) -> np.ndarray:
        """Qwen3-Embedding native embeddings, RE-NORMALIZED to exact unit norm in float32,
        (n, d). The model's own ``normalize_embeddings=True`` is unit only to ~1e-3 at 1024 dims
        (float32 accumulation), and the index treats ``mat @ q`` AS cosine, so the producer
        tightens the magnitude to exactly 1.0 — the renorm the now-removed truncation head used to
        perform. No prompt is passed, so capture and recall encode symmetrically (same text → same
        vector)."""
        vecs = np.asarray(self._model.encode(
            texts, normalize_embeddings=True, convert_to_numpy=True), dtype=np.float32)
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return np.ascontiguousarray(vecs / norms, dtype=np.float32)
