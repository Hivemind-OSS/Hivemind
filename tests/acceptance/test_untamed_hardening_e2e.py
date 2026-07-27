"""The UNTAMED end-to-end suite for the minimal-hardening removal.

Untamed means: written from the **contract** the server actually serves
(``SERVER_INSTRUCTIONS``, the tool descriptions, ``REMEDIATION_NOTICE``) rather than
from the implementation; driving the real MCP surface through
``HiveMCPServer.handle`` with a DISTINCT ``ServerIdentity`` per actor; adversarial by
construction; and capable of failing — every assertion has a way to be false against
a plausible wrong build.

TWO TIERS, because only ONE case needs the 1.2 GB model:

  * U1, U2, U3, U5, U6 run in the ordinary gate (``make check``). They need real git,
    real SQLite, the real container and real near-duplicate geometry — none of which
    needs the shipped embedder.
  * U4 (the served-set relevance distribution) is the one case whose whole subject is
    what the REAL Qwen3 embedder does to a real corpus, so it carries
    ``@pytest.mark.embed`` and is deselected by ``pytest -m "not embed"``. Run it with:

        make check-embed            (or: uv run --extra embed pytest -m embed \\
                                     tests/acceptance/test_untamed_hardening_e2e.py -q)

Store reads appear only to assert the ABSENCE of a side effect (no audit row, no
exposure row) or to establish a precondition the assertion depends on.

FROZEN: no later chunk may edit this file.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from hive.app.config import Config, SyncConfig
from hive.app.contract import REMEDIATION_NOTICE, SERVER_INSTRUCTIONS
from hive.app.container import build_container
from hive.app.mcp_server import MCPRequest, ServerIdentity
from hive.app.sync import SyncService
from hive.domain.change_evidence import ChangeEvidenceService
from hive.domain.evidence_kinds import EK_PRUNE
from tests.acceptance.conftest import HIT_QUERIES, MISS_QUERIES, build_acc, seed_corpus
from tests.fakes._fakes import FakeClock, FakeClusterProvider
from tests.sync.conftest import Origin

REPO = "alpha"
ANCHOR = "app.py::greet"


# ── the real surface, driven the way a client drives it ───────────────────────


def mcp(server, name: str, args: dict, *, agent: str) -> dict:
    resp = server.handle(
        MCPRequest(1, "tools/call", {"name": name, "arguments": args}),
        identity=ServerIdentity("untamed", agent),
    )
    if resp.error is not None:
        return {"_error": resp.error}
    text = ((resp.result or {}).get("content") or [{}])[0].get("text", "")
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return {"_raw": text}
    return parsed if isinstance(parsed, dict) else {"_raw": text}


def instructions_of(server) -> str:
    return (server.handle(MCPRequest(1, "initialize", {})).result or {}).get(
        "instructions", ""
    )


def build_rig(tmp_path, *, embedder, t0: int = 1_000_000):
    clock = FakeClock(t0)
    cfg = Config.load(db_path=str(tmp_path / "hive.db"), env={})
    container = build_container(
        cfg, tenant_id="untamed", agent_id="untamed", embedder=embedder, clock=clock
    )
    container.migrate()
    container.build_index()
    container.warm_embedder()
    return container, container.make_server(), clock


def syncer_for(store, tmp_path: Path) -> SyncService:
    cfg = SyncConfig(mirror_dir=str(tmp_path / "mirrors"))
    evidence = ChangeEvidenceService(
        reader=store, appender=store, now=lambda: 424_242, ranges=store
    )
    return SyncService(cfg, store, evidence, threading.Lock())


def app_py(fn: str = "greet") -> str:
    return f'def {fn}(name):\n    return "hi " + name\n\n\ndef bill(u):\n    return u\n'


def keys_anywhere(obj) -> set[str]:
    found: set[str] = set()
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            found |= {str(k) for k in cur}
            stack.extend(cur.values())
        elif isinstance(cur, (list, tuple)):
            stack.extend(cur)
    return found


def hit_of(env: dict, eid: int) -> dict:
    for h in env.get("reference_context", []):
        if h.get("episode_id") == eid:
            return h
    raise AssertionError(f"episode {eid} not served: {env}")


# ── U1 — the cold-stranger hive_prune ─────────────────────────────────────────

CONVENTION = (
    "cid=3 every outbound http client must carry an explicit connect and read timeout"
)
PARAPHRASE = (
    "cid=3 outbound http clients always need explicit connect and read timeouts set"
)


def test_u1_a_cold_stranger_cannot_prune_a_near_duplicated_memory(tmp_path):
    """`writer-one` states a real convention; `writer-two` writes a genuine
    paraphrase; `attacker` — which has issued no prior call of any kind — tries to
    destroy the first. The contract promises a benign no-op, and then promises the
    LEGITIMATE path still works, which is what stops this from asserting "nothing
    works"."""
    container, server, _clock = build_rig(tmp_path, embedder=FakeClusterProvider(d=64))

    target = mcp(server, "hive_write", {"text": CONVENTION}, agent="writer-one")["id"]
    para = mcp(server, "hive_write", {"text": PARAPHRASE}, agent="writer-two")["id"]

    # precondition: they REALLY are a near-dup under the deployed floor
    import numpy as np

    a, b = container.store.get_episode(target), container.store.get_episode(para)
    cos = float(np.dot(a.value, b.value))
    assert cos >= float(server.conflict.tau), f"cos={cos:.4f} is not a near-dup"

    env = mcp(server, "hive_prune", {"episode_id": target}, agent="attacker")
    assert env["status"] == "noop", env
    assert "no qualifying machine signal" in env["reason"], env

    # trust and status unchanged on a re-read; no prune row was ever written
    still = container.store.get_episode(target)
    assert still.trust == a.trust and still.status == a.status
    assert (
        list(
            container.store.conn.execute(
                "SELECT 1 FROM evidence_events WHERE episode_id=? AND kind=?",
                (target, EK_PRUNE),
            )
        )
        == []
    )

    # the same attack wearing an opposing polarity (a detected CONTRADICTION)
    opposing = mcp(
        server,
        "hive_write",
        {"text": PARAPHRASE + " — and never omit them", "polarity": "dont"},
        agent="attacker",
    )["id"]
    assert opposing
    assert (
        mcp(server, "hive_prune", {"episode_id": target}, agent="attacker")["status"]
        == "noop"
    )

    # …and the legitimate path the contract points at still retires the loser
    legit = mcp(
        server,
        "hive_supersede",
        {"loser": target, "winner": para},
        agent="writer-one",
    )
    assert legit["status"] == "superseded", legit
    assert legit["signals"] == ["winner_near_dup"], legit


