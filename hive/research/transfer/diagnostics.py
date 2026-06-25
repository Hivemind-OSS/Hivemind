"""Diagnostic chain decomposition — the store's-own view of WHERE a transfer chain broke (Chunk 4).

When a ``recall_on`` pair fails, these reducers say which link broke: the upstream agent captured the
wrong fact (``capture_fidelity``), the fact never became servable (``promoted_servable``), or the
downstream recall did not surface it (``served_correct``). All three are the SAME reduction — "did any
observed text carry the ground-truth value" — over different observation sources, so the workhorse is
``carries_value`` and the three stages are one ``chain_decomposition`` call.

This is the DIAGNOSTIC side of the gold/diagnostic firewall: ``scoring.py`` must NOT import it (a
purity test pins the non-import), so a per-system "where it broke" signal can never become the gold
"did it pass". It is a fail-soft side-channel (Law 6): an empty or unlabeled observation contributes
nothing rather than raising.
"""
from __future__ import annotations

from typing import Iterable, Mapping

from hive.research.transfer.substrate import _tokens


def carries_value(texts_by_pair: Mapping[str, Iterable[str]],
                  value_by_pair: Mapping[str, str]) -> dict[str, bool]:
    """Per pair: did ANY observed text carry the ground-truth value (its tokens are a subset of the
    text's)? A pair with no ground-truth label contributes nothing (fail-soft, no raise)."""
    out: dict[str, bool] = {}
    for pid, texts in texts_by_pair.items():
        if pid not in value_by_pair:
            continue
        want = _tokens(value_by_pair[pid])
        out[pid] = any(want <= _tokens(t) for t in texts)
    return out


def chain_decomposition(*, captured: Mapping[str, Iterable[str]],
                        servable: Mapping[str, Iterable[str]],
                        served: Mapping[str, Iterable[str]],
                        value_by_pair: Mapping[str, str]) -> dict:
    """Decompose the transfer chain into its three observable links, each ``{pair_id: bool}``:
    ``capture_fidelity`` (an upstream capture carried the true value), ``promoted_servable`` (a
    servable memory carried it at the downstream turn), ``served_correct`` (the downstream recall
    actually surfaced it)."""
    return {
        "capture_fidelity": carries_value(captured, value_by_pair),
        "promoted_servable": carries_value(servable, value_by_pair),
        "served_correct": carries_value(served, value_by_pair),
    }
