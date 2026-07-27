"""The FROZEN contract suite for the minimal-hardening removal (I1-I8).

Four defects are closed by DELETING machinery, so this file states what the system
must do AFTERWARDS, in the vocabulary the served contract uses:

  I1  near-duplication never authorizes destroying a memory;
  I2  ``hive_supersede`` still retires on a demonstrated near-dup successor;
  I3  a control character in an anchor is refused at the write boundary;
  I4  no single stored binding can fault a repo's whole drift leg;
  I5  a served hit carries EXACTLY ONE staleness answer (``drift``);
  I6  the git drift feed and the census feed's other products are untouched;
  I7  every served hit clears ``recall.tau_serve``;
  I8  the served contract advertises only signals the gate can produce.

Every test drives the REAL surfaces: the real container + ``HiveMCPServer.handle``,
the real ``SyncService.tick()`` over real tmp git origins, and the real
``anchor_grammar`` write boundary. Store reads appear only to assert the ABSENCE of
a side effect (no audit row, no exposure row, no stored binding).

RED-FIRST MECHANICS: authored against the UN-BUILT system; every not-yet-existing
name is reached through a ``require_*`` guard so red is an ASSERTION, never a
collection error.

FROZEN: no later chunk may edit this file. A genuine defect in it is an escalation.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

import numpy as np
import pytest

from hive.app.config import SyncConfig
from hive.app.contract import (
    METADATA_FIELD_LIMIT,
    REMEDIATION_NOTICE,
    SERVER_INSTRUCTIONS,
)
from hive.app.mcp_server import MCPRequest
from hive.app.sync import SyncService
from hive.app.tool_defs import TOOL_DEFINITIONS
from hive.domain.anchor_grammar import anchor_grammar_error, probe_target
from hive.domain.change_evidence import ChangeEvidenceService
from hive.domain.evidence_kinds import (
    EK_OUTCOME_VERIFIED_HELPED,
    EK_PRUNE,
    EK_STALE_SUSPECT,
    EK_SUPERSEDE,
)
from hive.domain.retirement import QUALIFYING_DRIFT
from tests.contract.conftest import (
    NOOP_REASON,
    call,
    evidence_rows,
    ident,
    insert_verify_audit,
    make_rig,
    meta_value,
    payload,
    recall,
    register_repo,
    require_method,
    served_ids,
)
from tests.fakes._fakes import FakeClusterProvider
from tests.sync.conftest import Origin

REPO = "alpha"
ANCHOR = "app.py::greet"
DAY_S = 86_400


# ── shared helpers ────────────────────────────────────────────────────────────


def keys_anywhere(obj: Any) -> set[str]:
    """Every mapping key at ANY nesting depth of an envelope — so a rider that was
    merely re-homed under another key cannot hide from an absence assertion."""
    found: set[str] = set()
    stack: list[Any] = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            found |= {str(k) for k in cur}
            stack.extend(cur.values())
        elif isinstance(cur, (list, tuple)):
            stack.extend(cur)
    return found


def write_as(rig, agent: str, text: str, **kw) -> int:
    env = payload(
        call(rig.server, "hive_write", {"text": text, **kw}, identity=ident(agent))
    )
    assert env.get("status") in ("approved", "redacted"), f"write did not land: {env}"
    return int(env["id"])


def prune_as(rig, agent: str, episode_id: int) -> dict:
    return payload(
        call(
            rig.server,
            "hive_prune",
            {"episode_id": episode_id},
            identity=ident(agent),
        )
    )


def supersede_as(rig, agent: str, loser: int, winner: int) -> dict:
    return payload(
        call(
            rig.server,
            "hive_supersede",
            {"loser": loser, "winner": winner},
            identity=ident(agent),
        )
    )


def recall_as(rig, agent: str, query: str, **kw) -> dict:
    return recall(rig.server, query, identity=ident(agent), **kw)


def hit_for_id(env: dict, episode_id: int) -> dict:
    for hit in env.get("reference_context", []):
        if hit.get("episode_id") == episode_id:
            return hit
    raise AssertionError(f"episode {episode_id} not served: {served_ids(env)}")


def exposure_ids(rig, trace_id: Optional[str] = None) -> list[int]:
    sql = "SELECT episode_id, trace_id FROM exposure"
    rows = [dict(r) for r in rig.store.conn.execute(sql)]
    if trace_id is not None:
        rows = [r for r in rows if r["trace_id"] == trace_id]
    return [int(r["episode_id"]) for r in rows]


# ── the near-dup substrate (I1 / I2) ──────────────────────────────────────────


@pytest.fixture
def dup_rig(tmp_path):
    """A real container whose embedder clusters by ``cid=<n>``: two distinct texts
    sharing a cid are a REAL near-duplicate under ``conflict.tau``."""
    return make_rig(tmp_path, embedder=FakeClusterProvider(d=32))


ORIG = "cid=1 every outbound http client must carry an explicit connect timeout"
PARA = "cid=1 outbound http clients need an explicit connect timeout, always"
LONE = "cid=9 readiness markers in a durable volume are cleared at boot start"
FAR = "cid=7 prefer merge sort over bubble sort on large inputs"


def assert_near_dup(rig, a: int, b: int) -> float:
    """Prove the PRECONDITION the attack depends on: the two rows really are a
    near-dup under the deployed ``conflict.tau`` — otherwise a green I1 would be
    proving nothing but a weak embedder."""
    ep_a, ep_b = rig.store.get_episode(a), rig.store.get_episode(b)
    assert ep_a is not None and ep_b is not None
    cos = float(np.dot(ep_a.value, ep_b.value))
    tau = float(rig.server.conflict.tau)
    assert cos >= tau, f"precondition failed: cos={cos:.4f} < conflict.tau={tau}"
    return cos


# ── I1: redundancy never authorizes destruction ───────────────────────────────


def test_a_cold_stranger_cannot_prune_a_near_duplicated_memory(dup_rig):
    """A memory whose only 'evidence' is that someone wrote a paraphrase of it is
    not retirable — by anyone, least of all an identity with no relationship to it."""
    target = write_as(dup_rig, "writer-one", ORIG)
    para = write_as(dup_rig, "writer-two", PARA)
    assert_near_dup(dup_rig, target, para)

    env = prune_as(dup_rig, "attacker", target)

    assert env.get("status") == "noop", env
    assert NOOP_REASON in str(env.get("reason", "")), env
    ep = dup_rig.store.get_episode(target)
    assert ep is not None and ep.trust != "deprecated", ep
    assert evidence_rows(dup_rig.store, target, EK_PRUNE) == []


def test_an_opposing_polarity_near_dup_does_not_qualify_a_prune(dup_rig):
    """The case a relation filter would have missed: ``polarity`` is caller-supplied,
    so an attacker gets a mechanically-detected CONTRADICTION for one extra keystroke."""
    target = write_as(dup_rig, "writer-one", ORIG, polarity="do")
    opposing = write_as(dup_rig, "attacker", PARA, polarity="dont")
    assert_near_dup(dup_rig, target, opposing)

    env = prune_as(dup_rig, "attacker", target)

    assert env.get("status") == "noop", env
    ep = dup_rig.store.get_episode(target)
    assert ep is not None and ep.trust != "deprecated", ep


def test_a_writer_cannot_self_authorize_via_a_near_dup(dup_rig):
    """The same author writing both halves is the cheapest form of the attack."""
    target = write_as(dup_rig, "writer-one", ORIG)
    mine = write_as(dup_rig, "writer-one", PARA)
    assert_near_dup(dup_rig, target, mine)

    assert prune_as(dup_rig, "writer-one", target).get("status") == "noop"


def test_a_semantically_isolated_target_is_still_a_noop(dup_rig):
    """Control: the behaviour that was already correct stays correct."""
    lone = write_as(dup_rig, "writer-one", LONE)
    assert prune_as(dup_rig, "attacker", lone).get("status") == "noop"


def test_the_contradiction_signal_is_never_stamped(dup_rig):
    """The audit vocabulary loses ``contradiction`` — no envelope and no ledger row
    may carry it, and the gate must not export it as a signal name."""
    import hive.domain.retirement as retirement

    assert not hasattr(retirement, "SIG_CONTRADICTION"), (
        "retirement still exports SIG_CONTRADICTION — the pair feed survives"
    )
    target = write_as(dup_rig, "writer-one", ORIG)
    para = write_as(dup_rig, "writer-two", PARA)
    assert_near_dup(dup_rig, target, para)
    for env in (
        prune_as(dup_rig, "attacker", target),
        supersede_as(dup_rig, "attacker", target, para),
    ):
        assert "contradiction" not in json.dumps(env), env
    rows = evidence_rows(dup_rig.store, target)
    assert all("contradiction" not in (r["payload"] or "") for r in rows), rows


def test_the_gate_takes_no_conflict_pair_feed(dup_rig):
    """Structural twin of I1: the pure gate must not accept a pair feed at all, so
    no boundary can re-open the attack by re-assembling one."""
    import inspect

    from hive.domain.retirement import retirement_evidence

    params = set(inspect.signature(retirement_evidence).parameters)
    assert "conflict_pairs" not in params, params
    assert "own_lines" not in params, params
    assert not hasattr(dup_rig.server, "_gate_conflict_pairs"), (
        "the boundary still assembles a conflict-pair feed"
    )
    assert not hasattr(dup_rig.server, "_gate_own_lines"), (
        "the boundary still assembles an own-lines feed"
    )


# ── I2: the retire-and-replace flow survives I1 intact ────────────────────────


def test_a_near_dup_winner_still_supersedes(dup_rig):
    loser = write_as(dup_rig, "writer-one", ORIG)
    winner = write_as(dup_rig, "writer-two", PARA)
    assert_near_dup(dup_rig, loser, winner)

    env = supersede_as(dup_rig, "writer-one", loser, winner)

    assert env.get("status") == "superseded", env
    assert env.get("signals") == ["winner_near_dup"], env
    ep = dup_rig.store.get_episode(loser)
    assert ep is not None and ep.trust == "deprecated" and ep.superseded_by == winner


def test_a_distant_winner_does_not_supersede(dup_rig):
    loser = write_as(dup_rig, "writer-one", LONE)
    winner = write_as(dup_rig, "writer-two", FAR)

    env = supersede_as(dup_rig, "writer-one", loser, winner)

    assert env.get("status") == "noop", env
    assert NOOP_REASON in str(env.get("reason", "")), env


def test_write_replaces_still_retires_on_a_near_dup_successor(dup_rig):
    old = write_as(dup_rig, "writer-one", ORIG)
    env = payload(
        call(
            dup_rig.server,
            "hive_write",
            {"text": PARA, "replaces": old},
            identity=ident("writer-one"),
        )
    )
    assert env.get("status") in ("approved", "redacted"), env
    ep = dup_rig.store.get_episode(old)
    assert ep is not None and ep.trust == "deprecated", ep


def test_supersede_is_idempotent(dup_rig):
    loser = write_as(dup_rig, "writer-one", ORIG)
    winner = write_as(dup_rig, "writer-two", PARA)
    assert_near_dup(dup_rig, loser, winner)
    first = supersede_as(dup_rig, "writer-one", loser, winner)
    assert first.get("status") == "superseded", first
    after_first = len(evidence_rows(dup_rig.store, loser, EK_SUPERSEDE))
    assert after_first, "precondition: the first supersede did stamp the ledger"
    second = supersede_as(dup_rig, "writer-one", loser, winner)
    assert second.get("status") == "superseded", second
    # the re-run ECHOES: no duplicate audit is written
    assert len(evidence_rows(dup_rig.store, loser, EK_SUPERSEDE)) == after_first


# ── I3: a control character is refused at the write boundary ──────────────────

CONTROL_ANCHORS = (
    "a.py\x00::f",
    "a.py\n::f",
    "a.py\x1b::f",
    "a.py\x7f::f",
    "a.py::f\x00",
    "a.py::f\n",
)


@pytest.fixture
def repo_rig(tmp_path):
    rig = make_rig(tmp_path)
    register_repo(rig.store, REPO, "https://example.invalid/alpha.git")
    return rig


@pytest.mark.parametrize("anchor", CONTROL_ANCHORS)
def test_a_control_character_anchor_is_refused_at_write(repo_rig, anchor):
    """A control character cannot enter the store: refused at the boundary, with a
    message naming the offending character."""
    err = anchor_grammar_error(anchor)
    assert err is not None, f"{anchor!r} is still grammatically valid"
    assert "control character" in err, err

    env = payload(
        call(
            repo_rig.server,
            "hive_write",
            {"text": "a lesson", "anchors": [{"repo": REPO, "anchor": anchor}]},
            identity=ident("writer-one"),
        )
    )
    assert env.get("status") not in ("approved", "redacted"), env
    rows = list(
        repo_rig.store.conn.execute(
            "SELECT anchor FROM episode_anchors WHERE anchor=?", (anchor,)
        )
    )
    assert rows == [], "the refused anchor reached the store anyway"


def test_the_four_pre_existing_refusals_are_unchanged():
    """BUG-077 / BUG-058 fence: the four earlier refusal MESSAGES are byte-identical
    — the new clause may not reword or re-order them."""
    assert anchor_grammar_error("") == "has an empty path component"
    assert anchor_grammar_error("::S") == "has an empty path component"
    assert anchor_grammar_error("app.py:greet") == (
        "'app.py:greet' — the symbol separator is '::', not ':' (a single-colon "
        "anchor matches no census subject, so the memory can never be "
        "outcome-verified); write 'app.py::greet'"
    )
    assert anchor_grammar_error("app.py::") == (
        "'app.py::' names no symbol after '::' — drop the separator to bind the file"
    )
    assert anchor_grammar_error("app.py::42") == (
        "'app.py::42' — '42' is a line number, not a symbol; bind the file as 'app.py'"
    )


def test_a_nested_symbol_anchor_still_passes():
    assert anchor_grammar_error("path/file.py::Ns::C.m") is None
    assert anchor_grammar_error("app.py") is None
    assert anchor_grammar_error("prose about the deploy process") is None


def test_the_read_historical_tokenizer_is_untouched():
    """BUG-083 fence: the refusal is MINT-side only — the historical single-colon
    population must still resolve for the prober."""
    assert probe_target("app.py:greet") == ("app.py", "greet")
    assert probe_target("app.py::Ns::C.m") == ("app.py", "Ns::C.m")


# ── I4: one hostile binding may not fault a repo's drift leg ──────────────────

POISON_ANCHOR = "poison.py\x00::boom"
HEALTHY_TEXT = "greet() must stay single-arg; callers pass positionally"
POISON_TEXT = "the poisoned binding's memory, written before the gate existed"


def app_py(body: str = 'return "hi " + name') -> str:
    return f"def greet(name):\n    {body}\n\n\ndef bill(user):\n    return user\n"


def make_syncer(store, tmp_path: Path, *, lifecycle=None, **cfg_kw) -> SyncService:
    cfg = SyncConfig(mirror_dir=str(tmp_path / "mirrors"), **cfg_kw)
    evidence = ChangeEvidenceService(
        reader=store, appender=store, now=lambda: 424_242, ranges=store
    )
    return SyncService(cfg, store, evidence, threading.Lock(), lifecycle=lifecycle)


@pytest.fixture
def poison_rig(tmp_path):
    """A real origin, a healthy anchored memory, and a poison binding inserted
    DIRECTLY into the store — standing in for a row written before the gate."""
    origin = Origin(tmp_path / "remote")
    origin.commit("app.py", app_py(), "seed app")
    origin.push()
    rig = make_rig(tmp_path)
    register_repo(rig.store, REPO, origin.url, canonical_ref="main")

    write_as(
        rig,
        "writer-one",
        HEALTHY_TEXT,
        anchors=[{"repo": REPO, "anchor": ANCHOR}],
    )
    poisoned = write_as(rig, "writer-one", POISON_TEXT)
    rig.store.conn.execute(
        "INSERT OR REPLACE INTO episode_anchors(episode_id, repo, anchor, fp_meta) "
        "VALUES(?,?,?,'')",
        (poisoned, REPO, POISON_ANCHOR),
    )
    require_method(rig.store, "anchor_baseline_put")(
        [(poisoned, REPO, POISON_ANCHOR, origin.sha("HEAD"), 0)]
    )
    return origin, rig, tmp_path


def test_one_unprobeable_binding_does_not_fault_the_repo_leg(poison_rig):
    """One stored row git cannot even be HANDED must not abort the repo's drift leg,
    freezing every other anchor's verdict at its last value."""
    origin, rig, tmp_path = poison_rig
    syncer = make_syncer(rig.store, tmp_path)

    syncer.tick()

    err = meta_value(rig.store, f"sync:{REPO}:last_error")
    assert not err, f"the poison binding failed the leg: {err}"
    env = recall_as(rig, "reader", HEALTHY_TEXT)
    hit = hit_for_id(env, served_ids(env)[0])
    assert hit["drift"]["type"] == "fresh", hit["drift"]


