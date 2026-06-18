"""M11 — the embedder/index/gate wiring: a single fail-fast `build_*` per seam.

The embedder and index each have ONE measured-right backend (local_st / exhaustive),
so each seam is a direct `build_*` that fails fast on any other provider string — a
second provider is a one-function edit (add the branch, register the adapter), never a
generic provider-registry an operator could mis-set (§9.14: encode the one right answer).
`build_gate(cfg)` is the ONE located owner that passes the frozen `cfg.recall` object BY
IDENTITY into the abstention gate — the floor physically cannot fork (CONFIG_DRIFT killed
structurally; M11).

The heavy adapter import is LAZY: Config validation is cheap and torch-free, while the
SentenceTransformer load happens when `build_embedder` actually invokes.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from hive.adapters import index_exhaustive
from hive.domain.recall import NormalizedEntropyGate

if TYPE_CHECKING:                       # avoid an import cycle (config lazily imports us)
    from hive.app.config import Config

_log = logging.getLogger("hive.registry")


# ── embedder seam (C1) ────────────────────────────────────────────────────────
def _build_local_st(cfg: "Config", *, head_bytes=None, model=None):
    """Default embedder (M01 LocalSTEmbedder, bge-small). Lazy: the factory import is
    torch-free and the heavy SentenceTransformer load is deferred to ``load()``/warm, so
    Config validation + ``build_container`` construction stay light. ``head_bytes`` (the
    persisted PCA geometry) and ``model`` (a test seam) are threaded through when given."""
    try:
        from hive.adapters.embedding.factory import build_provider  # noqa: PLC0415 — lazy
    except ImportError as exc:
        _log.error("registry.embedder_unavailable provider=local_st reason=%s",
                   type(exc).__name__)
        raise RuntimeError(
            "embedding.provider='local_st' needs sentence-transformers; install hive[embed] "
            "(the M01 adapter), or register another provider") from exc
    return build_provider(cfg, head_bytes=head_bytes, model=model)


def build_embedder(cfg: "Config", **kwargs: Any):
    """Construct the configured embedder; fail fast (ValueError) on any provider other than
    the one measured-right default — the likely agent typo, caught at startup not at first
    recall. ``kwargs`` (e.g. ``head_bytes`` — the persisted PCA geometry — or ``model`` — a
    test seam) are forwarded; the composition root threads them, the registry only selects +
    fails fast."""
    name = cfg.embedding.provider
    if name != "local_st":
        raise ValueError(
            f"unknown embedding provider {name!r}; valid=['local_st']")
    _log.info("registry.build_embedder provider=%s model=%s", name, cfg.embedding.model)
    return _build_local_st(cfg, **kwargs)


# ── vector-index seam (C5/C3) ─────────────────────────────────────────────────
def build_index(cfg: "Config"):
    """Construct the exhaustive index; ASSERT the authoritative-index property holds
    (exhaustive never silently flips to ANN — the build-time closure of the
    approx_threshold trap; Law 5 never-flip-to-ANN guard)."""
    idx = index_exhaustive.build_index("exhaustive", cfg.geometry.d)
    if idx.is_authoritative() is not True:
        raise AssertionError(
            "build_index postcondition violated: exhaustive backend is not authoritative")
    return idx


# ── the abstention gate (floor-by-identity) ───────────────────────────────────
def build_gate(cfg: "Config") -> NormalizedEntropyGate:
    """THE single located owner of the never-hallucinate floor wiring. The frozen
    `cfg.recall` object is handed to the gate BY IDENTITY (not `cfg.recall.H_frac_max`
    copied as a float) so a future second gate cannot fork the floor — `from_recall`
    reads both the entropy floor and the additive `tau_top1` top-1 floor off that one
    object. Invariant: `build_gate(cfg)._recall is cfg.recall` (pinned by test + mutation)."""
    return NormalizedEntropyGate.from_recall(cfg.recall, cfg.recall.softmax_beta)
