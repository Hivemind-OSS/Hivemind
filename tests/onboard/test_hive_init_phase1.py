"""P1.12 / M07 — hive_init phase-1: purity (zero writes) + rules-file resolution.
The producer watch_repos / watch_warning coupling was removed with the producer.
"""
from __future__ import annotations

import pytest

from hive.adapters.sqlite_db import connect
from hive.adapters.store_sqlite import SqliteEpisodeStore
from hive.app.onboard import InstallPlanner
from hive.domain.ports import InstallPlanner as InstallPlannerPort


def _store():
    return SqliteEpisodeStore(connect(":memory:"))


def _planner(store=None, *, trailer="Hive-Trace"):
    return InstallPlanner(store or _store(), stamp_trailer=trailer)


def test_planner_conforms_to_port():
    # the real adapter satisfies the port the MCP surface drives (not just the fake)
    assert isinstance(_planner(), InstallPlannerPort)


def test_phase1_returns_plan_writes_nothing(tmp_path):
    store = _store()
    plan = _planner(store).plan(str(tmp_path), "claude-code")
    assert plan.expected_confirm_hash == plan.rules_block.block_hash
    assert plan.harness == "claude-code"
    # zero writes: no link key persisted by phase-1
    assert store.meta_get(f"hive_init:link:{tmp_path}") is None


def test_phase1_resolves_primary_rules_file(tmp_path):
    (tmp_path / "AGENTS.md").write_text("x")             # only AGENTS.md exists
    plan = _planner().plan(str(tmp_path), "generic")
    assert plan.rules_file == "AGENTS.md"


def test_phase1_priority_first_existing_wins(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("x")
    (tmp_path / "AGENTS.md").write_text("y")
    plan = _planner().plan(str(tmp_path), "generic")
    assert plan.rules_file == "CLAUDE.md"                # CLAUDE.md outranks AGENTS.md


def test_phase1_fallback_when_no_candidate_exists(tmp_path):
    plan = _planner().plan(str(tmp_path), "generic")
    assert plan.rules_file == "CLAUDE.md"                # create-fallback


def test_phase1_explicit_rules_file_overrides(tmp_path):
    (tmp_path / "AGENTS.md").write_text("x")
    plan = _planner().plan(str(tmp_path), "generic", ".cursorrules")
    assert plan.rules_file == ".cursorrules"


def test_phase1_not_a_dir_fails_fast(tmp_path):
    with pytest.raises(ValueError):
        _planner().plan(str(tmp_path / "does-not-exist"), "generic")


def test_phase1_empty_trailer_fails_fast():
    with pytest.raises(ValueError):
        InstallPlanner(_store(), stamp_trailer="")       # no silent empty trailer


def test_phase1_plan_carries_manifest_and_recipe(tmp_path):
    plan = _planner().plan(str(tmp_path), "claude-code")
    assert plan.manifest.manifest_version >= 1 and plan.manifest.hooks
    assert plan.recipe.harness == "claude-code" and plan.recipe.resolved_tier == 2
    assert plan.recipe.hook_files                        # Tier-2 host → at least one hook file


def test_phase1_rules_file_resolution_is_harness_aware(tmp_path):
    # only .cursorrules exists; claude-code's candidate is CLAUDE.md, so it ignores the
    # cursor file and falls back to its own create-target — the profile drives resolution.
    (tmp_path / ".cursorrules").write_text("x")
    assert _planner().plan(str(tmp_path), "claude-code").rules_file == "CLAUDE.md"
    assert _planner().plan(str(tmp_path), "cursor").rules_file == ".cursorrules"
