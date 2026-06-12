"""Chunk 6 — the provenance banner + the finalize playbook.

The load-bearing property (★): the banner is stamped OUTSIDE the hashed ``<!-- hive-init
-->`` block, so appending it to the rules file leaves the phase-2 confirm hash untouched —
a re-touch guard that can never break the handshake it guards.
"""
from __future__ import annotations

import hashlib

from hive.app.onboard import (
    SETUP_BANNER_PREFIX, build_recipe, provenance_banner, render_rules_block,
)
from hive.domain.models import RULES_BLOCK_END, RULES_BLOCK_START


# ── the banner generator ──────────────────────────────────────────────────────
def test_banner_carries_harness_tier_manifest_and_do_not_rerun():
    b = provenance_banner("claude-code", 2, manifest_version=1)
    assert b.startswith(SETUP_BANNER_PREFIX) and b.endswith("-->")
    assert "harness=claude-code" in b and "tier=2" in b and "manifest=1" in b
    assert "DO NOT re-run" in b


def test_banner_has_no_block_markers_and_no_secret():
    # it lives OUTSIDE the hashed block, so it must carry neither delimiter…
    b = provenance_banner("cursor", 1)
    assert RULES_BLOCK_START not in b and RULES_BLOCK_END not in b
    # …nor any secret/trailer bytes (it is a label-only marker)
    for needle in ("AKIA", "sk-", "-----BEGIN"):
        assert needle not in b


def test_banner_default_manifest_version_is_the_single_source():
    from hive.app.onboard import ONBOARDING_MANIFEST_VERSION
    assert f"manifest={ONBOARDING_MANIFEST_VERSION}" in provenance_banner("generic", 1)


# ── ★ the banner cannot perturb the phase-2 hash ──────────────────────────────
def test_banner_appended_outside_block_preserves_confirm_hash():
    """A rules file = rendered block + the banner. Locating the block by its markers
    extracts EXACTLY the rendered block, whose sha256 still equals block_hash — so a
    server recomputing the canonical hash (or extracting+hashing) confirms unchanged.
    Mutating the banner to embed a block marker (the deliberate fault) breaks this."""
    block = render_rules_block("Hive-Trace", 1)
    banner = provenance_banner("claude-code", 2)
    file_contents = f"{block.rendered_text}\n\n{banner}\n"

    start = file_contents.index(RULES_BLOCK_START)
    end = file_contents.index(RULES_BLOCK_END) + len(RULES_BLOCK_END)
    extracted = file_contents[start:end]

    assert extracted == block.rendered_text                  # banner sat strictly outside
    assert hashlib.sha256(extracted.encode("utf-8")).digest() == block.block_hash
    assert banner not in extracted                           # never inside the hashed span


def test_banner_precedes_no_marker_so_extraction_is_unambiguous():
    # the END marker the extractor anchors on is the block's, never the banner's
    banner = provenance_banner("windsurf", 1)
    assert RULES_BLOCK_END not in banner


# ── the finalize sequence rides the recipe playbook (in-band, no skill needed) ──
def test_playbook_encodes_verify_then_banner_then_short_circuit():
    pb = build_recipe("claude-code").playbook
    assert "Verify gate" in pb
    assert "V1" in pb and "V2" in pb and "V3" in pb
    assert "provenance banner" in pb.lower() or "banner" in pb.lower()
    assert "OUTSIDE" in pb                                   # banner stamped outside the block
    assert "already set up" in pb.lower()                    # the re-touch short-circuit


def test_playbook_is_tier_aware():
    # Tier-2 names the hook-file merge + the V4 live-hook check; Tier-1 marks V4 N/A
    cc = build_recipe("claude-code").playbook
    generic = build_recipe("generic").playbook
    assert "settings.json" in cc and "hooks_seen" in cc
    assert "V4 N/A" in generic and "settings.json" not in generic