def test_a_poisoned_binding_reads_unverifiable(poison_rig):
    origin, rig, tmp_path = poison_rig
    make_syncer(rig.store, tmp_path).tick()

    env = recall_as(rig, "reader", POISON_TEXT)
    hit = hit_for_id(env, served_ids(env)[0])
    assert hit["drift"]["type"] == "unverifiable", hit["drift"]


def test_the_prunes_still_run_on_a_tick_with_a_poison_binding(poison_rig):
    """The prunes sit AFTER the prober in the same leg — a raise there skips them."""
    origin, rig, tmp_path = poison_rig
    require_method(rig.store, "anchor_baseline_put")(
        [(999_999, REPO, "ghost.py::gone", "c" * 40, 0)]
    )
    make_syncer(rig.store, tmp_path).tick()

    orphans = list(
        rig.store.conn.execute(
            "SELECT 1 FROM anchor_baselines WHERE episode_id=999999",
        )
    )
    assert orphans == [], "anchor_baselines_prune never ran"


def test_the_healthy_sibling_still_moves_on_a_later_commit(poison_rig):
    """(d) no frozen false-fresh: a second tick after the code moves updates the
    healthy binding's verdict."""
    origin, rig, tmp_path = poison_rig
    syncer = make_syncer(rig.store, tmp_path)
    syncer.tick()
    origin.commit("app.py", "def farewell(name):\n    return name\n", "rename greet")
    origin.push()
    syncer.tick()

    env = recall_as(rig, "reader", HEALTHY_TEXT)
    hit = hit_for_id(env, served_ids(env)[0])
    assert hit["drift"]["type"] in QUALIFYING_DRIFT, hit["drift"]


