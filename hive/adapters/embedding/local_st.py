"""M01 — LocalSTEmbedder: the real, shipping ``EmbeddingProvider`` (bge-small, baked,
CPU) behind the embedding swap seam [B2/D2].

The SINGLE encode chain for BOTH capture and recall (no encode/encode_batch
split-brain — the reference's lazy-fit / random-fallback fork is DELETED): every text
goes bge-small (native 384, L2-normalized) → a FROZEN PCA head (384→256) → L2-normalize.

The projection head is FROZEN at construction, never lazily fit on the hot path:
  * ``head_bytes`` given  → the head is deserialized from the HVH1 codec (the persisted,
    W_version'd geometry — the production path: build_container reads it from the store
    meta so every restart reuses the SAME basis and recall is byte-stable).
  * no head bytes         → the head is fit ONCE, at ``load()`` (warm) time, from a
    DETERMINISTIC bootstrap corpus embedded through bge — a real PCA basis (NOT a random
    JL projection, which config rejects), identical across processes for a given
    (model, w_version) because the corpus and ``np.linalg.eigh`` are deterministic.

``load()`` is the warm step the container's ``warm_embedder`` drives: it loads the
SentenceTransformer (the ~one-time model resident cost) and fits/installs the head. Until
then ``loaded`` is False, so the M12 healthcheck refuses to go green (healthy ≡ resident).
"""
from __future__ import annotations

import logging
from typing import Optional, Sequence

import numpy as np

from hive.adapters.embedding.head import NATIVE_DIM, PROJECTED_DIM, FrozenPcaHead
from hive.domain.errors import GeometryError

_log = logging.getLogger("hive.embedding")

DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"
# ≥ NATIVE_DIM (384) so the bootstrap PCA fit is full-rank; a touch over for headroom.
DEFAULT_BOOTSTRAP_N = 448


# ── deterministic bootstrap corpus (the cold-start PCA fit data) ──────────────
# A repo-independent, deterministic sentence pack. The fit only needs broad variance
# directions in bge space, so the pack spans engineering / ops / data / general topics.
# Combinatorial (subjects × predicates) → > NATIVE_DIM distinct sentences, with a
# ``salt`` rotation so a W_version bump can fit a genuinely different (still valid) basis.
_SUBJECTS: tuple[str, ...] = (
    "the database connection pool", "the redis cache layer", "the message queue",
    "the authentication service", "the deployment pipeline", "the vector index",
    "the embedding model", "the background worker", "the rate limiter",
    "the migration script", "the load balancer", "the secret scanner",
    "the retry policy", "the health check", "the container entrypoint",
    "the git outcome producer", "the recall gate", "the credit attributor",
    "the bootstrap loader", "the schema validator", "the backup snapshot",
    "the observability spine", "the config loader", "the tenant boundary",
    "the WAL transaction", "the index rebuild", "the posterior update",
)
_PREDICATES: tuple[str, ...] = (
    "must never drop keys under memory pressure", "is sized to avoid per-request churn",
    "fails fast when a required value is missing", "logs every boundary error with context",
    "runs inside a single immediate transaction", "rejects non-finite inputs at the boundary",
    "is rebuilt from the durable store on boot", "holds the cursor on a partial failure",
    "scans for credentials before any write", "is idempotent across a restart",
    "refuses to report green until it is resident", "splits reward by recall margin",
    "preserves the variance of the native space", "round-trips its version bit for bit",
    "never widens a false positive into a clawback", "stays clean of protocol pollution",
    "degrades to identity when uncertain", "excludes the held-out isolation slice",
)


