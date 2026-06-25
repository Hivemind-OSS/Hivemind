"""S3 — the store's-own-verdict DIAGNOSTIC carriers (the firewall's off-gold side).

These reduce raw run observations — the recall envelopes an agent saw, the retirement-result
dicts the orchestrator issued, the servable sets per step — into the "what did the store actually
do" sets that the Pareto scorers consume as their SUBJECT. They are the diagnostic that explains
WHY a regime won or lost; they are deliberately kept in a module the gold scorer (``scoring``)
does NOT import, so the store's "is it retired/served" verdict can never become the gold (§5,
AC6).

Defensive by construction (BUG-002/003/005 class): a ``noop`` / ``refused`` retirement result, a
``superseded`` with no loser, or a recall hit for an unmapped episode contributes NOTHING rather
than raising — the same posture the ``McpHttpClient`` parse takes.
"""
from __future__ import annotations

from typing import Iterable, Mapping, Sequence


def served_sources_by_task(envelopes_by_task: Sequence[Sequence[Mapping]],
                           source_by_eid: Mapping[int, str]) -> list[set[str]]:
    """Per task, the set of labeled source ids that entered served context — read from each recall
    envelope's ``reference_context`` hits, mapped ``episode_id → source_id``. An abstain (empty
    ``reference_context``) serves nothing; a hit for an episode with no known source (an organic
    capture, say) contributes nothing to the LABELED-served set. // O(tasks · hits)."""
    out: list[set[str]] = []
    for envelopes in envelopes_by_task:
        served: set[str] = set()
        for env in envelopes:
            for hit in (env.get("reference_context") or []):
                src = source_by_eid.get(hit.get("episode_id"))
                if src is not None:
                    served.add(src)
        out.append(served)
    return out


def retired_sources(results: Iterable[Mapping],
                    source_by_eid: Mapping[int, str]) -> set[str]:
    """The set of labeled source ids actually retired, read from the orchestrator's
    ``hive_prune`` / ``hive_supersede`` result dicts: a ``{status:"pruned", episode_id}`` retires
    that episode, a ``{status:"superseded", loser}`` retires the loser. A ``noop`` / ``refused``
    result (no real retirement) and an unmapped episode contribute nothing. // O(results)."""
    out: set[str] = set()
    for res in results:
        status = res.get("status")
        eid = None
        if status == "pruned":
            eid = res.get("episode_id")
        elif status == "superseded":
            eid = res.get("loser")
        if eid is None:
            continue
        src = source_by_eid.get(eid)
        if src is not None:
            out.add(src)
    return out


def store_size_curve(servable_by_step: Sequence[Iterable[str]]) -> list[int]:
    """The bloat curve: total servable rows per step (reported, never optimised — a scalar a
    system games by pruning everything). // O(steps · servable)."""
    return [len(set(step)) for step in servable_by_step]