def test_a_real_leg_fault_is_still_reported(poison_rig):
    """The guard is per-binding, not per-leg: a repo the daemon genuinely cannot
    sync still records its fault, and the healthy repo's key stays clean."""
    origin, rig, tmp_path = poison_rig
    register_repo(rig.store, "broken", str(tmp_path / "nope.git"), canonical_ref="main")

    make_syncer(rig.store, tmp_path).tick()

    assert meta_value(rig.store, "sync:broken:last_error"), (
        "an unreachable repo recorded no leg fault"
    )
    assert not meta_value(rig.store, f"sync:{REPO}:last_error")


# ── I5: exactly ONE staleness answer per hit ──────────────────────────────────


@pytest.fixture
def drift_rig(tmp_path):
    """A real origin + a real tick, so the drift verdict on the wire is measured
    rather than seeded."""
    origin = Origin(tmp_path / "remote")
    origin.commit("app.py", app_py(), "seed app")
    origin.push()
    rig = make_rig(tmp_path)
    register_repo(rig.store, REPO, origin.url, canonical_ref="main")
    return origin, rig, tmp_path


def _stale_hit(origin, rig, tmp_path, *, text: str = HEALTHY_TEXT, **write_kw) -> int:
    eid = write_as(
        rig, "writer-one", text, anchors=[{"repo": REPO, "anchor": ANCHOR}], **write_kw
    )
    syncer = make_syncer(rig.store, tmp_path)
    syncer.tick()
    origin.commit("app.py", "def farewell(name):\n    return name\n", "rename greet")
    origin.push()
    syncer.tick()
    return eid


