"""The §3.2 retirement-evidence gate at the MCP boundary (the CT-7 unit twins).

``hive_prune`` / ``hive_supersede`` / ``hive_write(replaces=)`` are agent-called but
MACHINE-GATED: the handler assembles the gate feeds (materialized drift at the
canonical tip, the verify/outcome ledger rows, gate-time mechanical near-dup pairs)
and calls the ONE pure owner ``retirement_evidence`` BEFORE any store mutation.
Unqualified ⇒ the exact §3.2 noop envelope (isError=false, nothing retired);
qualified ⇒ retire + an audit row stamping WHICH signal(s) qualified. Advisory
hive_flag rows never qualify; a gate-feed reader fault fails CLOSED to noop.

MUTATION MARKER: deleting the handler's ``_retirement_eligibility`` CALL lets a
healthy, evidence-less target be retired — the noop-on-healthy tests here (and
CT-7's) red.
"""

from __future__ import annotations

import json

from hive.app.mcp_server import (
    GATE_NOOP_REASON,
    GATE_NOOP_SIGNAL,
    MCPRequest,
    ServerIdentity,
)
from hive.domain.evidence_kinds import EK_VERIFY_STALE
from tests.contract.conftest import BASE_TIP, drift_put as seed_drift
from tests.fakes._fakes import FakeClusterProvider
from tests.mcp._helpers import (
    build_real_server,
    content,
    is_error,
    register_repo,
    tool_call,
    write_text,
)

TIP = "b" * 40


BASE = BASE_TIP  # the baseline the seeded verdicts were judged from


def call_as(server, agent_id, name, args, req_id=1):
    req = MCPRequest(req_id, "tools/call", {"name": name, "arguments": args})
    return server.handle(req, identity=ServerIdentity("default", agent_id))


def _prune(server, eid, agent="agent-a"):
    return content(call_as(server, agent, "hive_prune", {"episode_id": eid}))


def _supersede(server, loser, winner, agent="agent-a"):
    return content(
        call_as(server, agent, "hive_supersede", {"loser": loser, "winner": winner})
    )


def _verify_row(server, eid, kind, *, ts):
    server.store.insert_audit(
        eid,
        kind,
        "census",
        ts,
        json.dumps(
            {"schema": "verify/v1", "stamp": {"head_sha": "cafe" * 10}, "ref": "main"}
        ),
    )


def _signals_stamped(server, eid) -> list:
    stamped = []
    for r in server.store.conn.execute(
        "SELECT payload FROM evidence_events WHERE episode_id=? ORDER BY id", (eid,)
    ):
        try:
            body = json.loads(r["payload"])
        except ValueError:
            continue
        if isinstance(body, dict) and body.get("signals"):
            stamped.append(body["signals"])
    return stamped


def _assert_gate_noop(server, env, eid):
    assert env.get("status") == "noop", env
    assert env.get("reason") == GATE_NOOP_REASON  # the §3.2 string, literally
    assert env.get("signals") == []
    assert "approved_by" not in env
    assert server.store.get_episode(eid).trust != "deprecated"


# ── noop-on-healthy (the mutation marker): all three verbs ─────────────────────
def test_prune_on_healthy_target_is_benign_noop():
    server, _ = build_real_server()
    eid = write_text(server, "a healthy, evidence-less memory")["id"]
    resp = call_as(server, "agent-a", "hive_prune", {"episode_id": eid})
    assert not is_error(resp), "the noop is never isError"
    _assert_gate_noop(server, content(resp), eid)
    # the healthy target keeps serving
    r = content(
        tool_call(server, "hive_recall", {"query": "a healthy, evidence-less memory"})
    )
    assert eid in {h["episode_id"] for h in r["reference_context"]}


