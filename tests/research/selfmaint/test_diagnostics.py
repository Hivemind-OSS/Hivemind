"""S3 — the store's-own-verdict DIAGNOSTIC carriers (the firewall's off-gold side).

These derive the "what did the store actually do" observations — which source_ids it served,
which it retired, how big it grew — from raw recall envelopes / retirement-result dicts. They
explain WHY a regime won or lost; they NEVER enter the gold (the gold scorer in ``scoring`` does
not import this module). Defensive by construction: a ``noop`` / ``refused`` retirement result,
or a hit for an unmapped episode, contributes nothing rather than crashing.
"""
from __future__ import annotations

from hive.research.selfmaint.diagnostics import (
    retired_sources, served_sources_by_task, store_size_curve,
)


def test_served_sources_by_task_maps_reference_context_hits():
    envelopes_by_task = [
        [{"reference_context": [{"episode_id": 10}, {"episode_id": 11}]}],
        [{"reference_context": []}],                         # an abstain serves nothing
        [{"reference_context": [{"episode_id": 99}]}],       # 99 is unmapped ⇒ contributes nothing
    ]
    out = served_sources_by_task(envelopes_by_task, {10: "v1", 11: "b1"})
    assert out == [{"v1", "b1"}, set(), set()]


def test_retired_sources_reads_prune_and_supersede_skips_noop():
    results = [
        {"status": "pruned", "episode_id": 10},
        {"status": "noop", "episode_id": 11, "reason": "unknown id"},   # retired nothing
        {"status": "superseded", "loser": 12, "winner": 13},
        {"status": "refused", "agi_mode": False},            # no id at all
    ]
    out = retired_sources(results, {10: "v1", 12: "b1", 13: "v2"})
    assert out == {"v1", "b1"}                               # pruned 10 + superseded loser 12


def test_store_size_curve_is_servable_count_per_step():
    assert store_size_curve([{"a", "b"}, {"a"}, set()]) == [2, 1, 0]