def test_a_served_hit_carries_no_last_verified_key(drift_rig):
    """The ``last_verified`` rider is gone from the wire at EVERY nesting level."""
    origin, rig, tmp_path = drift_rig
    eid = _stale_hit(origin, rig, tmp_path)

    env = recall_as(rig, "reader", HEALTHY_TEXT)

    assert "last_verified" not in keys_anywhere(env), sorted(keys_anywhere(env))
    assert hit_for_id(env, eid)["drift"]["type"] in QUALIFYING_DRIFT


def test_a_stale_hit_carries_remediation_and_a_fresh_one_does_not(drift_rig):
    origin, rig, tmp_path = drift_rig
    eid = _stale_hit(origin, rig, tmp_path)
    stale = hit_for_id(recall_as(rig, "reader", HEALTHY_TEXT), eid)
    assert stale.get("remediation") == REMEDIATION_NOTICE, stale

    fresh_origin = Origin(tmp_path / "remote2")
    fresh_origin.commit("app.py", app_py(), "seed app")
    fresh_origin.push()
    register_repo(rig.store, "beta", fresh_origin.url, canonical_ref="main")
    fresh_id = write_as(
        rig,
        "writer-one",
        "bill() takes the user row, never the id",
        anchors=[{"repo": "beta", "anchor": ANCHOR}],
    )
    make_syncer(rig.store, tmp_path).tick()
    fresh = hit_for_id(
        recall_as(rig, "reader", "bill() takes the user row, never the id"), fresh_id
    )
    assert fresh["drift"]["type"] == "fresh", fresh["drift"]
    assert "remediation" not in fresh, fresh


