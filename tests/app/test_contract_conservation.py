"""Information-conservation gate: diffs the LIVE served union (SERVER_INSTRUCTIONS + the
onboarding payload, once it exists, + every tool description) against the frozen v.06
inventory in tests/app/fixtures/contract_corpus_v06.py. Every NECESSARY semantic unit must
still be found somewhere in the live union — a unit may change channel, but may never vanish.
Before render_onboarding_payload exists the live union is byte-identical to the frozen corpus,
so this passes trivially; it must still pass once the served constants are rewritten, which is
the actual proof nothing load-bearing was lost in the move."""
from __future__ import annotations

import json

from hive.app import onboard_ref
from hive.app.tool_defs import TOOL_DEFINITIONS
from tests.app.fixtures.contract_corpus_v06 import FROZEN_SECTIONS, INVENTORY, NECESSARY_UNITS


def _live_union() -> str:
    """The current served union. Falls back to just instructions+descriptions when
    render_onboarding_payload doesn't exist yet — at that point it is, by construction,
    identical to the frozen v.06 corpus."""
    parts = [onboard_ref.SERVER_INSTRUCTIONS]
    payload_fn = getattr(onboard_ref, "render_onboarding_payload", None)
    if payload_fn is not None:
        parts.append(json.dumps(payload_fn(), default=str))
    parts.extend(t["description"] for t in TOOL_DEFINITIONS)
    return "\n".join(parts)


def test_no_necessary_unit_lost_vs_v06():
    union = _live_union()
    lost = []
    for e in NECESSARY_UNITS:
        if not any(p in union for p in e["probes"]):
            lost.append((e["id"], e["unit"], e["probes"]))
    assert lost == [], f"{len(lost)} NECESSARY unit(s) missing from the live union: {lost}"


def test_v06_inventory_is_exhaustive_over_the_frozen_corpus():
    """Guards the fixture itself: every section of the frozen corpus is named by >=1 inventory
    entry's source_sections, so the checklist can't silently omit a section it never examined."""
    covered = set()
    for e in INVENTORY:
        covered.update(e["source_sections"])
    missing = set(FROZEN_SECTIONS) - covered
    assert missing == set(), f"frozen sections never referenced by any inventory entry: {missing}"


def test_inventory_probes_are_individually_verified_against_their_claimed_sections():
    """Guards the inventory's internal soundness against its own frozen baseline: a probe
    claimed for a section must actually be found in THAT section alone, not merely somewhere
    in the union of all claimed sections — a union-only check can't detect a section that
    claims coverage it doesn't have (e.g. two embeddings of the same fact differing in
    wording or case)."""
    bad = []
    for e in INVENTORY:
        if e["classification"] != "NECESSARY":
            continue
        for s in e["source_sections"]:
            if not any(p in FROZEN_SECTIONS[s] for p in e["probes"]):
                bad.append((e["id"], e["unit"], s))
    assert bad == [], f"probe(s) not found in their individually-claimed section: {bad}"


def test_every_droppable_entry_carries_a_reason():
    for e in INVENTORY:
        if e["classification"] == "DROPPABLE":
            assert e.get("reason"), f"#{e['id']} {e['unit']} is DROPPABLE with no reason"