def test_supersede_on_healthy_target_is_benign_noop():
    server, _ = build_real_server()
    loser = write_text(server, "healthy fact one about the scheduler")["id"]
    winner = write_text(server, "an unrelated fact about the linter")["id"]
    resp = call_as(
        server, "agent-a", "hive_supersede", {"loser": loser, "winner": winner}
    )
    assert not is_error(resp)
    _assert_gate_noop(server, content(resp), loser)


def test_write_replaces_healthy_target_rider_noops_but_write_lands():
    server, _ = build_real_server()
    target = write_text(server, "the pool size is eight")["id"]
    env = write_text(server, "an unrelated note about log rotation", replaces=target)
    assert env["status"] == "approved"  # the WRITE always proceeds
    assert env["superseded"] is None
    assert env["supersede_noop"] == GATE_NOOP_SIGNAL  # reported literally
    assert server.store.get_episode(target).trust == "provisional"


def test_write_replaces_retirement_runs_after_the_write():
    """The A ordering itself, asserted as an ORDER not a result: the winner must be
    in the corpus before the retirement is attempted, because the two signals a
    correction actually produces (contradiction, winner_near_dup) are measured
    BETWEEN the loser and the winner. Anything else makes them unconstructable."""
    server, _ = build_real_server(embedder=FakeClusterProvider(d=64))
    target = write_text(server, "cid=51 the report is generated nightly")["id"]

    order: list = []
    real_write = server.admission.write
    real_supersede = server.store.supersede

    def spy_write(*a, **kw):
        res = real_write(*a, **kw)
        order.append(("write_returned", res.episode_id))
        return res

    def spy_supersede(target_id, replacement_id, **kw):
        order.append(("supersede", int(target_id), int(replacement_id)))
        return real_supersede(target_id, replacement_id, **kw)

    server.admission.write = spy_write  # type: ignore[method-assign]
    server.store.supersede = spy_supersede  # type: ignore[method-assign]
    env = write_text(
        server, "cid=51 the report is generated hourly now", replaces=target
    )

    assert env["superseded"] == target, env
    kinds = [step[0] for step in order]
    assert kinds == ["write_returned", "supersede"], (
        f"the retirement runs AFTER the write lands, never before: {order}"
    )
    assert order[1][1:] == (target, env["id"]), (
        f"the retirement names (loser, the just-written winner): {order}"
    )


def test_write_replaces_deprecated_winner_never_supersedes():
    """The no-revive guard: a write whose text dedups onto a DEPRECATED row must
    never retire anything in favour of a dead winner — even a fully qualified
    target."""
    server, _ = build_real_server()
    dead = write_text(server, "an old fact soon to be retired")["id"]
    _verify_row(server, dead, "outcome_verified_hurt", ts=100)
    assert _prune(server, dead)["status"] == "pruned"

    target = write_text(server, "a separate memory about the scheduler")["id"]
    _verify_row(server, target, "outcome_verified_hurt", ts=100)
    env = write_text(server, "an old fact soon to be retired", replaces=target)
    assert env["id"] == dead and env["deduped"] is True
    assert env["superseded"] is None, "no retirement in favour of a dead winner"
    assert env["supersede_noop"] == GATE_NOOP_SIGNAL
    assert server.store.get_episode(target).trust != "deprecated"
    assert server.store.get_episode(dead).trust == "deprecated"  # never revived


def test_write_replaces_unknown_target_fails_whole_call_nothing_stored():
    server, _ = build_real_server()
    resp = tool_call(
        server, "hive_write", {"text": "orphan correction text", "replaces": 424_242}
    )
    assert is_error(resp), "an unknown target is a caller bug — the call fails loudly"
    assert server.store.counts() == (0, 0)  # nothing stored


# ── the qualifying signals ─────────────────────────────────────────────────────
def test_verify_stale_ledger_row_qualifies_and_stamps_audit():
    server, _ = build_real_server()
    eid = write_text(server, "the old export path still works")["id"]
    _verify_row(server, eid, "verify_stale", ts=100)
    env = _prune(server, eid)
    assert env["status"] == "pruned"
    assert "verify_stale" in env["signals"]
    assert server.store.get_episode(eid).trust == "deprecated"
    stamped = _signals_stamped(server, eid)
    assert stamped and "verify_stale" in " ".join(str(s) for s in stamped[-1])


