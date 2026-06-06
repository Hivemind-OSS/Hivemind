"""M11 — the three swap seams as plain, greppable dicts + fail-fast `build_*`.

Adding a provider is ONE dict entry + one adapter file, with zero core change (the spec §10
swap mandate made literal: every registration is visible at one read, not hidden behind
decorator magic). `build_gate(cfg)` is the ONE located owner that passes the frozen
`cfg.recall` object BY IDENTITY into the abstention gate — the floor physically cannot fork
(CONFIG_DRIFT killed structurally; M11 §2.3).

Registry values are LAZY constructors: Config validation checks only key-presence (cheap,
torch-free), while the heavy adapter import happens when `build_*` actually invokes.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable

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


EMBEDDING_PROVIDERS: dict[str, Callable[..., Any]] = {
    "local_st": _build_local_st,
}


def build_embedder(cfg: "Config", **kwargs: Any):
    """Construct the configured embedder; fail fast (ValueError, valid keys) on an unknown
    seam string — the likely agent typo, caught at startup not at first recall. ``kwargs``
    (e.g. ``head_bytes`` — the persisted PCA geometry — or ``model`` — a test seam) are
    forwarded to the selected ctor; the composition root threads them, the registry only
    selects + fails fast."""
    name = cfg.embedding.provider
    ctor = EMBEDDING_PROVIDERS.get(name)
    if ctor is None:
        raise ValueError(
            f"unknown embedding provider {name!r}; valid={sorted(EMBEDDING_PROVIDERS)}")
    _log.info("registry.build_embedder provider=%s model=%s", name, cfg.embedding.model)
    return ctor(cfg, **kwargs)


# ── vector-index seam (C5/C3) ─────────────────────────────────────────────────
def _build_exhaustive(cfg: "Config"):
    return index_exhaustive.build_index("exhaustive", cfg.geometry.d)


INDEX_PROVIDERS: dict[str, Callable[..., Any]] = {
    "exhaustive": _build_exhaustive,
}


def build_index(cfg: "Config"):
    """Construct the configured index; ASSERT the §4.3 authoritative property holds for an
    authoritative backend (exhaustive never silently flips to ANN — the build-time closure of
    the approx_threshold trap)."""
    name = cfg.index.backend
    ctor = INDEX_PROVIDERS.get(name)
    if ctor is None:
        raise ValueError(f"unknown index backend {name!r}; valid={sorted(INDEX_PROVIDERS)}")
    idx = ctor(cfg)
    if name == "exhaustive" and idx.is_authoritative() is not True:
        raise AssertionError(
            "build_index postcondition violated: exhaustive backend is not authoritative")
    if idx.is_authoritative() is not True:
        _log.warning("registry.index_approximate backend=%s; exact-eval recall not guaranteed",
                     name)
    return idx


# ── outcome-producer seam (C10) ───────────────────────────────────────────────
def _build_git_inproc(cfg: "Config", **deps: Any):
    from hive.domain.produce import OutcomeProducer
    return OutcomeProducer(**deps)


PRODUCER_PROVIDERS: dict[str, Callable[..., Any]] = {
    "git_inproc": _build_git_inproc,
}


def build_producer(cfg: "Config", **deps: Any):
    """Construct the configured producer; fail fast on an unknown seam string. The runtime
    deps (source/joiner/attributor/store/utility_store/clock) are passed through by the
    composition root (wired in a later chunk); the registry only selects + fails fast."""
    name = cfg.producer.provider
    ctor = PRODUCER_PROVIDERS.get(name)
    if ctor is None:
        raise ValueError(
            f"unknown producer provider {name!r}; valid={sorted(PRODUCER_PROVIDERS)}")
    return ctor(cfg, **deps)


# ── the abstention gate (floor-by-identity) ───────────────────────────────────
def build_gate(cfg: "Config") -> NormalizedEntropyGate:
    """THE single located owner of the never-hallucinate floor wiring. The frozen
    `cfg.recall` object is handed to the gate BY IDENTITY (not `cfg.recall.H_frac_max`
    copied as a float) so a future second gate cannot fork the floor. Invariant:
    `build_gate(cfg)._recall is cfg.recall` (pinned by test + RULE-2 mutation)."""
    return NormalizedEntropyGate.from_recall(cfg.recall, cfg.recall.softmax_beta)
