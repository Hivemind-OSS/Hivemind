"""M11 — the `hive_health` snapshot: cheap, poll-safe, and NEVER-raising.

`health(cfg, store, embedder)` fails SOFT to `ok=False` on ANY probe failure (store /
embedder) so a container HEALTHCHECK can never itself crash. Includes `embedder_loaded`
(present + boolean; False until the model is resident — a container is not healthy before
the model is in RAM) and `index_authoritative` (the authoritative-index property). The producer-tick /
watch-repos fields were removed with the producer subsystem.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from hive.app.config import Config

_log = logging.getLogger("hive.health")

_AUTHORITATIVE_BACKENDS = frozenset({"exhaustive"})


@dataclass(frozen=True)
class HealthSnapshot:
    ok: bool
    db_path: str
    tenant_id: str
    embedder_loaded: bool
    w_version: int
    index_authoritative: bool
    episodes_approved: int
    episodes_pending: int
    error: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def health(cfg: "Config", store: Any, embedder: Any) -> HealthSnapshot:
    """Probe the live components; fail-soft. // O(N) for the one grouped episode count."""
    db_path = _safe(lambda: cfg.runtime.db_path, "?")
    tenant_id = _safe(lambda: cfg.runtime.tenant_id, "default")
    index_authoritative = _safe(
        lambda: cfg.index.backend in _AUTHORITATIVE_BACKENDS, False)

    error: Optional[str] = None
    try:
        embedder_loaded = bool(getattr(embedder, "loaded", False))
        w_version = int(getattr(embedder, "w_version", 0))
        approved, pending = store.counts()
        ok = True
    except Exception as exc:                       # noqa: BLE001 — health NEVER raises
        # type name only; a probe error message could in principle echo a path/value
        error = f"{type(exc).__name__}"
        _log.error("health.probe_failed kind=%s db_path=%s", error, db_path)
        embedder_loaded = False
        w_version = 0
        approved = pending = 0
        ok = False

    return HealthSnapshot(
        ok=ok,
        db_path=str(db_path),
        tenant_id=str(tenant_id),
        embedder_loaded=embedder_loaded,
        w_version=w_version,
        index_authoritative=bool(index_authoritative),
        episodes_approved=int(approved),
        episodes_pending=int(pending),
        error=error,
    )


def _safe(fn, default):
    try:
        return fn()
    except Exception:                              # noqa: BLE001 — even config reads fail soft
        return default