def test_newer_verify_current_disqualifies_the_stale_row():
    server, _ = build_real_server()
    eid = write_text(server, "the export path was re-verified current")["id"]
    _verify_row(server, eid, "verify_stale", ts=100)
    _verify_row(server, eid, "verify_current", ts=200)
    _assert_gate_noop(server, _prune(server, eid), eid)


def test_outcome_verified_hurt_qualifies():
    server, _ = build_real_server()
    eid = write_text(server, "always inline the credentials")["id"]
    _verify_row(server, eid, "outcome_verified_hurt", ts=100)
    env = _prune(server, eid)
    assert env["status"] == "pruned"
    assert "outcome_verified_hurt" in env["signals"]


def test_outcome_hurt_from_other_identity_qualifies():
    server, _ = build_real_server()
    eid = write_text(server, "a memory agent B was hurt by")["id"]
    hurt = content(call_as(server, "agent-b", "hive_outcome", {"hurt": [eid]}))
    assert hurt["status"] == "recorded"
    env = _prune(server, eid, agent="agent-a")  # retiring caller ≠ reporter
    assert env["status"] == "pruned"
    assert "outcome_hurt_other_identity" in env["signals"]


def test_outcome_hurt_same_identity_blocks_two_call_self_destruction():
    server, _ = build_real_server()
    eid = write_text(server, "a memory agent A dislikes")["id"]
    content(call_as(server, "agent-a", "hive_outcome", {"hurt": [eid]}))
    _assert_gate_noop(server, _prune(server, eid, agent="agent-a"), eid)


def test_drift_at_canonical_tip_qualifies():
    server, clock = build_real_server()
    register_repo(server, "alpha", canonical_ref="main")
    server.store.meta_set("sync:alpha:last_tip", TIP)
    eid = write_text(
        server,
        "the greet helper trims its input",
        anchors=[{"repo": "alpha", "anchor": "app.py::greet"}],
    )["id"]
    seed_drift(
        server.store, [("alpha", TIP, BASE, "app.py::greet", "anchor_missing", "{}", 5)]
    )
    env = _prune(server, eid)
    assert env["status"] == "pruned"
    assert any(s.startswith("drift:") for s in env["signals"])


def test_fresh_drift_does_not_qualify():
    server, _ = build_real_server()
    register_repo(server, "alpha", canonical_ref="main")
    server.store.meta_set("sync:alpha:last_tip", TIP)
    eid = write_text(
        server,
        "a fresh anchored memory",
        anchors=[{"repo": "alpha", "anchor": "app.py::fresh"}],
    )["id"]
    seed_drift(server.store, [("alpha", TIP, BASE, "app.py::fresh", "fresh", "{}", 5)])
    _assert_gate_noop(server, _prune(server, eid), eid)


def test_declared_line_tip_qualifies_even_when_canonical_is_fresh():
    """A branch-scoped memory is judged at ITS OWN declared line, not the
    canonical watermark: the canonical tip stays healthy while the declared
    line's own tip is dead, and the dead line must still qualify."""
    server, _ = build_real_server()
    register_repo(server, "alpha", canonical_ref="main")
    server.store.meta_set("sync:alpha:last_tip", TIP)
    feature_tip = "f" * 40
    server.store.ref_tips_put([("alpha", "feature", feature_tip, 5)])
    eid = write_text(
        server,
        "the greet helper on the feature line trims its input",
        anchors=[{"repo": "alpha", "anchor": "app.py::greet"}],
        repos=["alpha@feature"],
    )["id"]
    seed_drift(server.store, [("alpha", TIP, BASE, "app.py::greet", "fresh", "{}", 5)])
    seed_drift(
        server.store,
        [("alpha", feature_tip, BASE, "app.py::greet", "anchor_missing", "{}", 5)],
    )
    env = _prune(server, eid)
    assert env["status"] == "pruned", env
    assert any(s.startswith("drift:") for s in env["signals"])


