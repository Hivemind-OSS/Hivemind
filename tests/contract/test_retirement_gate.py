"""CT-7 — retirement requires a qualifying MACHINE signal; unqualified = benign
no-op (plan §4 intent 7, §3.2 — the D4 gate, single owner
``hive/domain/retirement.py``).

``hive_prune`` / ``hive_supersede`` / ``hive_write(replaces=)`` retire ONLY when
the server itself finds a qualifying signal for the target: drift at the
canonical tip, a ``verify_stale`` ledger row not outdated by a newer
``verify_current``, a server-written ``outcome_verified_hurt``, an
``outcome_hurt`` from an identity OTHER than the retiring caller (two-call
self-destruction blocked), or a mechanical contradiction / near-dup-winner.
Advisory ``hive_flag`` rows never qualify. Unqualified ⇒ the §3.2 noop envelope
(never ``isError``); a gate-feed reader fault fails CLOSED (noop, never a
retire). No approver argument exists anywhere.
"""

from __future__ import annotations

import json

from hive.domain.lifecycle import DEPRECATED, PROVISIONAL
from tests.contract.conftest import (
    DRIFT_ANCHOR_MISSING,
    NOOP_REASON,
    call,
    BASE_TIP,
    drift_put,
    evidence_rows,
    ident,
    insert_verify_audit,
    is_error,
    make_rig,
    payload,
    recall,
    register_repo,
    served_ids,
    set_canonical_tip,
    trust_of,
    write,
    write_ok,
)
from tests.fakes._fakes import FakeClusterProvider

TIP = "b" * 40
A = ident("agent-a")
B = ident("agent-b")


BASE = BASE_TIP  # the baseline the seeded verdicts were judged from


def _rig(tmp_path, *, cluster: bool = False):
    rig = make_rig(tmp_path, embedder=FakeClusterProvider(d=32) if cluster else None)
    register_repo(
        rig.store, "alpha", "https://example.invalid/alpha.git", canonical_ref="main"
    )
    set_canonical_tip(rig.store, "alpha", TIP)
    return rig


def _prune(rig, eid, identity=A) -> dict:
    return payload(
        call(rig.server, "hive_prune", {"episode_id": eid}, identity=identity)
    )


def _supersede(rig, loser, winner, identity=A) -> dict:
    return payload(
        call(
            rig.server,
            "hive_supersede",
            {"loser": loser, "winner": winner},
            identity=identity,
        )
    )


def _assert_gate_noop(env: dict, rig, eid: int) -> None:
    assert env.get("status") == "noop", f"unqualified retirement is a noop: {env}"
    assert NOOP_REASON in str(env.get("reason", "")), (
        f"the noop reason states the missing machine signal literally: {env}"
    )
    assert env.get("signals") == [], f"no signal qualified — signals is []: {env}"
    assert "approved_by" not in env, "no approver field exists in v3"
    assert trust_of(rig.store, eid) != DEPRECATED, "nothing may be retired"


def _assert_retired(rig, eid: int, *, expect_signal_substr: str) -> None:
    assert trust_of(rig.store, eid) == DEPRECATED
    audits = evidence_rows(rig.store, eid)
    stamped = []
    for row in audits:
        try:
            body = json.loads(row["payload"])
        except ValueError:
            continue
        if isinstance(body, dict) and body.get("signals"):
            stamped.append(body["signals"])
    assert stamped, (
        f"a qualified retirement stamps WHICH signal(s) qualified in its audit: "
        f"{audits}"
    )
    joined = " ".join(str(s) for s in stamped[-1])
    assert expect_signal_substr in joined, (
        f"the audit names the qualifying signal ({expect_signal_substr!r}): {stamped}"
    )


# ── noop-on-healthy: all three verbs, envelopes asserted literally ────────────


def test_prune_on_healthy_target_is_benign_noop(tmp_path):
    rig = _rig(tmp_path)
    eid = write_ok(rig.server, "a healthy, evidence-less memory")
    resp = call(rig.server, "hive_prune", {"episode_id": eid}, identity=A)
    env = payload(resp)
    assert not is_error(resp), f"the noop is never isError: {env}"
    _assert_gate_noop(env, rig, eid)
    assert eid in served_ids(recall(rig.server, "a healthy, evidence-less memory"))


