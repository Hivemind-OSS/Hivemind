"""Demand-gap clustering for ``hive_health(include_gaps=true)`` — a deterministic,
LLM-free report of what the fleet keeps asking for and not getting.

Greedy leader clustering over the windowed miss rows: each vector-bearing miss
joins the FIRST cluster whose leader is within ``tau`` cosine, else founds a new
cluster (insertion order ⇒ deterministic for a given store). Vector-less
``secret_refused`` misses cannot cluster (no content survives the scan) and are
surfaced as one aggregate bucket so refused demand stays visible without leaking
anything. v3: each miss's REPO scope (an optional ``repos`` name sequence on the
row — absent reads as global) rides the report — every gap entry carries the
sorted union of its members' scopes, so unmet demand names the partitions that
asked. App-side and read-only: nothing here writes or promotes.
"""
from __future__ import annotations

from typing import Mapping, Optional, Sequence

import numpy as np


def _row_repos(r: Mapping) -> set[str]:
    """The miss row's repo scope as a name set — total over hostile/absent shapes
    (a feed without the v3 ``repos`` key, e.g. the trends window rows, reads as
    a global miss and contributes nothing)."""
    raw = r.get("repos")
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return set()
    return {x for x in raw if isinstance(x, str) and x}


def _cos(a: "np.ndarray", b: "np.ndarray") -> float:
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na <= 0.0 or nb <= 0.0 or not (np.all(np.isfinite(a)) and np.all(np.isfinite(b))):
        return -1.0                       # undecidable ⇒ never joins a cluster
    return float(np.dot(a, b) / (na * nb))


def _cluster(rows: Sequence[Mapping], *,
             tau: float) -> tuple[list[dict], Optional[dict]]:
    """The shared greedy-leader pass: (vector clusters WITH their ``_vec``
    leader, the vector-less refused aggregate or None).  // O(rows·clusters·d)."""
    clusters: list[dict] = []
    refused: dict | None = None
    for r in rows:
        vec = r.get("vector")
        if vec is None:
            if refused is None:
                refused = {"representative_query": "", "miss_count": 0,
                           "miss_types": {}, "last_seen": 0, "_repos": set()}
            refused["miss_count"] += 1
            refused["miss_types"][r["miss_type"]] = \
                refused["miss_types"].get(r["miss_type"], 0) + 1
            refused["last_seen"] = max(refused["last_seen"], int(r["ts"]))
            refused["_repos"] |= _row_repos(r)
            continue
        for c in clusters:
            if _cos(c["_vec"], vec) >= tau:
                c["miss_count"] += 1
                c["miss_types"][r["miss_type"]] = \
                    c["miss_types"].get(r["miss_type"], 0) + 1
                c["last_seen"] = max(c["last_seen"], int(r["ts"]))
                c["_repos"] |= _row_repos(r)
                break
        else:
            clusters.append({"_vec": vec, "representative_query": r["query_text"],
                             "miss_count": 1, "miss_types": {r["miss_type"]: 1},
                             "last_seen": int(r["ts"]),
                             "_repos": _row_repos(r)})
    return clusters, refused


def cluster_misses(rows: Sequence[Mapping], *, tau: float,
                   top_n: int = 10) -> list[dict]:
    """Cluster miss rows (``{query_text, vector, miss_type, ts, repos?}``) into at
    most ``top_n`` demand gaps, ordered by miss_count desc then recency desc. Each
    gap: ``{representative_query, miss_count, miss_types, last_seen, repos}`` — the
    representative is the cluster founder's text; ``repos`` is the sorted union of
    the members' repo scopes ([] = purely global demand).
    // O(rows · clusters · d)."""
    clusters, refused = _cluster(rows, tau=tau)
    out = clusters + ([refused] if refused is not None else [])
    out.sort(key=lambda c: (-c["miss_count"], -c["last_seen"]))
    return [{**{k: v for k, v in c.items() if k not in ("_vec", "_repos")},
             "repos": sorted(c["_repos"])} for c in out[:top_n]]