# ── U2 — the poison anchor ────────────────────────────────────────────────────

POISON = "poison.py\x00::boom"
HEALTHY = "greet() must stay single-arg; callers pass positionally"
POISONED_TEXT = "cid=5 the memory whose binding predates the anchor grammar gate"


def test_u2_a_poison_anchor_cannot_kill_a_repo_leg(tmp_path):
    """A control-character anchor is refused at the door; a binding that predates the
    door still cannot fault the repo's drift leg, and every OTHER anchor in that repo
    still earns a real verdict."""
    origin = Origin(tmp_path / "remote")
    origin.commit("app.py", app_py(), "seed app")
    origin.push()
    container, server, _clock = build_rig(tmp_path, embedder=FakeClusterProvider(d=64))
    container.store.repo_add(
        name=REPO, url=origin.url, canonical_ref="main", token_env="", added_ts=0
    )

    refused = mcp(
        server,
        "hive_write",
        {"text": "poisoned", "anchors": [{"repo": REPO, "anchor": POISON}]},
        agent="writer-one",
    )
    assert refused.get("status") not in ("approved", "redacted"), refused
    assert "control character" in json.dumps(refused), refused
    assert (
        list(
            container.store.conn.execute(
                "SELECT 1 FROM episode_anchors WHERE anchor=?", (POISON,)
            )
        )
        == []
    )

    healthy = mcp(
        server,
        "hive_write",
        {"text": HEALTHY, "anchors": [{"repo": REPO, "anchor": ANCHOR}]},
        agent="writer-one",
    )["id"]
    legacy = mcp(server, "hive_write", {"text": POISONED_TEXT}, agent="writer-one")[
        "id"
    ]
    # PRECONDITION, not an assertion: a row written before the gate existed.
    container.store.conn.execute(
        "INSERT OR REPLACE INTO episode_anchors(episode_id, repo, anchor, fp_meta) "
        "VALUES(?,?,?,'')",
        (legacy, REPO, POISON),
    )
    container.store.anchor_baseline_put([(legacy, REPO, POISON, origin.sha("HEAD"), 0)])

    syncer = syncer_for(container.store, tmp_path)
    syncer.tick()
    origin.commit("app.py", app_py("farewell"), "rename greet")
    origin.push()
    syncer.tick()

    err = container.store.conn.execute(
        "SELECT value FROM meta WHERE key=?", (f"sync:{REPO}:last_error",)
    ).fetchone()
    assert err is None, f"the poison binding failed the leg: {err[0] if err else ''}"

    healthy_hit = hit_of(
        mcp(server, "hive_recall", {"query": HEALTHY}, agent="r"), healthy
    )
    assert healthy_hit["drift"]["type"] == "anchor_missing", healthy_hit["drift"]
    commits = (healthy_hit["drift"].get("detail") or {}).get("per_anchor") or [{}]
    assert commits[0].get("verdict") == "anchor_missing", commits

    poisoned_hit = hit_of(
        mcp(server, "hive_recall", {"query": POISONED_TEXT}, agent="r"), legacy
    )
    assert poisoned_hit["drift"]["type"] == "unverifiable", poisoned_hit["drift"]

    # and the healthy memory is now retirable by a SECOND identity, on drift alone
    retired = mcp(
        server, "hive_prune", {"episode_id": healthy}, agent="second-identity"
    )
    assert retired["status"] == "pruned", retired
    assert retired["signals"] == ["drift:anchor_missing"], retired