def test_an_orphaned_verify_row_never_reaches_the_wire(drift_rig):
    """R2: historical ``verify_*`` rows survive in the ledger as honest history and
    are read by NOTHING."""
    origin, rig, tmp_path = drift_rig
    eid = _stale_hit(origin, rig, tmp_path)
    insert_verify_audit(rig.store, eid, "verify_current", ts=999_999, ref="main")

    env = recall_as(rig, "reader", HEALTHY_TEXT)

    assert "last_verified" not in keys_anywhere(env), sorted(keys_anywhere(env))
    hit = hit_for_id(env, eid)
    assert hit["drift"]["type"] in QUALIFYING_DRIFT, hit["drift"]
    assert hit.get("remediation") == REMEDIATION_NOTICE, hit


def test_a_hit_read_off_its_declared_line_carries_no_remediation(drift_rig):
    """BUG-070's rule, now carried STRUCTURALLY: a stale-tier verdict routes to the
    advisory ``branch_scoped`` for an off-line consumer, and ``branch_scoped`` is not
    in ``QUALIFYING_DRIFT`` — so the rider cannot fire."""
    origin, rig, tmp_path = drift_rig
    origin.push("main:refs/heads/feature")
    eid = _stale_hit(origin, rig, tmp_path, repos=[f"{REPO}@feature"])

    env = recall_as(rig, "reader", HEALTHY_TEXT, repos=[f"{REPO}@main"])
    hit = hit_for_id(env, eid)

    assert hit["drift"]["type"] == "branch_scoped", hit["drift"]
    assert "remediation" not in hit, hit
    assert "last_verified" not in keys_anywhere(env)


