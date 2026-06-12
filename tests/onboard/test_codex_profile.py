"""CV1 — the ``codex`` harness profile: tier 1, AGENTS.md default, MCP
registration surfaced as reference text (operator-owned ~/.codex/config.toml),
and the seat-identity line every recipe playbook now carries."""
from __future__ import annotations

from hive.adapters.sqlite_db import connect
from hive.adapters.store_sqlite import SqliteEpisodeStore
from hive.app.onboard import InstallPlanner, build_recipe, resolve_profile
from hive.app.tool_defs import TOOL_DEFINITIONS
from hive.domain.models import TIER_RULES


def test_codex_profile_round_trips(tmp_path):
    store = SqliteEpisodeStore(connect(":memory:"))
    planner = InstallPlanner(store, stamp_trailer="Hive-Trace")
    plan = planner.plan(str(tmp_path), "codex")               # phase 1
    assert plan.harness == "codex"
    assert plan.rules_file == "AGENTS.md"                     # codex create-default
    assert plan.recipe.resolved_tier == TIER_RULES            # tier 1
    assert plan.recipe.hook_files == ()                       # no OS hooks below tier 2
    res = planner.confirm(str(tmp_path), plan.expected_confirm_hash, "codex")
    assert res["linked"] is True                              # phase 2
    assert res["link"]["harness"] == "codex" and res["link"]["tier"] == TIER_RULES


def test_codex_profile_row_and_enum():
    p = resolve_profile("codex")
    assert p.harness == "codex" and p.max_tier == TIER_RULES
    assert p.rules_file_candidates == ("AGENTS.md",)
    assert "~/.codex/config.toml" in p.mcp_config_target
    init = next(t for t in TOOL_DEFINITIONS if t["name"] == "hive_init")
    assert "codex" in init["inputSchema"]["properties"]["harness"]["enum"]


def test_playbook_carries_registration_and_seat_identity():
    # per-seat identity is emitted in EVERY tier-1 recipe, not tribal
    # knowledge — the codex playbook names its config target inline.
    pb = build_recipe("codex").playbook
    assert "~/.codex/config.toml" in pb
    assert "--agent <repo-name>" in pb
    assert "hive token <seat>" in pb
    for harness in ("cursor", "opencode", "generic", "claude-code"):
        other = build_recipe(harness).playbook
        assert "--agent <repo-name>" in other and "hive token <seat>" in other