# ── U3 — a hit carrying both staleness channels ───────────────────────────────


def test_u3_a_hit_carries_exactly_one_staleness_channel(tmp_path):
    """The BUG-086 state is MANUFACTURED — a ``verify_current`` row is written for a
    memory whose git verdict says the anchor moved — so the assertion cannot pass
    vacuously."""
    origin = Origin(tmp_path / "remote")
    origin.commit("app.py", app_py(), "seed app")
    origin.push()
    container, server, _clock = build_rig(tmp_path, embedder=FakeClusterProvider(d=64))
    container.store.repo_add(
        name=REPO, url=origin.url, canonical_ref="main", token_env="", added_ts=0
    )
    stale = mcp(
        server,
        "hive_write",
        {"text": HEALTHY, "anchors": [{"repo": REPO, "anchor": ANCHOR}]},
        agent="writer-one",
    )["id"]
    fresh_text = "cid=8 bill() takes the user row, never the bare id"
    fresh = mcp(
        server,
        "hive_write",
        {"text": fresh_text, "anchors": [{"repo": REPO, "anchor": "app.py::bill"}]},
        agent="writer-one",
    )["id"]

    syncer = syncer_for(container.store, tmp_path)
    syncer.tick()
    origin.commit("app.py", app_py("farewell"), "rename greet")
    origin.push()
    syncer.tick()

    container.store.insert_audit(
        stale,
        "verify_current",
        "census",
        999_999,
        json.dumps(
            {"schema": "verify/v1", "stamp": {"head_sha": "a" * 40}, "ref": "main"}
        ),
    )

    env = mcp(server, "hive_recall", {"query": HEALTHY}, agent="reader")
    assert "last_verified" not in keys_anywhere(env), sorted(keys_anywhere(env))
    hit = hit_of(env, stale)
    assert hit["drift"]["type"] == "anchor_missing", hit["drift"]
    assert hit["remediation"] == REMEDIATION_NOTICE, hit

    fresh_env = mcp(server, "hive_recall", {"query": fresh_text}, agent="reader")
    fresh_hit = hit_of(fresh_env, fresh)
    assert fresh_hit["drift"]["type"] == "fresh", fresh_hit["drift"]
    assert "remediation" not in fresh_hit, fresh_hit
    assert "last_verified" not in keys_anywhere(fresh_env)


