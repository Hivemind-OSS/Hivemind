"""CT-1 — write serves now as provisional (plan §4 intent 1, §3.3).

Given a connected agent, when ``hive_write{text}`` (NO approver field exists), the
row is servable immediately with ``trust=provisional`` and a matching recall serves
it labeled provisional. Full scenario matrix: plain write; anchors in 1 and 2
repos; general write; dedup onto quarantined (lifts to provisional + audit); dedup
onto provisional/established (idempotent); dedup onto deprecated (no revive);
secret refuse (0 rows); redact; ``replaces=`` qualified vs evidence-less vs
unknown target; unknown repo name refused clean.
"""

from __future__ import annotations

import pytest

from hive.domain.lifecycle import DEPRECATED, ESTABLISHED, PROVISIONAL, QUARANTINED
from tests.contract.conftest import (
    NOOP_REASON,
    SECRET_TEXT,
    call,
    capture,
    evidence_rows,
    hit_for,
    ident,
    insert_verify_audit,
    is_error,
    make_rig,
    n_episodes,
    payload,
    recall,
    register_repo,
    served_ids,
    trust_of,
    write,
    write_ok,
)

T = "use connection pooling for the database, never one connection per request"


def _register_ab(rig) -> None:
    register_repo(rig.store, "alpha", "https://example.invalid/alpha.git")
    register_repo(rig.store, "beta", "https://example.invalid/beta.git")


# ── plain write → provisional, served now ─────────────────────────────────────


def test_plain_write_lands_provisional_and_serves(rig):
    env = write(rig.server, T)
    assert env.get("status") == "approved", f"v3 write did not land: {env}"
    assert env.get("trust") == "provisional", (
        f"v3 write envelope must label the row provisional: {env}"
    )
    assert isinstance(env.get("id"), int)
    assert "approved_by" not in env, "no approver field exists in v3"
    assert trust_of(rig.store, env["id"]) == PROVISIONAL

    served = recall(rig.server, T)
    assert served.get("abstained") is False, (
        f"provisional write must serve now: {served}"
    )
    hit = hit_for(served, env["id"])
    assert hit["trust"] == "provisional"


def test_write_with_anchor_in_one_repo(rig):
    _register_ab(rig)
    eid = write_ok(
        rig.server,
        "the recall gate lives in hive/domain/recall.py",
        anchors=[{"repo": "alpha", "anchor": "hive/domain/recall.py::RecallPipeline"}],
    )
    served = recall(rig.server, "the recall gate lives in hive/domain/recall.py")
    hit = hit_for(served, eid)
    assert hit.get("repos") == ["alpha"], f"hit must carry its repo set: {hit}"
    assert hit.get("anchors") == [
        {"repo": "alpha", "anchor": "hive/domain/recall.py::RecallPipeline"}
    ]


def test_write_with_anchors_in_two_repos(rig):
    _register_ab(rig)
    eid = write_ok(
        rig.server,
        "both services share the same retry idiom",
        anchors=[
            {"repo": "alpha", "anchor": "svc/a.py::retry"},
            {"repo": "beta", "anchor": "svc/b.py::retry"},
        ],
    )
    served = recall(rig.server, "both services share the same retry idiom")
    hit = hit_for(served, eid)
    assert sorted(hit.get("repos") or []) == ["alpha", "beta"]
    assert {(a["repo"], a["anchor"]) for a in hit.get("anchors") or []} == {
        ("alpha", "svc/a.py::retry"),
        ("beta", "svc/b.py::retry"),
    }


def test_general_write_carries_empty_scope(rig):
    eid = write_ok(rig.server, "prefer small PRs over big-bang merges")
    served = recall(rig.server, "prefer small PRs over big-bang merges")
    hit = hit_for(served, eid)
    assert hit.get("repos") == [], f"a general memory's repos is []: {hit}"
    assert hit.get("anchors") == []


# ── dedup semantics on every trust state ──────────────────────────────────────


def test_dedup_onto_quarantined_lifts_to_provisional_with_audit(rig):
    cap = capture(rig.server, T, identity=ident("writer-a"))
    assert cap.get("status") == "quarantined"
    eid = cap["id"]
    before = len(evidence_rows(rig.store, eid))

    env = write(rig.server, T, identity=ident("writer-b"))
    assert env.get("id") == eid, "dedup must land on the existing row"
    assert trust_of(rig.store, eid) == PROVISIONAL, (
        "v3 dedup-onto-quarantined LIFTS to provisional (never established)"
    )
    after = len(evidence_rows(rig.store, eid))
    assert after > before, (
        "the quarantined→provisional lift must write an audit row (a silent "
        "lift is the named mutation)"
    )


def test_dedup_onto_provisional_is_idempotent(rig):
    eid = write_ok(rig.server, T)
    env2 = write(rig.server, T)
    assert env2.get("id") == eid
    assert env2.get("deduped") is True
    assert trust_of(rig.store, eid) == PROVISIONAL


def test_dedup_onto_established_keeps_established(rig):
    eid = write_ok(rig.server, T)
    assert rig.store.set_trust(eid, ESTABLISHED, now=int(rig.clock.now()))
    env2 = write(rig.server, T)
    assert env2.get("id") == eid
    assert env2.get("deduped") is True
    assert trust_of(rig.store, eid) == ESTABLISHED, "a write never demotes"