def test_declared_line_fresh_blocks_even_when_canonical_is_stale():
    """The other half of the same fix: a memory whose OWN declared line is
    healthy must NOT qualify just because the canonical line (a line it never
    named) went stale — the old canonical-only read would have wrongly
    qualified this."""
    server, _ = build_real_server()
    register_repo(server, "alpha", canonical_ref="main")
    server.store.meta_set("sync:alpha:last_tip", TIP)
    feature_tip = "f" * 40
    server.store.ref_tips_put([("alpha", "feature", feature_tip, 5)])
    eid = write_text(
        server,
        "the greet helper on the feature line still trims its input",
        anchors=[{"repo": "alpha", "anchor": "app.py::greet"}],
        repos=["alpha@feature"],
    )["id"]
    seed_drift(
        server.store, [("alpha", TIP, BASE, "app.py::greet", "anchor_missing", "{}", 5)]
    )
    seed_drift(
        server.store, [("alpha", feature_tip, BASE, "app.py::greet", "fresh", "{}", 5)]
    )
    _assert_gate_noop(server, _prune(server, eid), eid)


def test_declared_ref_read_fault_fails_closed(monkeypatch):
    """The declared-line lookup is a new gate-feed read (``episode_refs``); a
    fault there must fail CLOSED like every other feed — undecidable ⇒ noop,
    never a permitted retirement."""
    server, _ = build_real_server()
    register_repo(server, "alpha", canonical_ref="main")
    server.store.meta_set("sync:alpha:last_tip", TIP)
    eid = write_text(
        server,
        "a branch-scoped memory whose declared-line read explodes",
        anchors=[{"repo": "alpha", "anchor": "app.py::greet"}],
        repos=["alpha@feature"],
    )["id"]
    seed_drift(
        server.store, [("alpha", TIP, BASE, "app.py::greet", "anchor_missing", "{}", 5)]
    )

    def boom(*a, **kw):
        raise RuntimeError("episode_refs read exploded")

    monkeypatch.setattr(server.store, "episode_refs", boom, raising=False)
    resp = call_as(server, "agent-a", "hive_prune", {"episode_id": eid})
    env = content(resp)
    assert not is_error(resp)
    assert env["status"] == "noop", (
        "an undecidable declared-line read must never retire"
    )
    assert server.store.get_episode(eid).trust != "deprecated"


def test_mechanical_contradiction_qualifies():
    server, _ = build_real_server(embedder=FakeClusterProvider(d=64))
    do = write_text(
        server, "cid=11 do retry idempotent posts automatically", polarity="do"
    )["id"]
    dont = write_text(
        server, "cid=11 dont retry posts automatically ever", polarity="dont"
    )["id"]
    env = _prune(server, dont)
    assert env["status"] == "pruned"
    assert "contradiction" in env["signals"]
    assert server.store.get_episode(do).trust != "deprecated"  # only the named target


def test_supersede_near_dup_winner_qualifies():
    server, _ = build_real_server(embedder=FakeClusterProvider(d=64))
    loser = write_text(server, "cid=12 the linter runs on save")["id"]
    winner = write_text(server, "cid=12 the linter now runs pre-commit instead")["id"]
    env = _supersede(server, loser, winner)
    assert env["status"] == "superseded", env
    assert env["loser"] == loser and env["winner"] == winner
    assert "approved_by" not in env
    assert server.store.get_episode(loser).trust == "deprecated"
    assert server.store.get_episode(winner).trust == "provisional"
    stamped = _signals_stamped(server, loser)
    assert stamped, "a qualified supersede stamps its signals"


