"""The evidence-EVENT kind registry — the single source for the ledger vocabulary.

PURE DATA: this module imports nothing (the purity gate forbids sqlite3|torch|subprocess|
os|git|time anywhere in hive/domain/; pure constants are fine). Every ``evidence_events``
write/read site PROJECTS from here — the lifecycle stamps promotions, the store stamps
supersede/prune/ttl_expired and filters its evidence reads, the MCP outcome verb stamps
helped/hurt, the censusctl change feed stamps change_outcome — so the written and queried
copies cannot drift (mirrors hive.domain.provenance).

DISTINCT from ``hive.domain.kinds``: that is the MEMORY-kind taxonomy (episodes.kind,
DDL-CHECK-enforced, projected into the served fleet contract and contract-guard WATCHED).
This registry names the append-only ``evidence_events.kind`` vocabulary, which has NO DDL
CHECK — these exact strings ARE the stored schema, so existing ledgers keep matching only
while the values stay byte-identical (pinned by tests/domain/test_evidence_kinds.py).
"""
from __future__ import annotations

EK_PROMOTE: str = "promote"                # demand promotion stamped by LifecycleService
EK_SUPERSEDE: str = "supersede"            # retirement-with-replacement (store.supersede)
EK_PRUNE: str = "prune"                    # bare retirement (store.deprecate, behind hive_prune)
EK_TTL_EXPIRED: str = "ttl_expired"        # lazy decay materialized by sweep_decayed
EK_OUTCOME_HELPED: str = "outcome_helped"  # agent-reported settled win (hive_outcome)
EK_OUTCOME_HURT: str = "outcome_hurt"      # agent-reported settled loss (hive_outcome)
EK_CHANGE_OUTCOME: str = "change_outcome"  # SHA-bound census change outcome (censusctl ingest)
EK_OUTCOME_VERIFIED_HELPED: str = "outcome_verified_helped"  # SHA-bound corroboration (census ingest)
EK_OUTCOME_VERIFIED_HURT: str = "outcome_verified_hurt"      # SHA-bound contradiction (census ingest)
EK_VERIFY_CURRENT: str = "verify_current"  # anchor verified intact at head SHA (census ingest)
EK_VERIFY_STALE: str = "verify_stale"      # anchor verified broken/removed at head SHA (census ingest)

# The single membership set: every write site emits a member; the write-site spies and
# the append-only goldens read it.
EVIDENCE_KINDS: frozenset[str] = frozenset({
    EK_PROMOTE, EK_SUPERSEDE, EK_PRUNE, EK_TTL_EXPIRED,
    EK_OUTCOME_HELPED, EK_OUTCOME_HURT, EK_CHANGE_OUTCOME,
    EK_OUTCOME_VERIFIED_HELPED, EK_OUTCOME_VERIFIED_HURT,
    EK_VERIFY_CURRENT, EK_VERIFY_STALE,
})