def test_an_unverifiable_hit_carries_no_remediation(drift_rig):
    origin, rig, tmp_path = drift_rig
    eid = write_as(
        rig,
        "writer-one",
        "prose the tree cannot answer",
        anchors=[{"repo": REPO, "anchor": "no/such/file.py::nope"}],
    )
    make_syncer(rig.store, tmp_path).tick()

    hit = hit_for_id(recall_as(rig, "reader", "prose the tree cannot answer"), eid)
    assert hit["drift"]["type"] == "unverifiable", hit["drift"]
    assert "remediation" not in hit, hit


def test_the_verify_channel_is_gone_from_every_layer():
    """Structural: the producer, its port, its adapter method, its schema and its two
    evidence kinds all leave together — a surviving half is a second oracle waiting."""
    import hive.adapters.store_sqlite as store_sqlite
    import hive.domain.change_evidence as change_evidence
    import hive.domain.evidence_kinds as evidence_kinds
    import hive.domain.ports as ports

    for name in (
        "classify_verify",
        "render_verify_payload",
        "verify_payload_ref",
        "VERIFY_PAYLOAD_SCHEMA",
    ):
        assert not hasattr(change_evidence, name), f"change_evidence.{name} survives"
    assert not hasattr(ports, "LastVerificationReader"), "the port survives"
    assert not hasattr(store_sqlite.SqliteEpisodeStore, "last_verification"), (
        "the store method survives"
    )
    for name in ("EK_VERIFY_CURRENT", "EK_VERIFY_STALE"):
        assert not hasattr(evidence_kinds, name), f"evidence_kinds.{name} survives"
    assert "verify_current" not in evidence_kinds.EVIDENCE_KINDS
    assert "verify_stale" not in evidence_kinds.EVIDENCE_KINDS


# ── I6: the git feed and the census feed's other products are untouched ───────


def test_an_anchored_memory_still_retires_on_git_drift(drift_rig):
    """Clause 1a is the surviving machine-gated retirement path for an anchored
    memory — and it is reachable by a caller with no other relationship to it."""
    origin, rig, tmp_path = drift_rig
    eid = _stale_hit(origin, rig, tmp_path)

    env = prune_as(rig, "second-identity", eid)

    assert env.get("status") == "pruned", env
    assert env.get("signals") == ["drift:anchor_missing"], env


def test_a_general_memory_retires_only_on_other_identity_hurt(drift_rig):
    """R4, stated as a contract: with the pair feed gone, an unanchored memory needs
    a SECOND identity's hurt evidence — its own author's is not enough."""
    origin, rig, tmp_path = drift_rig
    eid = write_as(rig, "writer-one", "a general lesson with no anchors at all")

    payload(
        call(
            rig.server,
            "hive_outcome",
            {"hurt": [eid]},
            identity=ident("writer-one"),
        )
    )
    assert prune_as(rig, "writer-one", eid).get("status") == "noop"

    payload(
        call(
            rig.server,
            "hive_outcome",
            {"hurt": [eid]},
            identity=ident("other-identity"),
        )
    )
    env = prune_as(rig, "writer-one", eid)
    assert env.get("status") == "pruned", env
    assert "outcome_hurt_other_identity" in env.get("signals", []), env


def test_the_established_rung_survives_the_verify_removal(drift_rig):
    """Law 3's top rung is fed by ``outcome_verified_helped`` — a DIFFERENT census
    product from the deleted verify rows."""
    origin, rig, tmp_path = drift_rig
    eid = write_as(
        rig,
        "writer-one",
        HEALTHY_TEXT,
        anchors=[{"repo": REPO, "anchor": ANCHOR}],
        polarity="dont",
    )
    syncer = make_syncer(rig.store, tmp_path, lifecycle=rig.container.lifecycle)
    syncer.tick()  # first connect: baseline only, no receipt
    origin.commit("app.py", app_py('return "hello " + name'), "tweak greet")
    origin.push()
    insert_verify_audit(
        rig.store, eid, EK_OUTCOME_VERIFIED_HELPED, ts=500_000, ref="main"
    )
    syncer.tick()  # a real range ⇒ census ingest ⇒ the established rung runs

    ep = rig.store.get_episode(eid)
    assert ep is not None and ep.trust == "established", ep


def test_blast_radius_changed_survives_the_verify_removal(drift_rig):
    """``stale_suspect`` → ``blast_radius_changed`` is kept, and it is a QUALIFYING
    drift verdict, so clause 1a still reaches a neighbourhood break."""
    origin, rig, tmp_path = drift_rig
    eid = write_as(
        rig, "writer-one", HEALTHY_TEXT, anchors=[{"repo": REPO, "anchor": ANCHOR}]
    )
    rig.store.insert_audit(
        eid,
        EK_STALE_SUSPECT,
        "census",
        500_000,
        json.dumps(
            {
                "schema": "stale_suspect/v1",
                "matched": {"path": "app.py", "symbol": "greet"},
                "seed": "other.py::dep",
                "drift": "breaking",
                "stamp": {"head_sha": "d" * 40},
            }
        ),
    )
    make_syncer(rig.store, tmp_path).tick()

    hit = hit_for_id(recall_as(rig, "reader", HEALTHY_TEXT), eid)
    assert hit["drift"]["type"] == "blast_radius_changed", hit["drift"]
    assert "blast_radius_changed" in QUALIFYING_DRIFT