def test_supersede_on_healthy_target_is_benign_noop(tmp_path):
    rig = _rig(tmp_path)
    loser = write_ok(rig.server, "healthy fact one about the scheduler")
    winner = write_ok(rig.server, "an unrelated fact about the linter")
    resp = call(
        rig.server, "hive_supersede", {"loser": loser, "winner": winner}, identity=A
    )
    env = payload(resp)
    assert not is_error(resp)
    _assert_gate_noop(env, rig, loser)


def test_write_replaces_unrelated_winner_noops(tmp_path):
    # the rider grants NO new authority: an unrelated correction (near-orthogonal
    # vectors) against a healthy target produces no signal on either side.
    rig = _rig(tmp_path)
    target = write_ok(rig.server, "the pool size is eight")
    env = write(rig.server, "an unrelated note about log rotation", replaces=target)
    assert env.get("status") == "approved", "the WRITE itself always lands"
    assert env.get("superseded") is None
    assert env.get("supersede_noop") == NOOP_REASON
    assert trust_of(rig.store, target) == PROVISIONAL


def test_replaces_drift_qualified_target_still_retires(tmp_path):
    # the target-side clause is unchanged by the reordering: drift on the memory's
    # own line qualifies whether or not the winner adds a signal.
    rig = _rig(tmp_path)
    target = write_ok(
        rig.server,
        "the greet helper strips whitespace",
        anchors=[{"repo": "alpha", "anchor": "app.py::greet"}],
    )
    drift_put(
        rig.store,
        [("alpha", TIP, BASE, "app.py::greet", DRIFT_ANCHOR_MISSING, "{}", 5)],
    )
    env = write(rig.server, "greet was removed; use salute instead", replaces=target)
    assert env.get("status") == "approved"
    assert env.get("superseded") == target, (
        f"a drift-qualified replaces target IS retired by the rider: {env}"
    )
    assert trust_of(rig.store, target) == DEPRECATED
    _assert_retired(rig, target, expect_signal_substr="drift")


def test_replaces_contradiction_correction_stamps_both_signals(tmp_path):
    # a correction that both answers the same need AND opposes its target's polarity
    # is the strongest correction shape there is — both between-row signals fire.
    rig = _rig(tmp_path, cluster=True)
    target = write_ok(
        rig.server, "cid=33 do retry idempotent posts automatically", polarity="do"
    )
    env = write(
        rig.server,
        "cid=33 dont retry posts automatically ever",
        replaces=target,
        polarity="dont",
    )
    assert env.get("superseded") == target, (
        f"a contradicting near-dup correction retires in one call: {env}"
    )
    _assert_retired(rig, target, expect_signal_substr="contradiction")
    audits = [
        json.loads(r["payload"])
        for r in evidence_rows(rig.store, target)
        if r["payload"].startswith("{")
    ]
    signals = [s for body in audits for s in (body.get("signals") or [])]
    assert "winner_near_dup" in signals and "contradiction" in signals, (
        f"both between-row signals are stamped: {signals}"
    )


def test_one_call_and_two_call_correction_reach_the_same_verdict(tmp_path):
    """I2 — the divergence that caused BUG-074 is unconstructable: the one-call rider
    IS the two-call path, so two identical fixtures reach the same trust and the same
    stamped signal set whichever verb the agent used."""

    def _fresh(name: str, *, cluster: bool = False):
        root = tmp_path / name
        root.mkdir()
        return _rig(root, cluster=cluster)

    def _signals(rig, eid) -> set:
        out: set = set()
        for row in evidence_rows(rig.store, eid):
            if not row["payload"].startswith("{"):
                continue
            body = json.loads(row["payload"])
            out.update(body.get("signals") or [])
        return out

    # near-dup pair — both must retire, on the same signal set
    one = _fresh("one", cluster=True)
    loser_a = write_ok(one.server, "cid=34 the cache is warmed at boot")
    env = write(one.server, "cid=34 the cache is warmed lazily now", replaces=loser_a)
    assert env.get("superseded") == loser_a

    two = _fresh("two", cluster=True)
    loser_b = write_ok(two.server, "cid=34 the cache is warmed at boot")
    winner_b = write_ok(two.server, "cid=34 the cache is warmed lazily now")
    assert _supersede(two, loser_b, winner_b).get("status") == "superseded"

    assert trust_of(one.store, loser_a) == trust_of(two.store, loser_b) == DEPRECATED
    assert _signals(one, loser_a) == _signals(two, loser_b), (
        "the one-call and two-call corrections cannot reach different verdicts"
    )

    # unrelated pair — both must noop
    three = _fresh("three")
    loser_c = write_ok(three.server, "an isolated fact about the scheduler")
    env = write(three.server, "an isolated fact about the linter", replaces=loser_c)
    assert env.get("superseded") is None

    four = _fresh("four")
    loser_d = write_ok(four.server, "an isolated fact about the scheduler")
    winner_d = write_ok(four.server, "an isolated fact about the linter")
    assert _supersede(four, loser_d, winner_d).get("status") == "noop"
    assert (
        trust_of(three.store, loser_c) == trust_of(four.store, loser_d) == PROVISIONAL
    )


