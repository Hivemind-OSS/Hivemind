"""hive.app.contract — the trimmed v3 served contract: five survivors, nothing else.

Unit mirror of the CT-11/CT-12 surface pins (tests/contract/test_served_contract.py and
test_deletion_total.py drive the served initialize/tools-list end-to-end; this file pins the
module itself so the slice stays green without the full server stack)."""

from __future__ import annotations

import importlib.util

from hive.app import contract
from hive.app.contract import (
    BAD_VS_STALE,
    METADATA_FIELD_LIMIT,
    REMEDIATION_NOTICE,
    SERVER_INSTRUCTIONS,
    WRITE_VS_CAPTURE,
)

SURVIVORS = {
    "METADATA_FIELD_LIMIT",
    "SERVER_INSTRUCTIONS",
    "REMEDIATION_NOTICE",
    "WRITE_VS_CAPTURE",
    "BAD_VS_STALE",
}

# The deleted-apparatus vocabulary: none of it may survive in ANY served string. The list is
# the CT-11 sweep plus the human/approver vocabulary the v3 (agent-adjudicated) contract
# defines out of existence.
DEAD_TOKENS = (
    "AGI",
    "approved_by",
    "approver",
    "HIVEMIND-RULES",
    "contract_version",
    "contract-version",
    "hive-edge",
    "include_onboarding",
    "your human",
)


def test_module_exports_exactly_the_five_survivors():
    """The rename is also a trim: CONTRACT_VERSION, the keystone/bundle machinery, the rules
    block, hooks, allowlist, procedure, edge-CLI reference, MIN_EDGE_VERSION, and
    render_onboarding_payload are all GONE — five public names remain, nothing else."""
    public = {n for n in vars(contract) if not n.startswith("_")}
    assert public == SURVIVORS, f"unexpected exports: {sorted(public ^ SURVIVORS)}"


def test_onboard_ref_module_is_gone():
    # the rename left no compatibility shim behind
    assert importlib.util.find_spec("hive.app.onboard_ref") is None


def test_metadata_field_limit_is_the_measured_truncation_point():
    assert METADATA_FIELD_LIMIT == 2048


def test_instructions_fit_the_cap_and_carry_no_dead_apparatus():
    # BUG-023 guard: the capped initialize field must arrive whole; and the deleted
    # install/AGI/approver apparatus may not survive in the served text (CT-11 mirror).
    assert 0 < len(SERVER_INSTRUCTIONS) <= METADATA_FIELD_LIMIT
    for dead in DEAD_TOKENS:
        assert dead not in SERVER_INSTRUCTIONS, f"dead apparatus served: {dead!r}"


def test_instructions_teach_the_v3_loop():
    """The trimmed floor still teaches the whole loop: recall-first (single-pointed, scoped),
    honest abstain, scoped store, write-vs-capture, outcome evidence, machine-gated
    retirement, advisory flag — and names every served verb."""
    s = SERVER_INSTRUCTIONS
    low = s.lower()
    assert "recall first" in low and "single-pointed" in low
    assert "never bundle" in low
    assert "repos=" in s and "anchor_prefix" in s  # scoped recall grammar
    assert "global" in low  # omitted scope = global search
    assert "never invent" in low  # honest abstain
    assert "anchors=[{repo, anchor}]" in s  # scoped store grammar
    assert "general" in low  # no repo = fleet-wide
    assert (
        "hive_outcome" in s and "evidence only" in low
    )  # outcome is evidence, not control
    assert "machine" in low and "no-op" in low  # machine-gated retirement…
    assert "never an error" in low  # …and unqualified is benign
    assert "advisory" in low  # hive_flag never retires
    for verb in (
        "hive_recall",
        "hive_write",
        "hive_capture",
        "hive_supersede",
        "hive_prune",
        "hive_outcome",
        "hive_flag",
        "hive_health",
    ):
        assert verb in s, f"verb roster missing {verb!r}"


def test_instructions_teach_trust_and_drift_vocabulary():
    # established is REDEFINED on the wire: outcome-verified on the canonical line (the human
    # vouch is gone); hits carry repos/anchors/drift and a drifted hit is reference-only.
    low = SERVER_INSTRUCTIONS.lower()
    assert "established" in low and "outcome-verified" in low
    assert "provisional" in low
    assert "drift" in low
    assert "reference only" in low


def test_instructions_keep_the_lean_store_philosophy():
    low = SERVER_INSTRUCTIONS.lower()
    assert "stigmergic" in low and "no direct communication" in low
    assert "maintainer" in low and "lean" in low
    assert "flow" in low and "bigger store" in low
    assert "store nothing" in low  # the zero-store escape survives


def test_write_vs_capture_and_bad_vs_stale_ride_the_floor_exactly_once():
    # single-sourced composition: the two decision rules are embedded verbatim, once each —
    # a second copy is the duplication the single-source posture forbids.
    assert SERVER_INSTRUCTIONS.count(WRITE_VS_CAPTURE) == 1
    assert SERVER_INSTRUCTIONS.count(BAD_VS_STALE) == 1


def test_write_vs_capture_is_the_v3_rule():
    """v3 write side: serve-as-provisional-then-heal. Write is the DEFAULT (serves now,
    trust=provisional); capture is the unclear-value tail (quarantined, demand-promoted).
    No approver, no human confirm — that machinery is deleted."""
    low = WRITE_VS_CAPTURE.lower()
    assert "hive_write" in WRITE_VS_CAPTURE and "hive_capture" in WRITE_VS_CAPTURE
    assert "provisional" in low
    assert "quarantined" in low and "demand" in low
    for dead in ("approver", "approved_by", "human", "confirm"):
        assert dead not in low, f"v2 approval vocabulary survives: {dead!r}"


def test_bad_vs_stale_keeps_the_retire_diagnosis():
    b = BAD_VS_STALE
    assert "hive_supersede" in b and "replaces=" in b  # STALE -> replace with successor
    assert "hive_prune" in b  # BAD -> bare retirement
    low = b.lower()
    assert "stale" in low and "successor" in low
    assert "incorrect" in low and "misleading" in low
    assert "not a guarantee" in low  # old ts alone retires nothing


def test_remediation_notice_is_agent_adjudicated():
    """The stale-hit rider carries the full single-anchor option set for the v3 flow:
    reference-only, outcome-hurt evidence now, then the MACHINE-GATED retirement verbs per
    BAD_VS_STALE — no human escalation, no hive_flag (a memory-vs-memory tool), and the
    unqualified outcome is named a benign no-op."""
    r = REMEDIATION_NOTICE
    assert "REFERENCE ONLY" in r
    for token in (
        "hive_outcome",
        "hurt=[",
        "hive_supersede",
        "hive_write(replaces=",
        "hive_prune",
        "BAD_VS_STALE",
    ):
        assert token in r, token
    low = r.lower()
    assert "machine" in low and "no-op" in low and "never an error" in low
    assert "hive_flag" not in r
    for dead in DEAD_TOKENS:
        assert dead not in r, f"dead apparatus in the rider: {dead!r}"
