"""[C5] — clean-store geometry re-embed (off the hot path).

``reembed_from_text`` is the clean-store geometry rewrite: a W_version bump re-projects
every APPROVED row's ``value`` from its blob text through the new head. It does NOT
re-scan — the store was already scanned at admission, and the text / content_hash /
status are left untouched; only ``value`` is rewritten. Re-scanning a trusted store would
be a redundant floor pass that the [C5] decision rejects.
"""
from __future__ import annotations

import logging
from typing import Any

import numpy as np

from hive.domain.errors import ReembedError

_log = logging.getLogger("hive.migration")


def reembed_from_text(store: Any, *, embedder: Any, scanner: Any = None) -> int:
    """Clean-store geometry rewrite [C5]: re-embed every APPROVED row from its blob text
    through ``embedder`` and rewrite ``value`` ONLY. Returns the count re-projected.
    Does NOT re-scan (``scanner`` is accepted for call-site symmetry but is NEVER
    invoked — a trusted store does not get a second floor pass); text / content_hash /
    status are untouched. // O(A · encode) time, A = #approved rows.

    Each re-projected vector is validated FINITE + 1-D BEFORE it is written, and the whole
    rewrite runs in ONE transaction: a non-finite embedder output (e.g. a zero vector
    normalized to NaN) raises ``ReembedError`` and rolls the batch back UN-mutated rather
    than persisting a NaN/Inf BLOB that would later crash ``rebuild_index_from_store``'s
    finiteness guard and strand the store with a cleared index over corrupt rows."""
    conn = store.conn
    rows = conn.execute(
        "SELECT id, text FROM episodes WHERE status='approved'").fetchall()
    n = 0
    with store.transaction():                        # atomic: a bad vector rolls back the rewrite
        for r in rows:
            value = np.asarray(embedder.encode(r["text"]), dtype=np.float32)
            if value.ndim != 1 or not bool(np.all(np.isfinite(value))):
                _log.error("reembed.nonfinite_vector episode_id=%s ndim=%d w_version=%s",
                           r["id"], value.ndim, getattr(embedder, "w_version", "?"))
                raise ReembedError(
                    f"embedder returned a non-finite or ill-shaped vector for episode "
                    f"{r['id']} (ndim={value.ndim}) — refusing to persist a corrupt value")
            vbytes = np.ascontiguousarray(value).tobytes()
            conn.execute("UPDATE episodes SET value=? WHERE id=?", (vbytes, r["id"]))
            n += 1
    store.rebuild_index_from_store()                 # warm-cache rebuild from approved-only [B3]
    _log.info("reembed.complete n_reprojected=%d w_version=%s", n,
              getattr(embedder, "w_version", "?"))
    return n