def bootstrap_corpus(n: int = DEFAULT_BOOTSTRAP_N, *, salt: int = 0) -> list[str]:
    """A deterministic list of ``n`` distinct sentences (subjects × predicates), rotated
    by ``salt`` so a W_version bump fits a different-but-valid basis. // O(n)."""
    subj, pred = _SUBJECTS, _PREDICATES
    out: list[str] = []
    i = 0
    total = len(subj) * len(pred)
    while len(out) < n and i < total:
        idx = (i + salt) % total
        s = subj[idx % len(subj)]
        p = pred[(idx // len(subj)) % len(pred)]
        out.append(f"{s} {p}.")
        i += 1
    if len(out) < n:                       # n exceeds the grid — pad deterministically
        for j in range(n - len(out)):
            out.append(f"bootstrap variance probe number {j + salt}.")
    return out


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
    """``text → value[256]`` (bge-small → frozen PCA head → unit-norm). The shipping
    embedder; satisfies the ``EmbeddingProvider`` Protocol (``d``, ``w_version``,
    ``encode``, ``encode_batch``) plus the M12 warm contract (``load``, ``loaded``,
    ``name``)."""

    def __init__(
        self, *, model_name: str = DEFAULT_MODEL, w_version: int = 1,
        head: Optional[FrozenPcaHead] = None, head_bytes: Optional[bytes] = None,
        bootstrap_n: int = DEFAULT_BOOTSTRAP_N, model=None,
    ) -> None:
        self.name = str(model_name)
        self.w_version = int(w_version)
        self.d = PROJECTED_DIM
        self._bootstrap_n = int(bootstrap_n)
        self._model = model                # an injected, already-loaded ST (tests) or None
        if head is not None:
            self._head: Optional[FrozenPcaHead] = head
        elif head_bytes is not None:
            self._head = FrozenPcaHead.from_bytes(head_bytes)   # persisted geometry
        else:
            self._head = None              # fit at load() from the bootstrap corpus
        # A supplied head's geometry tag MUST match this embedder's declared w_version — a
        # persisted-head/declared-version mismatch would silently corrupt the §6.1#4 re-embed
        # round-trip (the head says one W_version, the stamp says another). Fail fast, loud.
        if self._head is not None and int(self._head.w_version) != self.w_version:
            raise GeometryError(
                f"head w_version {self._head.w_version} != embedder w_version {self.w_version} "
                "— a persisted head from a different geometry cannot be loaded under this stamp")
        # resident iff BOTH the model and the frozen head are in place
        self.loaded = self._model is not None and self._head is not None

    # ── warm: load the model + freeze the head (the M12 boot.embedder_warm step) ──
    def load(self) -> "LocalSTEmbedder":
        """Bring the embedder RESIDENT: load the SentenceTransformer (CPU) and, if no
        head was supplied, fit + freeze the bootstrap PCA head. Idempotent. Raises on a
        genuine load/fit failure (the entrypoint maps that to EX_UNAVAILABLE=69)."""
        if self._model is None:
            from sentence_transformers import SentenceTransformer  # noqa: PLC0415 — lazy/heavy
            _log.info("embedding.model_load name=%s", self.name)
            self._model = SentenceTransformer(self.name, device="cpu")
        if self._head is None:
            _log.info("embedding.head_fit w_version=%d bootstrap_n=%d native_dim=%d",
                      self.w_version, self._bootstrap_n, NATIVE_DIM)
            self._head = self._fit_bootstrap_head()
        self.loaded = True
        return self

    def head_bytes(self) -> bytes:
        """The frozen head serialized via the HVH1 codec (for persistence to the store
        meta so a restart reuses the SAME geometry). Requires the head to be resident."""
        if self._head is None:
            raise RuntimeError("head not resident — call load() first")
        return self._head.to_bytes()

    # ── encode (the single chain; native bge → head → unit) ──────────────────────
    def encode(self, text: str) -> np.ndarray:
        if not self.loaded:
            self.load()
        native = self._encode_native([text])[0]
        out = self._head(native)           # 384 → 256, re-normalized in the head
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
        """bge-small native embeddings (L2-normalized, float32, (n, 384))."""
        vecs = self._model.encode(
            texts, normalize_embeddings=True, convert_to_numpy=True)
        return np.ascontiguousarray(np.asarray(vecs, dtype=np.float32))

    def _fit_bootstrap_head(self) -> FrozenPcaHead:
        samples = bootstrap_corpus(self._bootstrap_n, salt=self.w_version)
        native = self._encode_native(samples)              # (bootstrap_n, 384)
        return FrozenPcaHead.fit_pca(native, self.w_version)