def test_dedup_onto_deprecated_never_revives(rig):
    eid = write_ok(rig.server, T)
    assert rig.store.deprecate(eid, actor="ct", ts=int(rig.clock.now()))
    env2 = write(rig.server, T)
    assert env2.get("id") == eid
    assert env2.get("deduped") is True
    assert trust_of(rig.store, eid) == DEPRECATED
    served = recall(rig.server, T)
    assert eid not in served_ids(served), "a deprecated row must stay retired"


# ── the secret floor (refuse / redact) ────────────────────────────────────────


def test_secret_refuse_writes_zero_rows(rig):
    before = n_episodes(rig.server)
    env = write(rig.server, SECRET_TEXT)
    assert env.get("status") == "refused", f"credential text must refuse: {env}"
    assert env.get("id") in (None,), "refuse stores nothing"
    scan = env.get("scan") or {}
    assert scan.get("rules"), "the refusal names its rules"
    assert n_episodes(rig.server) == before


def test_redact_masks_and_lands_provisional(tmp_path, monkeypatch):
    # The redact disposition is the scanner adapter's operator seam
    # (redact_mode) — the composition root wires it; force redact mode through
    # the same real adapter so the REAL redact path runs end-to-end.
    import hive.app.container as container_mod
    from hive.adapters.scanner_regex import DefaultSecretScanner
    from hive.domain.secret_scan import REDACT

    monkeypatch.setattr(
        container_mod,
        "DefaultSecretScanner",
        lambda enabled=True: DefaultSecretScanner(redact_mode=REDACT, enabled=enabled),
    )
    rig = make_rig(tmp_path)
    env = write(rig.server, SECRET_TEXT)
    assert env.get("status") == "redacted", f"redact-mode secret must redact: {env}"
    assert env.get("trust") == "provisional"
    assert isinstance(env.get("id"), int)
    assert "sk-ABCDEFGHIJKLMNOPQRSTUVWX12345678" not in env.get("redacted_preview", "")
    assert trust_of(rig.store, env["id"]) == PROVISIONAL


# ── the replaces= retirement rider (gate-passes vs gate-fails vs unknown) ─────


def test_replaces_qualified_target_is_retired(rig):
    target = write_ok(rig.server, "the old flag name is HIVE_FOO")
    # a qualifying machine signal: a server-written verified-hurt ledger row
    insert_verify_audit(
        rig.store, target, "outcome_verified_hurt", ts=int(rig.clock.now())
    )
    env = write(
        rig.server, "the flag is HIVE_BAR now (HIVE_FOO is dead)", replaces=target
    )
    assert env.get("status") == "approved"
    assert env.get("superseded") == target, (
        f"a qualified replaces target must be retired: {env}"
    )
    assert trust_of(rig.store, target) == DEPRECATED
    served = recall(rig.server, "the old flag name is HIVE_FOO")
    assert target not in served_ids(served)


def test_replaces_evidence_less_target_noops_but_write_lands(rig):
    target = write_ok(rig.server, "the cache TTL is five minutes")
    before = trust_of(rig.store, target)
    env = write(rig.server, "the cache TTL is ten minutes", replaces=target)
    assert env.get("status") == "approved", (
        f"the WRITE always proceeds — only the rider no-ops: {env}"
    )
    assert isinstance(env.get("id"), int)
    assert env.get("superseded") is None, f"nothing may be retired: {env}"
    assert env.get("supersede_noop") == NOOP_REASON, (
        f"the envelope reports the rider noop literally: {env}"
    )
    assert trust_of(rig.store, target) == before == PROVISIONAL
    served = recall(rig.server, "the cache TTL is five minutes")
    assert target in served_ids(served), "the healthy target keeps serving"


def test_replaces_unknown_target_fails_whole_call_nothing_stored(rig):
    before = n_episodes(rig.server)
    resp = call(
        rig.server,
        "hive_write",
        {"text": "orphan correction text", "replaces": 424_242},
    )
    env = payload(resp)
    assert is_error(resp) or env.get("status") == "refused", (
        f"an unknown replaces target fails the whole call loudly: {env}"
    )
    assert n_episodes(rig.server) == before, "nothing stored on the failed call"
    served = recall(rig.server, "orphan correction text")
    assert served.get("abstained") is not False or not served_ids(served)


# ── boundary grammar: unknown repo names refuse clean ─────────────────────────


@pytest.mark.parametrize(
    "extra",
    [
        {"anchors": [{"repo": "ghost", "anchor": "x.py::f"}]},
        {"repos": ["ghost"]},
    ],
)
def test_unknown_repo_name_refused_nothing_stored(rig, extra):
    _register_ab(rig)
    before = n_episodes(rig.server)
    resp = call(rig.server, "hive_write", {"text": "scoped to a ghost repo", **extra})
    env = payload(resp)
    assert env.get("status") == "refused", (
        f"an unregistered repo name is a clean refusal: {env}"
    )
    assert not is_error(resp), "a grammar refusal is a clean envelope, not a tool error"
    assert n_episodes(rig.server) == before


def test_quarantined_capture_stays_unserved(rig):
    # write-vs-capture floor: the capture path still quarantines (unchanged flow)
    cap = capture(rig.server, "a capture that must not serve yet")
    assert cap.get("status") == "quarantined"
    assert trust_of(rig.store, cap["id"]) == QUARANTINED
    served = recall(rig.server, "a capture that must not serve yet")
    assert cap["id"] not in served_ids(served)