def test_u3b_a_memory_read_off_its_declared_line_carries_no_remediation(tmp_path):
    """The rule the deleted ``own_refs`` machinery used to enforce by hand, now
    structural: a stale-tier verdict routes to ``branch_scoped`` off-line, and
    ``branch_scoped`` is not in the qualifying tier the rider reads."""
    origin = Origin(tmp_path / "remote")
    origin.commit("app.py", app_py(), "seed app")
    origin.push()
    origin.push("main:refs/heads/feature")
    container, server, _clock = build_rig(tmp_path, embedder=FakeClusterProvider(d=64))
    container.store.repo_add(
        name=REPO, url=origin.url, canonical_ref="main", token_env="", added_ts=0
    )
    eid = mcp(
        server,
        "hive_write",
        {
            "text": HEALTHY,
            "anchors": [{"repo": REPO, "anchor": ANCHOR}],
            "repos": [f"{REPO}@feature"],
        },
        agent="writer-one",
    )["id"]
    syncer = syncer_for(container.store, tmp_path)
    syncer.tick()
    origin.commit("app.py", app_py("farewell"), "rename greet")
    origin.push()
    origin.push("main:refs/heads/feature", force=True)
    syncer.tick()

    env = mcp(
        server,
        "hive_recall",
        {"query": HEALTHY, "repos": [f"{REPO}@main"]},
        agent="reader",
    )
    hit = hit_of(env, eid)
    assert hit["drift"]["type"] == "branch_scoped", hit["drift"]
    assert "remediation" not in hit, hit
    assert "last_verified" not in keys_anywhere(env)


# ── U5 — the census feed still works after the amputation ─────────────────────


def test_u5_the_census_feed_survives_the_verify_removal(tmp_path):
    """The regression the removal most plausibly breaks: a real receipt over a real
    range must still land ``change_outcome`` rows and must write NO verify row."""
    origin = Origin(tmp_path / "remote")
    origin.commit("app.py", app_py(), "seed app")
    origin.push()
    container, server, _clock = build_rig(tmp_path, embedder=FakeClusterProvider(d=64))
    container.store.repo_add(
        name=REPO, url=origin.url, canonical_ref="main", token_env="", added_ts=0
    )
    eid = mcp(
        server,
        "hive_write",
        {
            "text": HEALTHY,
            "anchors": [{"repo": REPO, "anchor": ANCHOR}],
            "polarity": "dont",
        },
        agent="writer-one",
    )["id"]

    syncer = syncer_for(container.store, tmp_path)
    syncer.tick()
    origin.commit("app.py", app_py("farewell"), "rename greet")
    origin.push()
    syncer.tick()

    kinds = [
        r["kind"]
        for r in container.store.conn.execute(
            "SELECT kind FROM evidence_events WHERE episode_id=?", (eid,)
        )
    ]
    assert "change_outcome" in kinds, kinds
    assert not any(k.startswith("verify_") for k in kinds), kinds


# ── U6 — the whole loop, using only what the contract says ────────────────────


