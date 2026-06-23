"""AGI-MODE sentinel primitive: the pure ``is_agi_override`` predicate + the reserved
``AGI_OVERRIDE`` actor constant. This is the domain half of AGI_MODE — it reacts to the
sentinel VALUE only, never to the transport flag (Law 4 purity). The flag that HONORS the
sentinel lives at the boundary (mcp_server), not here."""
from __future__ import annotations

from hive.domain.agi import AGI_OVERRIDE, is_agi_override


def test_constant_is_the_reserved_actor_sentinel():
    assert AGI_OVERRIDE == "AGI_OVERRIDE"


def test_predicate_true_only_on_the_exact_sentinel():
    assert is_agi_override(AGI_OVERRIDE) is True


def test_predicate_false_on_a_human_named_vouch():
    # a human approver name is NOT the override — the byte-distinguishable distinction the
    # durable row preserves (an override write can never masquerade as a human vouch).
    for name in ("alice", "user", "orchestrator", "agi_override", "AGI_OVERRIDE "):
        assert is_agi_override(name) is False


def test_predicate_false_on_empty_or_none():
    # the handler defaults a missing approver to "" before the predicate; None is tolerated.
    assert is_agi_override("") is False
    assert is_agi_override(None) is False  # type: ignore[arg-type]
