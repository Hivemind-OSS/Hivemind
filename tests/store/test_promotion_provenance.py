"""SqliteEpisodeStore.promotion_provenance: reads the newest promote audit's stamped
demand_independence per episode, defensively skips malformed/pre-stamp payloads, omits
ids with no promote audit, and conforms to the PromotionProvenanceReader port."""
from __future__ import annotations

import json

import numpy as np

from hive.adapters.index_exhaustive import ExhaustiveCosineIndex
from hive.adapters.sqlite_db import connect
from hive.adapters.store_sqlite import SqliteEpisodeStore
from hive.domain.ports import PromotionProvenanceReader

DIM = 4


def _store() -> SqliteEpisodeStore:
    return SqliteEpisodeStore(connect(":memory:"), index=ExhaustiveCosineIndex(DIM))


def _seed_episode(s: SqliteEpisodeStore, text: str, *, ts: int = 10) -> int:
    eid, _ = s.stage(text=text, weight=1.0, proposed_by="writer", ts=ts)
    ok = s.complete(eid, np.eye(DIM, dtype=np.float32)[0], expected_version=0,
                    trust="provisional", last_active_ts=ts)
    assert ok
    return eid


def _di_payload(rho_bar, n_eff, k):
    return json.dumps({"rule": "demand", "n_misses": k,
                       "demand_independence": {"rho_bar": rho_bar, "n_eff": n_eff, "k": k}})


# ── port conformance: the REAL adapter, not just a fake ─────────────────────────
def test_real_store_satisfies_promotion_provenance_reader():
    assert isinstance(SqliteEpisodeStore(connect(":memory:")), PromotionProvenanceReader)


# ── the read ────────────────────────────────────────────────────────────────────
def test_reads_stamped_demand_independence():
    s = _store()
    eid = _seed_episode(s, "a thin provisional")
    s.insert_audit(eid, "promote", "server", 11, _di_payload(0.9, 1.0, 4))
    prov = s.promotion_provenance([eid])
    assert prov == {eid: (0.9, 1.0, 4)}


def test_newest_promote_audit_wins():
    s = _store()
    eid = _seed_episode(s, "re-promoted provisional")
    s.insert_audit(eid, "promote", "server", 10, _di_payload(0.5, 3.0, 4))   # older
    s.insert_audit(eid, "promote", "server", 20, _di_payload(0.95, 1.0, 4))  # newer
    prov = s.promotion_provenance([eid])
    assert prov == {eid: (0.95, 1.0, 4)}                     # newest stamp by ts


def test_id_with_no_promote_audit_is_omitted():
    s = _store()
    eid = _seed_episode(s, "never promoted-with-stamp")
    assert s.promotion_provenance([eid]) == {}


def test_pre_stamp_promote_row_is_omitted():
    # a pre-3b promotion audit (no demand_independence key) under-claims (skipped, never raises).
    s = _store()
    eid = _seed_episode(s, "legacy promotion")
    s.insert_audit(eid, "promote", "server", 11, json.dumps({"rule": "demand", "n_misses": 4}))
    assert s.promotion_provenance([eid]) == {}


def test_malformed_payload_is_skipped_not_raised():
    s = _store()
    eid = _seed_episode(s, "broken audit")
    s.insert_audit(eid, "promote", "server", 11, "not json at all{{{")
    assert s.promotion_provenance([eid]) == {}             # defensive: no raise


def test_other_kinds_are_ignored():
    # a non-'promote' audit carrying a demand_independence-shaped payload must NOT be read.
    s = _store()
    eid = _seed_episode(s, "wrong-kind audit")
    s.insert_audit(eid, "expose", "server", 11, _di_payload(0.9, 1.0, 4))
    assert s.promotion_provenance([eid]) == {}


def test_empty_ids_returns_empty():
    s = _store()
    assert s.promotion_provenance([]) == {}


def test_mixed_ids_returns_only_stamped():
    s = _store()
    a = _seed_episode(s, "stamped one")
    b = _seed_episode(s, "unstamped one")
    s.insert_audit(a, "promote", "server", 11, _di_payload(0.8, 2.0, 5))
    prov = s.promotion_provenance([a, b])
    assert prov == {a: (0.8, 2.0, 5)}                       # b omitted (no stamp)
