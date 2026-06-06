"""P1.12 / M07 — hive_init phase-1: purity (zero writes) + rules-file resolution +
watch_warning (non-blocking) + the rejected-coupling guard (producer config unchanged).
"""
from __future__ import annotations

import pytest

from hive.adapters.sqlite_db import connect
from hive.adapters.store_sqlite import SqliteEpisodeStore
from hive.app.onboard import InstallPlanner
from hive.domain.ports import InstallPlanner as InstallPlannerPort


def _store():
    return SqliteEpisodeStore(connect(":memory:"))


def _planner(store=None, *, trailer="Hive-Trace", watch=()):
    return InstallPlanner(store or _store(), stamp_trailer=trailer, watch_repos=watch)


def test_planner_conforms_to_port():
    # the real adapter satisfies the port the MCP surface drives (not just the fake)
    assert isinstance(_planner(), InstallPlannerPort)


def test_phase1_returns_plan_writes_nothing(tmp_path):
    store = _store()
    plan = _planner(store, watch=(str(tmp_path),)).plan(str(tmp_path), "claude-code")
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


def test_phase1_emits_watch_warning_unwatched(tmp_path):
    plan = _planner(watch=("/some/other/repo",)).plan(str(tmp_path), "generic")
    assert plan.watch_warning is not None


def test_phase1_no_warning_when_watched(tmp_path):
    plan = _planner(watch=(str(tmp_path),)).plan(str(tmp_path), "generic")
    assert plan.watch_warning is None


def test_phase1_producer_config_unchanged(tmp_path):
    watch = ("/other",)
    p = _planner(watch=watch)
    p.plan(str(tmp_path), "generic")
    assert p._watch == watch                             # never mutates watch_repos (rejected coupling)


def test_phase1_not_a_dir_fails_fast(tmp_path):
    with pytest.raises(ValueError):
        _planner().plan(str(tmp_path / "does-not-exist"), "generic")


def test_phase1_empty_trailer_fails_fast():
    with pytest.raises(ValueError):
        InstallPlanner(_store(), stamp_trailer="")       # §6 row 3: no silent empty trailer
