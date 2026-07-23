"""SqliteEpisodeStore.last_verification — the newest verify_current/verify_stale ledger
row per requested id, as ``{eid: (ts, head_sha, state)}``. Read-only; powers the served
verification-recency stamp on recall hits. Conforms to the LastVerificationReader port;
defensive payload parse mirrors ``promotion_provenance`` (a malformed newest row is
skipped so an older parseable row may still answer; nothing parseable ⇒ the id is ABSENT)."""

from __future__ import annotations

import json

import numpy as np

from hive.adapters.sqlite_db import connect
from hive.adapters.store_sqlite import SqliteEpisodeStore
from hive.domain.evidence_kinds import EK_VERIFY_CURRENT, EK_VERIFY_STALE
from hive.domain.ports import LastVerificationReader

DIM = 4


def _store() -> SqliteEpisodeStore:
    return SqliteEpisodeStore(connect(":memory:"))


def _seed(s: SqliteEpisodeStore, text: str) -> int:
    eid, _ = s.stage(
        text=text, weight=1.0, proposed_by="w", ts=10, anchors=[("r", "a.py::F")]
    )
    s.complete(
        eid,
        np.eye(DIM, dtype=np.float32)[0],
        expected_version=0,
        trust="established",
        last_active_ts=10,
    )
    return eid


def _verify_payload(head_sha: str = "b" * 40) -> str:
    return json.dumps(
        {
            "schema": "verify/v1",
            "matched": {"path": "a.py", "symbol": "F"},
            "exists_after": True,
            "drift": "unchanged",
            "stamp": {
                "base_sha": "a" * 40,
                "head_sha": head_sha,
                "combdrift": {"combdrift_version": "0.1.0"},
                "matrix_head": {
                    "graph_sha256": "g" * 64,
                    "commit_sha": "b" * 40,
                    "engine_version": "0.1.0",
                },
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def test_real_store_satisfies_last_verification_reader():
    assert isinstance(_store(), LastVerificationReader)


def test_newest_verify_row_wins():
    s = _store()
    eid = _seed(s, "verified memory")
    s.insert_audit(eid, EK_VERIFY_CURRENT, "census", 100, _verify_payload("1" * 40))
    s.insert_audit(eid, EK_VERIFY_STALE, "census", 200, _verify_payload("2" * 40))
    assert s.last_verification([eid]) == {eid: (200, "2" * 40, "stale")}


def test_same_ts_newest_row_id_wins():
    s = _store()
    eid = _seed(s, "same-ts memory")
    s.insert_audit(eid, EK_VERIFY_STALE, "census", 100, _verify_payload("1" * 40))
    s.insert_audit(eid, EK_VERIFY_CURRENT, "census", 100, _verify_payload("2" * 40))
    assert s.last_verification([eid]) == {eid: (100, "2" * 40, "current")}


def test_state_maps_from_the_kind():
    s = _store()
    a, b = _seed(s, "current one"), _seed(s, "stale one")
    s.insert_audit(a, EK_VERIFY_CURRENT, "census", 100, _verify_payload())
    s.insert_audit(b, EK_VERIFY_STALE, "census", 100, _verify_payload())
    got = s.last_verification([a, b])
    assert got[a][2] == "current" and got[b][2] == "stale"


def test_unverified_id_is_absent():
    s = _store()
    verified, plain = _seed(s, "verified"), _seed(s, "never verified")
    s.insert_audit(verified, EK_VERIFY_CURRENT, "census", 100, _verify_payload())
    got = s.last_verification([verified, plain])
    assert verified in got and plain not in got


def test_other_evidence_kinds_never_count():
    s = _store()
    eid = _seed(s, "only outcomes")
    s.insert_audit(eid, "change_outcome", "census", 100, _verify_payload())
    s.insert_audit(eid, "outcome_helped", "agent", 100, "{}")
    assert s.last_verification([eid]) == {}


def test_malformed_newest_payload_falls_back_to_the_older_parseable_row():
    s = _store()
    eid = _seed(s, "half-broken ledger")
    s.insert_audit(eid, EK_VERIFY_CURRENT, "census", 100, _verify_payload("1" * 40))
    s.insert_audit(eid, EK_VERIFY_STALE, "census", 200, "not-json")
    s.insert_audit(eid, EK_VERIFY_STALE, "census", 300, '{"stamp": {"head_sha": 7}}')
    assert s.last_verification([eid]) == {eid: (100, "1" * 40, "current")}


def test_nothing_parseable_means_absent_never_a_raise():
    s = _store()
    eid = _seed(s, "all-broken ledger")
    s.insert_audit(eid, EK_VERIFY_STALE, "census", 100, "not-json")
    assert s.last_verification([eid]) == {}


def test_empty_ids_returns_empty_dict():
    assert _store().last_verification([]) == {}


# ── canonical-ref scoping (the read-time rider filter; None/"" = today's read) ────


def _reffed_payload(head_sha: str, ref: str | None) -> str:
    body = json.loads(_verify_payload(head_sha))
    if ref is not None:
        body["ref"] = ref
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


def test_canonical_ref_none_is_todays_read():
    s = _store()
    eid = _seed(s, "unscoped read")
    s.insert_audit(
        eid, EK_VERIFY_STALE, "census", 200, _reffed_payload("2" * 40, "feat-y")
    )
    assert (
        s.last_verification([eid], canonical_ref=None)
        == s.last_verification([eid])
        == {eid: (200, "2" * 40, "stale")}
    )


def test_canonical_ref_skips_newest_foreign_row_older_canonical_answers():
    # The newest row measured a foreign line: skipped; the older canonical row wins.
    s = _store()
    eid = _seed(s, "foreign-newest ledger")
    s.insert_audit(
        eid, EK_VERIFY_CURRENT, "census", 100, _reffed_payload("1" * 40, "master")
    )
    s.insert_audit(
        eid, EK_VERIFY_STALE, "census", 200, _reffed_payload("2" * 40, "feat-y")
    )
    assert s.last_verification([eid], canonical_ref="master") == {
        eid: (100, "1" * 40, "current")
    }


def test_canonical_ref_keeps_canonical_row():
    s = _store()
    eid = _seed(s, "canonical ledger")
    s.insert_audit(
        eid, EK_VERIFY_STALE, "census", 200, _reffed_payload("2" * 40, "master")
    )
    assert s.last_verification([eid], canonical_ref="master") == {
        eid: (200, "2" * 40, "stale")
    }


def test_canonical_ref_keeps_legacy_refless_row():
    # marker target: a legacy row carries NO ref — the ABSENCE RULE keeps it counting
    # under a set canonical_ref (skipping it would silently blind the rider on every
    # pre-stamp ledger).
    s = _store()
    eid = _seed(s, "legacy ledger")
    s.insert_audit(eid, EK_VERIFY_STALE, "census", 200, _reffed_payload("2" * 40, None))
    assert s.last_verification([eid], canonical_ref="master") == {
        eid: (200, "2" * 40, "stale")
    }


def test_canonical_ref_all_foreign_rows_means_absent():
    s = _store()
    eid = _seed(s, "all-foreign ledger")
    s.insert_audit(
        eid, EK_VERIFY_STALE, "census", 100, _reffed_payload("1" * 40, "feat-a")
    )
    s.insert_audit(
        eid, EK_VERIFY_STALE, "census", 200, _reffed_payload("2" * 40, "feat-b")
    )
    assert s.last_verification([eid], canonical_ref="master") == {}