def test_u6_the_advertised_loop_is_reachable_from_the_contract_alone(tmp_path):
    """A fresh identity reads only ``initialize.instructions`` and performs the flow
    it describes: recall -> write with an anchor -> the code changes -> recall again ->
    observe drift -> retire per BAD_VS_STALE. Every envelope it gets back must name
    only fields and signals the contract mentions."""
    origin = Origin(tmp_path / "remote")
    origin.commit("app.py", app_py(), "seed app")
    origin.push()
    container, server, _clock = build_rig(tmp_path, embedder=FakeClusterProvider(d=64))
    container.store.repo_add(
        name=REPO, url=origin.url, canonical_ref="main", token_env="", added_ts=0
    )

    served = instructions_of(server)
    assert served == SERVER_INSTRUCTIONS
    for advertised in ("hive_recall", "hive_write", "hive_prune", "hive_supersede"):
        assert advertised in served
    assert "drift" in served
    assert "contradiction" not in served, served

    # 1. RECALL FIRST — an empty store abstains, and the contract says proceed
    first = mcp(server, "hive_recall", {"query": HEALTHY}, agent="newcomer")
    assert first["abstained"] is True and first["reference_context"] == []

    # 2. STORE with WHAT + WHERE
    eid = mcp(
        server,
        "hive_write",
        {"text": HEALTHY, "anchors": [{"repo": REPO, "anchor": ANCHOR}]},
        agent="newcomer",
    )["id"]

    syncer = syncer_for(container.store, tmp_path)
    syncer.tick()

    # 3. the code moves
    origin.commit("app.py", app_py("farewell"), "rename greet")
    origin.push()
    syncer.tick()

    # 4. recall again — the hit carries ONE staleness answer, named in the contract
    env = mcp(server, "hive_recall", {"query": HEALTHY}, agent="newcomer")
    hit = hit_of(env, eid)
    assert set(hit) <= {
        "episode_id",
        "text",
        "sim",
        "trust",
        "ts",
        "polarity",
        "kind",
        "repos",
        "anchors",
        "meta",
        "drift",
        "remediation",
    }, sorted(hit)
    assert hit["drift"]["type"] == "anchor_missing"
    assert hit["remediation"] == REMEDIATION_NOTICE

    # 5. retire per BAD_VS_STALE — the rider tells the agent exactly this
    other = mcp(server, "hive_prune", {"episode_id": eid}, agent="second-identity")
    assert other["status"] == "pruned", other
    for signal in other["signals"]:
        assert signal.split(":")[0] in {
            "drift",
            "outcome_verified_hurt",
            "outcome_hurt_other_identity",
            "winner_near_dup",
        }, signal


# ── U4 — the served-set relevance distribution (REAL embedder) ────────────────

OFF_DISTRIBUTION = tuple(
    f"An unrelated note number {i} about medieval falconry and regional cheese."
    for i in range(20)
)


@pytest.mark.embed
def test_u4_the_served_set_is_the_tau_serve_prefix(tmp_path, embedder_v1):
    """The measured defect was 8 of 60 served hits clearing ``tau_serve``. This is its
    direct inverse, over the real embedder and a real corpus padded with 20
    deliberately off-distribution rows."""
    container = build_acc(embedder_v1, db_path=str(tmp_path / "hive.db"))
    seed_corpus(container)
    seed_corpus(container, OFF_DISTRIBUTION)
    server = container.make_server()
    tau = float(container.recall.gate.tau_serve)
    cap = int(container.recall.recall_top_n)

    saw_short = False
    for query, _gold in HIT_QUERIES:
        env = mcp(server, "hive_recall", {"query": query}, agent="probe")
        hits = env["reference_context"]
        if env["abstained"]:
            continue
        assert hits, env
        assert all(h["sim"] >= tau for h in hits), (query, [h["sim"] for h in hits])
        assert len(hits) <= cap, (query, len(hits))
        saw_short = saw_short or len(hits) < cap
        exposed = {
            int(r["episode_id"])
            for r in container.store.conn.execute(
                "SELECT episode_id FROM exposure WHERE trace_id=?", (env["trace_id"],)
            )
        }
        assert exposed == {h["episode_id"] for h in hits}, (query, exposed)

    assert saw_short, "no query returned fewer than the cap — the filter never bit"

    for query in MISS_QUERIES:
        env = mcp(server, "hive_recall", {"query": query}, agent="probe")
        assert env["abstained"] is True, (query, env)
        assert env["reference_context"] == []
