"""The AGI-MODE sentinel primitive — the PURE domain half of the operator opt-in.

AGI_MODE lets an operator delegate the per-write human vouch to an agent under an explicit,
audited, default-OFF switch. The switch itself is a TRANSPORT concern (it lives at the
mcp_server boundary, never in the pure domain — Law 4). What lives HERE is only the reserved
actor VALUE and the predicate that recognizes it: the domain reacts to the sentinel VALUE,
never to the flag, so ``tests/test_purity.py`` stays green (no config/flag import in domain).

The sentinel ``approved_by == "AGI_OVERRIDE"`` is the byte-DISTINGUISHABLE marker an override
write/retire stamps in the durable row. It is HONORED only when AGI_MODE is ON (the boundary
gate) and REJECTED fail-closed when OFF — so an override is structurally UNCONSTRUCTABLE in the
OFF build, keeping honesty structural (Law 1), never behavioral. An override never ASSERTS
truth (it substitutes the ACTOR, not the trust claim — Law 3); paired with it, the admission
door under-claims the ORIGIN as ``agent_reasoned`` (Law 2 / INV-2), never ``human``.

PURE DATA + one total predicate: imports nothing (the purity gate forbids
sqlite3|torch|subprocess|os|git|time anywhere in hive/domain/)."""
from __future__ import annotations

# The reserved actor sentinel. Unconstructable as a real human/orchestrator name by
# convention; the boundary gate honors it only under AGI_MODE.
AGI_OVERRIDE = "AGI_OVERRIDE"


def is_agi_override(approved_by: str) -> bool:
    """True iff ``approved_by`` is EXACTLY the reserved override sentinel. Total — a missing
    (None) or any other approver is a plain ``False`` (an agent-supplied human-looking name is
    NOT an override; the gate governs only this one exact value)."""
    return approved_by == AGI_OVERRIDE
