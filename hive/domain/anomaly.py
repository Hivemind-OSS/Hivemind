"""Pure embedding-cluster anomaly detector (DROPPABLE, flag-only).

Flags a candidate that sits in an abnormally COMPACT near-dup cluster — the MINJA
PSS-flood / AgentPoison trigger-cluster signature, where an attacker floods the store with
many near-identical poisoned variants to manufacture demand. Detection-ONLY: the flag is
stamped into the promotion audit and NEVER blocks promotion (THEORY §10 O7 — resolution
stays the human ``hive_supersede`` rail). The recommended-future hold-on-anomaly lever stays
OFF and out of scope.

PURE: stdlib + numpy via ``conflict._cosine`` only. The detector is total and precision-first
— an undecidable (non-finite / shape-mismatch / zero-norm) neighbor is SKIPPED, never counted.
This module is a cleanly-droppable sub-chunk: nothing else depends on it (provenance / n_eff /
the suspect-consensus worklist do not import it).
"""

from __future__ import annotations

from typing import Optional, Sequence

import numpy as np

from hive.domain.conflict import _cosine


def cluster_anomaly(
    candidate_vec: Optional[np.ndarray],
    neighbors: Sequence[Optional[np.ndarray]],
    *,
    tau: float,
    min_cluster: int,
) -> bool:
    """True iff at least ``min_cluster`` neighbors lie within cosine >= ``tau`` of the
    candidate — an abnormally compact near-dup cluster. PURE cosine; an undecidable neighbor
    (None cosine) is SKIPPED, never counted (precision-first); total, never raises. A
    ``min_cluster`` below 1 is a meaningless threshold ⇒ never flags (defensive).  // O(n·d)."""
    if min_cluster < 1:
        return False
    n_close = 0
    for nb in neighbors:
        c = _cosine(candidate_vec, nb)
        if c is None:  # undecidable ⇒ skipped, never counted
            continue
        if c >= tau:
            n_close += 1
            if n_close >= min_cluster:
                return True
    return False
