"""§4 hook manifest + harness profile/recipe — the abstract, versioned HookManifest
projected onto each IDE at its capability tier. The load-bearing invariant (the §7 mutation
pin): a Tier-≤1 host NEVER receives OS hook files — enforced at construction, so a violation
is UNCONSTRUCTABLE, not merely asserted."""
from __future__ import annotations

import json

import pytest

from hive.app.onboard import (
    HARNESS_PROFILES, HOOK_MANIFEST, ONBOARDING_MANIFEST_VERSION, build_recipe,
    resolve_profile,
)
from hive.domain.models import (
    TIER_HOOKS, TIER_RULES, HarnessProfile, HarnessRecipe, HookFile, HookManifest,
)


# ── carrier invariants ────────────────────────────────────────────────────────
def test_manifest_requires_at_least_one_hook():
    with pytest.raises(ValueError):
        HookManifest(manifest_version=1, hooks=())


def test_profile_max_tier_iff_hook_mechanism():
    with pytest.raises(ValueError):                       # tier-2 claim with no mechanism = a lie
        HarnessProfile("x", ("R",), "t", None, TIER_HOOKS)
    with pytest.raises(ValueError):                       # a mechanism hidden at tier-1 = the inverse lie
        HarnessProfile("x", ("R",), "t", "mech", TIER_RULES)
    HarnessProfile("x", ("R",), "t", "mech", TIER_HOOKS)  # consistent rows construct fine
    HarnessProfile("y", ("R",), "t", None, TIER_RULES)


def test_recipe_tier1_must_emit_zero_hook_files():
    """★ the §7 mutation pin: a Tier-≤1 host with OS hook files cannot be constructed."""
    with pytest.raises(ValueError):
        HarnessRecipe(harness="x", resolved_tier=TIER_RULES, manifest_version=1,
                      rules_addendum="a", playbook="p", hook_files=(HookFile("p", "c", "m"),))


def test_recipe_tier2_must_emit_a_hook_file():
    with pytest.raises(ValueError):
        HarnessRecipe(harness="x", resolved_tier=TIER_HOOKS, manifest_version=1,
                      rules_addendum="a", playbook="p", hook_files=())


# ── the pre-baked manifest + profiles ─────────────────────────────────────────
def test_manifest_version_is_the_single_source():
    # the manifest, the link record, and the hive_health onboarding hint share one version
    assert HOOK_MANIFEST.manifest_version == ONBOARDING_MANIFEST_VERSION
    assert len(HOOK_MANIFEST.hooks) >= 1


def test_profiles_present_only_claude_code_is_tier2():
    assert set(HARNESS_PROFILES) == {"claude-code", "cursor", "windsurf", "cline",
                                     "opencode", "codex", "generic"}
    tier2 = {h for h, p in HARNESS_PROFILES.items() if p.max_tier >= TIER_HOOKS}
    assert tier2 == {"claude-code"}                       # the only host with a hook mechanism today


def test_resolve_profile_unknown_falls_back_to_generic():
    assert resolve_profile("emacs").harness == "generic"  # long-tail host self-resolves
    assert resolve_profile("claude-code").harness == "claude-code"


# ── build_recipe: the projection ──────────────────────────────────────────────
def test_build_recipe_claude_code_is_tier2_with_hook_file():
    r = build_recipe("claude-code")
    assert r.resolved_tier == TIER_HOOKS and len(r.hook_files) >= 1
    hf = r.hook_files[0]
    assert hf.path == ".claude/settings.json" and hf.mechanism == "claude-settings-json"
    assert r.manifest_version == HOOK_MANIFEST.manifest_version
    assert r.rules_addendum and r.playbook


def test_build_recipe_tier1_hosts_emit_no_hook_files():
    for harness in ("cursor", "windsurf", "cline", "opencode", "codex", "generic"):
        r = build_recipe(harness)
        assert r.resolved_tier == TIER_RULES
        assert r.hook_files == ()                         # no OS hooks below Tier 2
        assert r.playbook and r.rules_addendum            # but still self-drivable


def test_build_recipe_hook_file_is_valid_json_no_secret():
    r = build_recipe("claude-code")
    parsed = json.loads(r.hook_files[0].content)          # a Tier-2 hook file is real JSON
    assert "hooks" in parsed
    for needle in ("AKIA", "sk-", "-----BEGIN"):
        assert needle not in r.hook_files[0].content
