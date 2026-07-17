"""Graph-propagated staleness: the receipt's optional propagation block.

For every breaking/removed regression seed, the blast radius already names the
engine node ids of the callers, dependents, and tests built against the old
contract. This module renders that neighbourhood in the receipt's public
vocabulary — ``path::Symbol`` strings resolved against the HEAD graph's node
table — so the kernel can join neighbours against episode anchors without ever
learning the engine's node-id scheme (one owner; the ids stay census/matrix
side). A node the head graph cannot name — a removed symbol, a file's own
node, an unknown id — is dropped: the block under-claims, never guesses. The
seed's own subject never rides as its own neighbour (it is already point
evidence). Detection fuel only: the kernel writes advisory ``stale_suspect``
rows from this block; nothing on either side retires a memory.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hive.census.engines import GraphPair, node_subject

if TYPE_CHECKING:  # annotation target only; the carrier arrives duck-readable
    from hive.census.join import RegressionFinding

# The only drifts that justify suspicion — a benign (additive/unchanged) change
# casts none on its neighbourhood.
_PROPAGATION_DRIFTS = frozenset({"breaking", "removed"})


def propagation_block(
    findings: tuple[RegressionFinding, ...],
    graphs: GraphPair,
    *,
    cap_neighbors: int = 32,
) -> list[dict]:
    """One entry per breaking/removed seed with >= 1 head-resolvable neighbour.

    Every cross-carrier field is read defensively (a malformed finding simply
    contributes nothing). Neighbours are the union of the finding's callers,
    dependents, and tests, deduplicated, seed-excluded, sorted, and capped at
    ``cap_neighbors`` — deterministic bytes for a reproducible artifact. An abstained
    finding (no seed, no impact hits) and a graph pair without a head graph
    degrade to nothing.
    """
    head_nx = getattr(getattr(graphs, "head", None), "nx", None)
    if head_nx is None:
        return []
    # The head side's path-dialect offset: neighbours must be spelled in the
    # repo-relative dialect episode anchors join on (BUG-038); a carrier
    # without one degrades to "" — the graph dialect, the pre-bridge bytes.
    head_offset = str(getattr(graphs, "head_offset", "") or "")
    entries: list[dict] = []
    for finding in findings or ():
        drift = str(getattr(finding, "drift", "") or "")
        if drift not in _PROPAGATION_DRIFTS:
            continue
        path = str(getattr(finding, "path", "") or "")
        symbol = str(getattr(finding, "symbol", "") or "")
        if not path or not symbol:
            continue
        seed_subject = f"{path}::{symbol}"
        neighbours: set[str] = set()
        for field in ("callers", "dependents", "tests"):
            for node_id in getattr(finding, field, ()) or ():
                subject = node_subject(head_nx, str(node_id), offset=head_offset)
                if subject is not None and subject != seed_subject:
                    neighbours.add(subject)
        if not neighbours:
            continue
        entries.append(
            {
                "seed": seed_subject,
                "drift": drift,
                "depth": int(getattr(finding, "depth", 0) or 0),
                "neighbors": sorted(neighbours)[: max(cap_neighbors, 0)],
            }
        )
    return entries
