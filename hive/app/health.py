"""M11 — the `hive_health` snapshot: cheap, poll-safe, and NEVER-raising.

`health(cfg, store, embedder)` fails SOFT to `ok=False` on ANY probe failure (store /
embedder) so a container HEALTHCHECK can never itself crash. Includes `embedder_loaded`
(present + boolean; False until the model is resident — a container is not healthy before
the model is in RAM) and `index_authoritative` (the §4.3 property). The producer-tick /
watch-repos fields were removed with the producer subsystem.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any, Callable, Optional

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
    # M07 additive link surfacing. ``linked`` is None UNTIL a repo_path is probed; in
    # that state as_dict() OMITS both keys so the no-repo_path payload is byte-identical
    # to the pre-M07 snapshot (the "tool count / no-token discipline" of the M07 spec).
    linked: Optional[bool] = None
    link: Optional[dict] = None

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if self.linked is None:                    # unprobed → drop link keys (byte-identical)
            d.pop("linked", None)
            d.pop("link", None)
        return d


def health(cfg: "Config", store: Any, embedder: Any, *,
           repo_path: Optional[str] = None,
           link_status: Optional[Callable[[str], tuple[bool, Optional[dict]]]] = None,
           ) -> HealthSnapshot:
    """Probe the live components; fail-soft. // O(N) for the one grouped episode count.

    M07: when ``repo_path`` is given, surface the onboarding link (``linked``/``link``)
    via the injected ``link_status`` probe (the InstallPlanner's ``link_status``). With
    NO ``repo_path`` the link keys are absent (``linked`` stays None) — the snapshot is
    byte-identical to the pre-M07 payload. The link probe NEVER raises the snapshot to
    ok=False (it is additive, fail-soft to unlinked)."""
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

    linked: Optional[bool] = None
    link: Optional[dict] = None
    if repo_path is not None:                       # additive link probe; fail-soft to unlinked
        try:
            if link_status is not None:
                probed, link = link_status(repo_path)
                linked = bool(probed)
            else:
                linked, link = False, None
        except Exception:                           # noqa: BLE001 — link probe never flips ok
            _log.warning("health.link_probe_failed (reported unlinked)")
            linked, link = False, None

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
        linked=linked,
        link=link,
    )


def _safe(fn, default):
    try:
        return fn()
    except Exception:                              # noqa: BLE001 — even config reads fail soft
        return default