# ── the qualifying signals ────────────────────────────────────────────────────


def test_drift_stale_qualifies(tmp_path):
    rig = _rig(tmp_path)
    eid = write_ok(
        rig.server,
        "the greet helper trims its input",
        anchors=[{"repo": "alpha", "anchor": "app.py::greet"}],
    )
    drift_put(
        rig.store,
        [("alpha", TIP, BASE, "app.py::greet", DRIFT_ANCHOR_MISSING, "{}", 5)],
    )
    env = _prune(rig, eid)
    assert env.get("status") == "pruned", f"drift at the canonical tip qualifies: {env}"
    _assert_retired(rig, eid, expect_signal_substr="drift")


def test_verify_stale_ledger_row_qualifies(tmp_path):
    rig = _rig(tmp_path)
    eid = write_ok(rig.server, "the old export path still works")
    insert_verify_audit(rig.store, eid, "verify_stale", ts=100)
    env = _prune(rig, eid)
    assert env.get("status") == "pruned", f"a verify_stale ledger row qualifies: {env}"
    _assert_retired(rig, eid, expect_signal_substr="stale")


def test_newer_verify_current_disqualifies_the_stale_row(tmp_path):
    rig = _rig(tmp_path)
    eid = write_ok(rig.server, "the export path was re-verified current")
    insert_verify_audit(rig.store, eid, "verify_stale", ts=100)
    insert_verify_audit(rig.store, eid, "verify_current", ts=200)
    env = _prune(rig, eid)
    _assert_gate_noop(env, rig, eid)


def test_outcome_verified_hurt_qualifies(tmp_path):
    rig = _rig(tmp_path)
    eid = write_ok(rig.server, "always inline the credentials")  # a bad memory
    insert_verify_audit(rig.store, eid, "outcome_verified_hurt", ts=100)
    env = _prune(rig, eid)
    assert env.get("status") == "pruned", (
        f"a server-written outcome_verified_hurt row qualifies: {env}"
    )
    _assert_retired(rig, eid, expect_signal_substr="hurt")


def test_outcome_hurt_from_other_identity_qualifies(tmp_path):
    rig = _rig(tmp_path)
    eid = write_ok(rig.server, "a memory agent B was hurt by")
    hurt = payload(call(rig.server, "hive_outcome", {"hurt": [eid]}, identity=B))
    assert hurt.get("status") == "recorded"
    env = _prune(rig, eid, identity=A)  # retiring caller ≠ the reporter
    assert env.get("status") == "pruned", (
        f"an outcome_hurt from ANOTHER identity qualifies: {env}"
    )
    _assert_retired(rig, eid, expect_signal_substr="hurt")


def test_outcome_hurt_same_identity_blocks_self_destruction(tmp_path):
    rig = _rig(tmp_path)
    eid = write_ok(rig.server, "a memory agent A dislikes")
    payload(call(rig.server, "hive_outcome", {"hurt": [eid]}, identity=A))
    env = _prune(rig, eid, identity=A)  # the SAME identity that reported hurt
    _assert_gate_noop(env, rig, eid)  # hive_outcome + hive_prune two-call blocked


def test_mechanical_contradiction_qualifies(tmp_path):
    rig = _rig(tmp_path, cluster=True)
    do = write_ok(
        rig.server, "cid=11 do retry idempotent posts automatically", polarity="do"
    )
    dont = write_ok(
        rig.server, "cid=11 dont retry posts automatically ever", polarity="dont"
    )
    env = _prune(rig, dont, identity=A)
    assert env.get("status") == "pruned", (
        f"a mechanical near-dup CONTRADICTION with a co-servable row qualifies: {env}"
    )
    _assert_retired(rig, dont, expect_signal_substr="contradiction")
    assert trust_of(rig.store, do) != DEPRECATED, "only the named target retires"


