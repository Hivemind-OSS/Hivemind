"""CT-8 — established = canonical-line outcome verification (plan §4 intent 8,
§3.1).

``provisional → established`` on >= 1 SHA-bound ``outcome_verified_helped`` row —
the ONLY path to the top tier (the human vouch is gone). The post-ingest
promotion sweep is ``LifecycleService.promote_established()`` (plan §6 step 1);
self-reported ``outcome_helped`` never establishes; a quarantined row with a win
goes via provisional first; established never decays.
"""

from __future__ import annotations

import json

from hive.domain.lifecycle import ESTABLISHED, PROVISIONAL, QUARANTINED
from tests.contract.conftest import (
    DAY_S,
    call,
    capture,
    evidence_rows,
    hit_for,
    ident,
    insert_verify_audit,
    payload,
    recall,
    require_method,
    trust_of,
    write_ok,
)

T = "bind liveness to the process start-time, not a raw PID"


def _sweep(rig) -> list[int]:
    promote = require_method(rig.container.lifecycle, "promote_established")
    out = promote()
    assert isinstance(out, list), (
        f"promote_established() -> list[int] (the promoted ids): {out!r}"
    )
    return out


def test_verified_win_establishes(rig):
    eid = write_ok(rig.server, T)
    assert trust_of(rig.store, eid) == PROVISIONAL
    insert_verify_audit(rig.store, eid, "outcome_verified_helped", ts=100)

    promoted = _sweep(rig)
    assert eid in promoted, f"the verified win must establish {eid}: {promoted}"
    assert trust_of(rig.store, eid) == ESTABLISHED
    # the transition writes its audit row naming the rule
    payloads = [r["payload"] for r in evidence_rows(rig.store, eid)]
    assert any("verified_established" in p for p in payloads), (
        f"the establish transition stamps rule=verified_established: {payloads}"
    )
    # and the label rides the very next recall
    hit = hit_for(recall(rig.server, T), eid)
    assert hit["trust"] == "established"


def test_self_reported_help_never_establishes(rig):
    eid = write_ok(rig.server, T)
    env = payload(
        call(rig.server, "hive_outcome", {"helped": [eid]}, identity=ident("agent-b"))
    )
    assert env.get("status") == "recorded"
    promoted = _sweep(rig)
    assert eid not in promoted
    assert trust_of(rig.store, eid) == PROVISIONAL, (
        "a self-reported outcome_helped alone must NEVER reach the top tier "
        "(promoting on settled_wins instead of verified_wins is the named mutation)"
    )


def test_quarantined_with_win_goes_via_provisional_first(rig):
    env = capture(rig.server, "a quarantined capture with a verified win")
    assert env.get("status") == "quarantined"
    eid = env["id"]
    insert_verify_audit(rig.store, eid, "outcome_verified_helped", ts=100)

    promoted = _sweep(rig)
    assert eid not in promoted, (
        "the establish sweep reads the PROVISIONAL set only — quarantined rows "
        "go via the provisional rung first"
    )
    assert trust_of(rig.store, eid) != ESTABLISHED
    assert trust_of(rig.store, eid) in (QUARANTINED, PROVISIONAL)


def test_established_never_decays(rig):
    eid = write_ok(rig.server, T)
    insert_verify_audit(rig.store, eid, "outcome_verified_helped", ts=100)
    assert eid in _sweep(rig)

    rig.clock.advance(400 * DAY_S)  # far past every TTL
    rig.container.lifecycle.sweep()
    assert trust_of(rig.store, eid) == ESTABLISHED, "established never decays"
    served = recall(rig.server, T)
    hit = hit_for(served, eid)
    assert hit["trust"] == "established"


def test_establish_audit_is_json_payload(rig):
    eid = write_ok(rig.server, T)
    insert_verify_audit(rig.store, eid, "outcome_verified_helped", ts=100)
    _sweep(rig)
    for row in evidence_rows(rig.store, eid):
        body = json.loads(row["payload"]) if row["payload"] else {}
        assert isinstance(body, dict)
