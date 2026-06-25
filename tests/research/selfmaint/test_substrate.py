"""S2 — the scenario seeder: the 3-class taxonomy, off-is-inert, anti-telegraph reuse, the
BUG-006 clean fallback, and the BUG-002-safe servable establish.

Seeded memories are labeled by provenance ``source_id`` ONLY — the agent never sees a "this is
bad" tag; a prunable-bad seed is a work-falsifiable wrong value (token-disjoint from the true
value, no self-telegraph) the agent must EARN detecting by hitting the contradiction in its real
task. The seeder is repo-parameterized; a self-contained ``fixture_repo_window`` drives the
offline tests + the no-API smoke.
"""
from __future__ import annotations

import pytest

from hive.research.selfmaint import substrate
from hive.research.selfmaint.daemon import McpHttpClient
from hive.research.selfmaint.substrate import (
    SeedSpec, SelfMaintTaskCase, build_selfmaint_substrate, fixture_repo_window, seed_store,
)

from ._selfmaint_helpers import live_daemon


# ── the 3-class taxonomy + anti-telegraph ───────────────────────────────────────────
def test_build_seeds_all_three_regimes():
    specs, tasks = build_selfmaint_substrate(fixture_repo_window(), seed=7)
    by_regime = {s.regime for s in specs}
    assert by_regime == {"prunable_bad", "keep_neutral", "keep_valuable"}
    assert all(isinstance(t, SelfMaintTaskCase) for t in tasks)


def test_prunable_bad_is_work_falsifiable_and_not_telegraphed():
    specs, _ = build_selfmaint_substrate(fixture_repo_window(), seed=7)
    bad = next(s for s in specs if s.regime == "prunable_bad")
    # the seed asserts the FALSE value, token-disjoint from the true value, and never echoes it.
    assert bad.false_value and bad.false_value != bad.true_value
    assert bad.false_value in bad.text and bad.true_value not in bad.text
    for tell in ("wrong", "fake", "ignore", "synthetic", "poison"):
        assert tell not in bad.text.lower()


def test_keep_regimes_carry_the_true_value_and_no_false():
    specs, _ = build_selfmaint_substrate(fixture_repo_window(), seed=7)
    for s in specs:
        if s.regime in ("keep_neutral", "keep_valuable"):
            assert s.false_value is None and s.true_value in s.text


# ── off-is-inert: empty regimes ⇒ a clean, un-seeded sequence ────────────────────────
def test_off_is_inert_empty_regimes_seeds_nothing():
    specs, tasks = build_selfmaint_substrate(fixture_repo_window(), seed=7, regimes=())
    assert specs == []
    # the task sequence survives but references no seeded fact (clean run).
    assert all(not t.needs_valuable and not t.falsified_by for t in tasks)


# ── BUG-006: an unsynthesizable falsehood falls back to clean, never aborts ──────────
def test_unsynthesizable_bad_fact_falls_back_clean(monkeypatch):
    # force every false-value synthesis to raise — the bad seed must be SKIPPED while the keep
    # seeds still build (mirrors poison_substrate's per-case try/except). Mutation: narrow the
    # except so ValueError escapes → the whole build aborts → this reds.
    def _boom(*a, **k):
        raise ValueError("cannot synthesize a sound falsehood")
    monkeypatch.setattr(substrate, "_false_value_for", _boom)
    specs, tasks = build_selfmaint_substrate(fixture_repo_window(), seed=7)
    assert not any(s.regime == "prunable_bad" for s in specs)        # the bad seed was dropped
    assert any(s.regime == "keep_valuable" for s in specs)           # the build did NOT abort
    # a task that was falsified only by the dropped bad fact now references no seeded bad.
    assert all(not t.falsified_by for t in tasks)


def test_task_cases_reference_only_successfully_seeded_facts():
    specs, tasks = build_selfmaint_substrate(fixture_repo_window(), seed=7)
    seeded = {s.source_id for s in specs}
    for t in tasks:
        assert t.needs_valuable <= seeded and t.falsified_by <= seeded


# ── SeedSpec is a self-asserting carrier ────────────────────────────────────────────
def test_seedspec_rejects_token_overlapping_bad_values():
    with pytest.raises(ValueError):
        SeedSpec(source_id="x", text="parse takes 5", regime="prunable_bad", kind="stale",
                 true_value="five args", false_value="five things")   # share token "five"


# ── seed_store: establish servable, return source_id → episode_id (BUG-002 safe) ─────
def test_seed_store_establishes_servable_and_maps_sources():
    url, _server, _clock, stop = live_daemon()
    try:
        client = McpHttpClient(url, agent_id="orchestrator")
        specs, _ = build_selfmaint_substrate(fixture_repo_window(), seed=7)
        mapping = seed_store(client, specs, approver="human")
        assert set(mapping) == {s.source_id for s in specs}         # every seed established
        bad = next(s for s in specs if s.regime == "prunable_bad")
        served = [h["episode_id"] for h in client.recall(bad.text)["reference_context"]]
        assert mapping[bad.source_id] in served                     # genuinely servable in every arm
    finally:
        stop()


def test_seed_store_skips_a_refused_write_without_empty_handle():
    # a secret-shaped seed is refused at the floor (no "id"); seed_store must SKIP it, never leak
    # an empty handle (BUG-002). Mutation: read res["id"] unconditionally → KeyError → this reds.
    url, _server, _clock, stop = live_daemon()
    try:
        client = McpHttpClient(url, agent_id="orchestrator")
        secret = SeedSpec(source_id="sec::token", regime="keep_neutral", kind="note",
                          text="deploy token sk-proj-0aZ9bY8cX7dW6eV5fU4tS3rR2qQ1pP0o keep it",
                          true_value="x")
        mapping = seed_store(client, [secret], approver="human")
        assert mapping == {}                                        # refused ⇒ nothing mapped
    finally:
        stop()
