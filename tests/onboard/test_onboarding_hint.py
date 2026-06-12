"""self-onboard hint — the pure ``onboarding_hint`` block any tool hands back when
touched from a repo with NO link record. Reference-not-instructions, fixed shape, a
version int + a static ``next`` naming the ONE call that links the repo; no repo content,
no secret. The MCP-surface integration (hive_health) is pinned in tests/mcp."""
from __future__ import annotations

import json

from hive.app.onboard import ONBOARDING_MANIFEST_VERSION, onboarding_hint


def test_onboarding_hint_shape():
    hint = onboarding_hint()
    assert hint["required"] is True
    assert isinstance(hint["manifest_version"], int)
    assert hint["manifest_version"] == ONBOARDING_MANIFEST_VERSION
    assert isinstance(hint["next"], str) and "hive_init" in hint["next"]
    # exactly the three contract keys — no leakage of internal state
    assert set(hint) == {"required", "manifest_version", "next"}


def test_onboarding_hint_version_override():
    """Chunk-5's HookManifest will pass an authoritative version; the override flows
    straight through (and is coerced to int), default untouched."""
    assert onboarding_hint(7)["manifest_version"] == 7
    assert onboarding_hint("3")["manifest_version"] == 3        # coerced
    assert onboarding_hint()["manifest_version"] == ONBOARDING_MANIFEST_VERSION


def test_onboarding_hint_is_pure_reference_no_secret():
    """It takes no repo input and embeds none — a static, JSON-clean reference block
    that can never carry a path, credential, or instruction-to-obey."""
    blob = json.dumps(onboarding_hint())
    for needle in ("AKIA", "sk-", "-----BEGIN", "/home/", "password", "token"):
        assert needle not in blob