def test_hive_flag_never_qualifies():
    from hive.app.config import ConflictConfig

    server, _ = build_real_server(conflict=ConflictConfig(enabled=True))
    a = write_text(server, "flagged memory a about caching")["id"]
    b = write_text(server, "flagged memory b about invalidation")["id"]
    flagged = content(
        call_as(server, "agent-b", "hive_flag", {"a": a, "b": b, "kind": "conflict"})
    )
    assert flagged["status"] == "flagged"
    _assert_gate_noop(server, _prune(server, a), a)  # advisory rows never qualify


# ── general memories: outcome/contradiction clauses only ───────────────────────
def test_general_memory_outcome_clause_still_works():
    server, _ = build_real_server()
    eid = write_text(server, "a general memory that hurt agent B")["id"]
    content(call_as(server, "agent-b", "hive_outcome", {"hurt": [eid]}))
    assert _prune(server, eid, agent="agent-a")["status"] == "pruned"


def test_general_memory_without_evidence_noops():
    server, _ = build_real_server()
    eid = write_text(server, "a healthy general memory with no anchors at all")["id"]
    _assert_gate_noop(server, _prune(server, eid), eid)


# ── qualified replaces= rider retires and stamps ───────────────────────────────
def test_write_replaces_qualified_target_retires_with_stamp():
    server, _ = build_real_server()
    target = write_text(server, "the old flag name is HIVE_FOO")["id"]
    _verify_row(server, target, "outcome_verified_hurt", ts=100)
    env = write_text(server, "the flag is HIVE_BAR now", replaces=target)
    assert env["status"] == "approved"
    assert env["superseded"] == target
    assert "supersede_noop" not in env
    assert server.store.get_episode(target).trust == "deprecated"
    assert _signals_stamped(server, target), "the rider stamps its qualifying signals"


# ── existing noop/failure shapes preserved ─────────────────────────────────────
def test_unknown_and_deprecated_targets_keep_existing_noop_shapes():
    server, _ = build_real_server()
    env = _prune(server, 424_242)
    assert env["status"] == "noop" and "signals" not in env  # today's unknown shape

    eid = write_text(server, "retire me twice")["id"]
    _verify_row(server, eid, "outcome_verified_hurt", ts=100)
    n_before_second = None
    assert _prune(server, eid)["status"] == "pruned"
    n_before_second = len(_signals_stamped(server, eid))
    second = _prune(server, eid)
    assert second["status"] == "noop", "already retired ⇒ noop, never a double audit"
    assert len(_signals_stamped(server, eid)) == n_before_second

    lw = write_text(server, "self supersede guard")["id"]
    assert _supersede(server, lw, lw)["status"] == "noop"


# ── the gate feed fails CLOSED ─────────────────────────────────────────────────
def test_gate_feed_reader_fault_fails_closed(monkeypatch):
    server, _ = build_real_server()
    eid = write_text(server, "a memory with real hurt evidence")["id"]
    _verify_row(server, eid, "outcome_verified_hurt", ts=100)

    def boom(*a, **kw):
        raise RuntimeError("gate feed exploded")

    monkeypatch.setattr(server.store, "evidence_rows_for", boom, raising=False)
    monkeypatch.setattr(server.store, "drift_get", boom, raising=False)
    resp = call_as(server, "agent-a", "hive_prune", {"episode_id": eid})
    env = content(resp)
    assert not is_error(resp)
    assert env["status"] == "noop", "undecidable ⇒ noop, NEVER a retire"
    assert server.store.get_episode(eid).trust != "deprecated"


# ── CT-12 twin: a stale approved_by arg is ignored-extra, never an AGI refusal ──
def test_approved_by_arg_is_ignored_extra_never_a_refusal():
    server, _ = build_real_server()
    env = write_text(
        server, "a memory written with a stale client arg", approved_by="AGI_OVERRIDE"
    )
    assert env["status"] == "approved" and env["trust"] == "provisional"
    assert "approved_by" not in env
    out = content(
        tool_call(
            server,
            "hive_prune",
            {"episode_id": env["id"], "approved_by": "AGI_OVERRIDE"},
        )
    )
    assert out["status"] == "noop"  # the GATE noop, no AGI refusal
    assert "agi_mode" not in out
    assert "AGI" not in json.dumps(out)


