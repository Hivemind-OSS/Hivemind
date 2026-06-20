"""build_provider — the embedding swap-seam factory (M01 / registry).

Selects + constructs the configured ``EmbeddingProvider``. The default is the real
``LocalSTEmbedder`` (Qwen3-Embedding-0.6B, baked). ``head_bytes`` (the persisted, W_version'd
truncation geometry from the store meta) is threaded through so a restart reuses the same
head; absent it, the embedder builds the truncation head from the model's native dim at
``load()``. ``model`` is an injection seam for tests (pass an already-loaded
SentenceTransformer / a double) so the provider can be exercised without re-loading the model.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from hive.adapters.embedding.local_st import LocalSTEmbedder

if TYPE_CHECKING:
    from hive.app.config import Config


def build_provider(cfg: "Config", *, head_bytes: Optional[bytes] = None,
                   model=None) -> LocalSTEmbedder:
    """Construct the real embedder for ``cfg`` (lazy: the model + head are loaded at
    ``warm_embedder``/``load()``, not here, so construction stays torch-cheap)."""
    return LocalSTEmbedder(
        model_name=cfg.embedding.model, w_version=cfg.geometry.W_version,
        d=cfg.geometry.d, head_bytes=head_bytes, model=model)