def test_supersede_near_dup_winner_qualifies(tmp_path):
    rig = _rig(tmp_path, cluster=True)
    loser = write_ok(rig.server, "cid=12 the linter runs on save")
    winner = write_ok(rig.server, "cid=12 the linter now runs pre-commit instead")
    env = _supersede(rig, loser, winner)
    assert env.get("status") == "superseded", (
        f"cos(loser, winner) >= conflict.tau — the successor answers the same "
        f"need, the supersede qualifies: {env}"
    )
    assert env.get("loser") == loser and env.get("winner") == winner
    assert "approved_by" not in env
    assert trust_of(rig.store, loser) == DEPRECATED
    assert trust_of(rig.store, winner) == PROVISIONAL


def test_hive_flag_never_qualifies(tmp_path):
    rig = _rig(tmp_path)
    a = write_ok(rig.server, "flagged memory a about caching")
    b = write_ok(rig.server, "flagged memory b about invalidation")
    flagged = payload(
        call(
            rig.server,
            "hive_flag",
            {"a": a, "b": b, "kind": "conflict"},
            identity=B,
        )
    )
    assert flagged.get("status") not in (None, "disabled"), flagged
    env = _prune(rig, a, identity=A)
    _assert_gate_noop(env, rig, a)  # agent-asserted advisory rows do NOT qualify


# ── general memories: clauses 2–3 only, drift is n/a ──────────────────────────


def test_general_memory_outcome_clause_still_works(tmp_path):
    rig = _rig(tmp_path)
    eid = write_ok(rig.server, "a general memory that hurt agent B")
    payload(call(rig.server, "hive_outcome", {"hurt": [eid]}, identity=B))
    env = _prune(rig, eid, identity=A)
    assert env.get("status") == "pruned", f"general memories retire on clause 2: {env}"


def test_general_memory_without_evidence_noops_drift_na(tmp_path):
    rig = _rig(tmp_path)
    eid = write_ok(rig.server, "a healthy general memory with no anchors at all")
    env = _prune(rig, eid)
    _assert_gate_noop(env, rig, eid)  # drift can never qualify a general memory


# ── existing noop/failure shapes preserved ────────────────────────────────────


def test_unknown_and_deprecated_targets_keep_existing_noop_shapes(tmp_path):
    rig = _rig(tmp_path)
    env = _prune(rig, 424_242)
    assert env.get("status") == "noop", f"unknown id stays a noop: {env}"

    eid = write_ok(rig.server, "retire me twice")
    insert_verify_audit(rig.store, eid, "outcome_verified_hurt", ts=100)
    first = _prune(rig, eid)
    assert first.get("status") == "pruned"
    second = _prune(rig, eid)
    assert second.get("status") == "noop", (
        f"an already-retired target is a noop, never a double audit: {second}"
    )

    lw = write_ok(rig.server, "self supersede guard")
    env = _supersede(rig, lw, lw)
    assert env.get("status") == "noop", f"self-supersede stays refused: {env}"


# ── the gate feed fails CLOSED ────────────────────────────────────────────────


def test_gate_feed_reader_fault_fails_closed(tmp_path, monkeypatch):
    rig = _rig(tmp_path)
    eid = write_ok(rig.server, "a memory with real hurt evidence")
    insert_verify_audit(rig.store, eid, "outcome_verified_hurt", ts=100)

    def boom(*a, **kw):
        raise RuntimeError("gate feed exploded")

    # every §3.2 gate feed raises → undecidable ⇒ ineligible ⇒ noop, NEVER a retire
    monkeypatch.setattr(rig.store, "evidence_rows_for", boom, raising=False)
    monkeypatch.setattr(rig.store, "drift_get", boom, raising=False)
    resp = call(rig.server, "hive_prune", {"episode_id": eid}, identity=A)
    env = payload(resp)
    assert env.get("status") == "noop", (
        f"an undecidable gate feed fails CLOSED — noop, never a retire: {env}"
    )
    assert trust_of(rig.store, eid) != DEPRECATED