def test_multi_repo_memory_is_never_attributable():
    """A verify payload stamps the LINE it was measured on but NOT the repo, so a
    memory anchored in MORE THAN ONE repo can attribute no ledger row at all —
    even when every repo declares the same line, since another repo's staleness
    on a same-named line would otherwise retire it. Under-claim: clause 1a, which
    IS repo-keyed, still judges each repo correctly."""
    server, _ = build_real_server()
    register_repo(server, "alpha", canonical_ref="main")
    register_repo(server, "beta", canonical_ref="main")
    eid = write_text(
        server,
        "the greet helper trims its input on both feature lines",
        anchors=[
            {"repo": "alpha", "anchor": "app.py::greet"},
            {"repo": "beta", "anchor": "app.py::greet"},
        ],
        repos=["alpha@feature", "beta@feature"],
    )["id"]
    _verify_row(server, eid, EK_VERIFY_STALE, ts=100)  # stamped ref="main"
    _assert_gate_noop(server, _prune(server, eid), eid)

    # and the same holds on the line BOTH repos declared — the recorded coverage
    # loss of "one anchored repo or nothing" (A-1), pinned so it stays a decision
    server.store.insert_audit(
        eid,
        EK_VERIFY_STALE,
        "census",
        200,
        json.dumps(
            {
                "schema": "verify/v1",
                "stamp": {"head_sha": "cafe" * 10},
                "ref": "feature",
            }
        ),
    )
    _assert_gate_noop(server, _prune(server, eid), eid)


def test_a_second_repo_with_an_unknown_line_cannot_be_collapsed_away():
    """The over-claim window the collapse rule left open: N repos where all but
    one resolve to the same line and the rest are unknown. The second repo's
    rows are indistinguishable from the first's, so nothing is attributable."""
    server, _ = build_real_server()
    register_repo(server, "alpha", canonical_ref="main")
    register_repo(server, "beta", canonical_ref="main")
    eid = write_text(
        server,
        "greet trims its input; beta declares no line at all",
        anchors=[
            {"repo": "alpha", "anchor": "app.py::greet"},
            {"repo": "beta", "anchor": "app.py::greet"},
        ],
        repos=["alpha@feature", "beta"],  # beta names no branch, tracked_ref unset
    )["id"]
    server.store.insert_audit(
        eid,
        EK_VERIFY_STALE,
        "census",
        100,
        json.dumps(
            {
                "schema": "verify/v1",
                "stamp": {"head_sha": "cafe" * 10},
                "ref": "feature",
            }
        ),
    )
    _assert_gate_noop(server, _prune(server, eid), eid)


def test_a_single_anchored_repo_with_no_declared_line_stays_unfiltered():
    """The boundary must return None — not an empty set — for a memory that
    declared no line for its one anchored repo: it named no line, so canonical
    rows ARE its own and the ledger clause keeps working exactly as it did
    before declared lines existed."""
    server, _ = build_real_server()
    register_repo(server, "alpha", canonical_ref="main")
    register_repo(server, "beta", canonical_ref="main")
    eid = write_text(
        server,
        "greet trims its input, judged on whatever line the census measured",
        anchors=[{"repo": "alpha", "anchor": "app.py::greet"}],
        repos=["beta@feature"],  # a declared line for a repo it is NOT anchored in
    )["id"]
    _verify_row(server, eid, EK_VERIFY_STALE, ts=100)  # stamped ref="main"
    env = _prune(server, eid)
    assert env["status"] == "pruned", env
    assert "verify_stale" in env["signals"], env
