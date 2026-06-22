"""build_provider — the embedding swap-seam factory (M01 / registry).

Selects + constructs the configured ``EmbeddingProvider``. The default is the real
``LocalSTEmbedder`` (Qwen3-Embedding-0.6B, baked), which emits the model's native vector
UNCHANGED (no projection / no dimension reduction). ``native_dim_for`` resolves the model's
native dim torch-free, so ``build_container`` can size the index/store BEFORE the heavy model
load; ``load()`` later asserts the real model agrees. A test that needs an embedder double
constructs ``LocalSTEmbedder(model=...)`` directly (or injects it at ``build_container``); this
factory builds the real one for the live system.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from hive.adapters.embedding.local_st import DEFAULT_MODEL, LocalSTEmbedder

if TYPE_CHECKING:
    from hive.app.config import Config

# Native embedding dim per shipped model — resolved torch-free so the index/store dim is known
# at construction (before the deferred model load). ``LocalSTEmbedder.load()`` asserts the real
# model agrees, so this declared value can never silently diverge from the model.
_NATIVE_DIMS: dict[str, int] = {
    DEFAULT_MODEL: 1024,        # Qwen/Qwen3-Embedding-0.6B
}


def native_dim_for(model_name: str) -> int:
    """The model's native embedding dim (the stored-vector dim), known without loading the
    model. Fail-fast on an unregistered model — its native dim must be added here (and the
    weights baked) before it can ship."""
    try:
        return _NATIVE_DIMS[model_name]
    except KeyError:
        raise ValueError(
            f"unknown embedding model {model_name!r}; register its native dim in "
            f"native_dim_for (valid={sorted(_NATIVE_DIMS)})") from None


def build_provider(cfg: "Config") -> LocalSTEmbedder:
    """Construct the real embedder for ``cfg`` (lazy: the model is loaded at
    ``warm_embedder``/``load()``, not here, so construction stays torch-cheap)."""
    return LocalSTEmbedder(
        model_name=cfg.embedding.model, d=native_dim_for(cfg.embedding.model))