def test_censusctl_no_longer_reports_the_verify_counters():
    """R5: the operator-facing counters go with the channel; the survivors stay."""
    import inspect

    import hive.tools.censusctl as censusctl
    from hive.domain.change_evidence import IngestReport

    fields = {f for f in IngestReport.__dataclass_fields__}
    assert "verify_current" not in fields and "verify_stale" not in fields, fields
    assert {"verified_helped", "verified_hurt", "stale_suspects"} <= fields, fields
    assert "verify_" not in inspect.getsource(censusctl)


def test_a_gate_feed_fault_noops_rather_than_retires(drift_rig, monkeypatch):
    """FAIL-CLOSED: a broken feed can refuse a retirement, never grant one."""
    origin, rig, tmp_path = drift_rig
    eid = _stale_hit(origin, rig, tmp_path)

    def boom(*_a, **_kw):
        raise RuntimeError("ledger unavailable")

    monkeypatch.setattr(rig.store, "evidence_rows_for", boom)
    env = prune_as(rig, "second-identity", eid)

    assert env.get("status") == "noop", env
    ep = rig.store.get_episode(eid)
    assert ep is not None and ep.trust != "deprecated"


# ── I7: every served hit clears tau_serve ─────────────────────────────────────


class PlanarProvider:
    """A controllable-angle embedder: ``rel:<i>`` texts sit at a FIXED cosine to the
    ``q:`` query direction and are mutually far enough apart that the MMR pass keeps
    them; anything else is orthogonal to the query. Deterministic, exact unit norm."""

    name = "planar"

    def __init__(self, d: int = 64, cos: float = 0.75, loaded: bool = True) -> None:
        self.d = int(d)
        self.cos = float(cos)
        self.loaded = bool(loaded)

    def load(self) -> "PlanarProvider":
        self.loaded = True
        return self

    def _unit(self, v: np.ndarray) -> np.ndarray:
        n = float(np.linalg.norm(v))
        return (v / n).astype(np.float32)

    def _vec(self, text: str) -> np.ndarray:
        v = np.zeros(self.d, dtype=np.float64)
        if text.startswith("q:"):
            v[0] = 1.0
        elif text.startswith("rel:"):
            i = int(text[4:].split()[0])
            v[0] = self.cos
            v[1 + (i % (self.d - 2))] = float(np.sqrt(1.0 - self.cos**2))
        else:
            i = abs(hash(text)) % (self.d // 2)
            v[self.d // 2 + i] = 1.0
        return self._unit(v)

    def encode(self, text: str) -> np.ndarray:
        return self._vec(text)

    def encode_batch(self, texts: Sequence[str]) -> np.ndarray:
        rows = list(texts)
        if not rows:
            return np.zeros((0, self.d), dtype=np.float32)
        return np.stack([self._vec(t) for t in rows], axis=0)


@pytest.fixture
def tau_rig(tmp_path):
    return make_rig(tmp_path, embedder=PlanarProvider(d=64))


def _tau_serve(rig) -> float:
    return float(rig.container.recall.gate.tau_serve)


def _seed(rig, relevant: int, weak: int, *, start: int = 0) -> list[int]:
    ids = [
        write_as(rig, "seed", f"rel:{i} a relevant lesson number {i}")
        for i in range(start, start + relevant)
    ]
    for j in range(weak):
        write_as(rig, "seed", f"weak lesson number {j} about something else entirely")
    rig.container.build_index()
    return ids


def test_every_served_hit_clears_tau_serve(tau_rig):
    """The direct inverse of the measured 8/60: one relevant candidate must not
    authorize serving the padding behind it."""
    _seed(tau_rig, relevant=1, weak=20)

    env = recall_as(tau_rig, "reader", "q: the relevant question")

    assert env.get("abstained") is False, env
    hits = env["reference_context"]
    assert len(hits) == 1, [h["sim"] for h in hits]
    assert all(h["sim"] >= _tau_serve(tau_rig) for h in hits), [h["sim"] for h in hits]


def test_recall_top_n_is_a_cap_not_a_target(tau_rig):
    _seed(tau_rig, relevant=15, weak=5)

    env = recall_as(tau_rig, "reader", "q: the relevant question")

    hits = env["reference_context"]
    cap = int(tau_rig.container.recall.recall_top_n)
    assert len(hits) == cap, len(hits)
    assert all(h["sim"] >= _tau_serve(tau_rig) for h in hits)


def test_a_sub_tau_row_is_never_exposed(tau_rig):
    """Belt-ordering: a row the floor rejects must never have its liveness refreshed
    by the very read that rejected it."""
    _seed(tau_rig, relevant=1, weak=20)

    env = recall_as(tau_rig, "reader", "q: the relevant question")

    assert sorted(exposure_ids(tau_rig, env["trace_id"])) == sorted(served_ids(env))
    assert len(served_ids(env)) == 1, served_ids(env)


def test_a_scoped_recall_also_filters_at_tau(tau_rig):
    register_repo(tau_rig.store, REPO, "https://example.invalid/alpha.git")
    ids = [
        write_as(tau_rig, "seed", f"rel:{i} a scoped relevant lesson {i}", repos=[REPO])
        for i in range(1)
    ]
    for j in range(20):
        write_as(tau_rig, "seed", f"weak scoped lesson {j} elsewhere", repos=[REPO])
    tau_rig.container.build_index()

    env = recall_as(tau_rig, "reader", "q: the relevant question", repos=[REPO])

    assert served_ids(env) == ids, served_ids(env)
    assert all(h["sim"] >= _tau_serve(tau_rig) for h in env["reference_context"])


def test_no_relevant_row_still_abstains(tau_rig):
    """The filter must not touch the abstain decision."""
    _seed(tau_rig, relevant=0, weak=20)

    env = recall_as(tau_rig, "reader", "q: the relevant question")

    assert env.get("abstained") is True, env
    assert env.get("reference_context") == []


def test_all_relevant_rows_unservable_is_empty_not_confident(tau_rig):
    """Law 1's biconditional survives the filter: no CONFIDENT-with-zero-hits state
    becomes reachable when every ≥tau row is unservable."""
    _seed(tau_rig, relevant=2, weak=5)
    tau_rig.clock.advance(60 * DAY_S)  # past the provisional TTL

    env = recall_as(tau_rig, "reader", "q: the relevant question")

    assert env.get("abstained") is True, env
    assert env.get("reference_context") == []
    assert env.get("state") != "CONFIDENT", env


def test_an_encode_fault_is_empty_not_a_sub_tau_serve(tau_rig, monkeypatch):
    _seed(tau_rig, relevant=1, weak=5)

    def boom(_text):
        raise RuntimeError("encoder down")

    monkeypatch.setattr(tau_rig.container.recall.embedder, "encode", boom)
    env = recall_as(tau_rig, "reader", "q: the relevant question")

    assert env.get("abstained") is True, env
    assert env.get("reference_context") == []


# ── I8: advertised == enforced ────────────────────────────────────────────────


def _tool(name: str) -> dict:
    for t in TOOL_DEFINITIONS:
        if t["name"] == name:
            return t
    raise AssertionError(f"tool {name!r} is not advertised")


def test_the_served_contract_advertises_only_reachable_signals():
    assert "contradiction" not in SERVER_INSTRUCTIONS, SERVER_INSTRUCTIONS
    assert "contradiction" not in REMEDIATION_NOTICE, REMEDIATION_NOTICE
    assert len(SERVER_INSTRUCTIONS.encode()) <= METADATA_FIELD_LIMIT


def test_the_tool_descriptions_match_the_gate():
    for name in ("hive_prune", "hive_supersede"):
        desc = _tool(name)["description"]
        assert "contradiction" not in desc, (name, desc)
        assert len(desc.encode()) <= METADATA_FIELD_LIMIT
    assert "near-dup successor" in _tool("hive_supersede")["description"]


def test_the_initialize_instructions_are_the_contract(tmp_path):
    rig = make_rig(tmp_path)
    resp = rig.server.handle(MCPRequest(1, "initialize", {}))
    served = (resp.result or {}).get("instructions", "")
    assert served == SERVER_INSTRUCTIONS
    assert "contradiction" not in served


def test_the_change_introduces_no_new_required_env_var():
    """§9.2: this change adds no boot-required env var — a fresh container boots
    from the same declared set."""
    from hive.app.config import Config

    cfg = Config.load(db_path=":memory:", env={})
    assert cfg.recall.tau_serve > 0.0


def _iter_signal_names() -> Iterable[str]:
    import hive.domain.retirement as retirement

    for name in dir(retirement):
        if name.startswith("SIG_"):
            yield str(getattr(retirement, name))


def test_the_signal_vocabulary_is_exactly_the_four_qualifying_clauses():
    """The audit vocabulary after the removal: drift, verified hurt, other-identity
    hurt, supersede near-dup winner. Nothing else may be stampable."""
    names = set(_iter_signal_names())
    assert names == {
        "drift:",
        "outcome_verified_hurt",
        "outcome_hurt_other_identity",
        "winner_near_dup",
    }, names
