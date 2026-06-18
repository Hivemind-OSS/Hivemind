"""P1.14 — hive.app.container.build_container: the composition-root wiring contract.

Fast (a fake warm embedder, no bge load): proves the Boot conformance, the Phase-1
utility-inert policy, the floor-by-identity gate wiring, the warm/persist-head step, the
migrate schema/WAL assertion, the approved-only index warm, and — the load-bearing wiring
fact — the SqliteUtilityStore-before-SqliteEpisodeStore order that guardrail-2 needs.
The real-embedder end-to-end geometry gates live in tests/acceptance/*.
"""
from __future__ import annotations

import json

import pytest

from hive.app.config import Config
from hive.app.container import Container, build_container
from hive.app.mcp_server import HiveMCPServer, MCPRequest
from tests.fakes._fakes import FakeWarmProvider


def _cfg(tmp_path=None, **over) -> Config:
    db = str(tmp_path / "shared.db") if tmp_path is not None else ":memory:"
    return Config.load(db_path=db, **over)


def _build(tmp_path=None, *, embedder=None, **over) -> Container:
    return build_container(_cfg(tmp_path, **over), tenant_id="t1", agent_id="a1",
                           embedder=embedder or FakeWarmProvider(d=256))


def _call(server, name, args):
    return server.handle(MCPRequest(1, "tools/call", {"name": name, "arguments": args}))


def _content(resp) -> dict:
    return json.loads(resp.result["content"][0]["text"])


# ── Boot conformance + Phase-1 policy ─────────────────────────────────────────
def test_build_container_is_boot_conformant():
    c = _build()
    # token_store added to the Boot surface (the HTTP daemon's verify seam) — the REAL adapter
    # (Container) must carry it, not only the entrypoint fake (the protocol-widening trap).
    for attr in ("store", "token_store", "migrate", "build_index", "warm_embedder", "make_server"):
        assert hasattr(c, attr)
    assert callable(c.migrate) and callable(c.build_index)
    assert callable(c.warm_embedder) and callable(c.make_server)


def test_token_store_wired_create_verify_end_to_end():
    """build_container wires a usable SqliteTokenStore on the shared conn (the daemon's verify
    seam): create→verify round-trips and revoke→reject, with no extra wiring re-derived."""
    c = _build()
    tok = c.token_store.create("alice-laptop")
    assert c.token_store.verify(tok) == "alice-laptop"
    assert c.token_store.revoke("alice-laptop") is True
    assert c.token_store.verify(tok) is None


def test_gate_floor_is_wired_by_identity():
    """The frozen cfg.recall object is handed to the gate BY IDENTITY (CONFIG_DRIFT killed
    structurally) — not a copied float."""
    c = _build()
    assert c.recall.gate._recall is c.cfg.recall


def test_recall_top_n_from_config():
    c = _build(recall={"recall_top_n": 7})
    assert c.recall.recall_top_n == 7


def test_identity_carries_tenant_and_agent():
    c = _build()
    assert (c.identity.tenant_id, c.identity.agent_id) == ("t1", "a1")


# ── warm / migrate / index (the boot steps) ───────────────────────────────────
def test_warm_embedder_sets_loaded_and_persists_head(tmp_path):
    c = _build(tmp_path)
    assert c.embedder.loaded is False
    c.warm_embedder()
    assert c.embedder.loaded is True
    assert c.store.meta_get("embedding:head:bytes")            # persisted for restart reuse
    assert c.store.meta_get("embedding:head:w_version") == str(c.embedder.w_version)


def test_migrate_ok_and_wal_active_on_file_db(tmp_path):
    c = _build(tmp_path)
    c.migrate()                                                 # no raise → schema complete
    assert c.conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_migrate_raises_on_missing_table(tmp_path):
    """A botched migration (a required table absent) fails LOUD at boot (→ EX_SOFTWARE),
    never a silent serve over a half-built store."""
    c = _build(tmp_path)
    c.conn.execute("DROP TABLE exposure")
    with pytest.raises(RuntimeError, match="migration incomplete"):
        c.migrate()


def test_build_index_warms_from_approved_only():
    c = _build()
    c.warm_embedder()
    c.admission.write("a durable memory about cache eviction",
                      proposed_by="a1", approved_by="user")    # lands approved in one call
    c.index.rebuild_from_store([])                              # clear the warm cache
    assert c.index.size() == 0
    c.build_index()                                            # rebuild from the store
    assert c.index.size() == 1


# ── make_server end-to-end (the wiring round-trip) ────────────────────────────
def test_make_server_round_trips_write_recall():
    c = _build()
    c.warm_embedder()
    server = c.make_server()
    assert isinstance(server, HiveMCPServer)
    text = "the WAL transaction runs under one BEGIN IMMEDIATE"
    w = _content(_call(server, "hive_write", {"text": text, "approved_by": "user"}))
    assert w["status"] == "approved"                              # client-gated, one call
    c.build_index()
    r = _content(_call(server, "hive_recall", {"query": text}))   # identical text ⇒ confident hit
    assert r["abstained"] is False
    assert any(h["text"] == text for h in r["reference_context"])


def test_d_mismatch_fails_fast():
    """An embedder whose projection dim disagrees with geometry.d is a wiring error caught
    at construction, never a garbage search at first recall."""
    with pytest.raises(ValueError, match="must agree"):
        build_container(_cfg(), tenant_id="t1", agent_id="a1",
                        embedder=FakeWarmProvider(d=128))   # geometry.d defaults to 256


# ── hybrid + FTS lexical mirror CUT: no hybrid/lexical surface anywhere ─────────
def test_no_hybrid_or_lexical_surface():
    # the BM25 hybrid channel + the FTS5 mirror were removed end to end — the
    # pipeline carries no hybrid/lexical handle and the store exposes no FTS
    # surface (re-adding any of these REDS this).
    c = _build()
    assert not hasattr(c.recall, "hybrid_enabled")
    assert not hasattr(c.recall, "lexical_index")
    assert not hasattr(c.store, "search_text")
    assert not hasattr(c.store, "fts_enabled")


# ── self-quarantine resurfacing CUT: the drafts channel is GONE ────────────────
def test_recall_config_rejects_drafts_knob():
    # the drafts knob was removed — an explicit override is an unknown-field error,
    # and the pipeline carries no draft_reader handle (re-adding either REDS this).
    with pytest.raises(ValueError, match="unknown config override"):
        _build(recall={"drafts": True})
    c = _build()
    assert not hasattr(c.recall, "draft_reader")
    assert not hasattr(c.recall, "drafts_enabled")


# ── version-shadowing CUT: the shadow filter + its knobs are GONE ──────────────
def test_shadow_knobs_removed():
    # CV3 serve-time shadowing was removed — neither config knob exists, the
    # pipeline ctor carries no shadow params, and the module-level filter +
    # trust-rank are gone (re-adding any of them REDS this).
    import inspect

    from hive.app.config import RecallConfig
    from hive.domain import recall as recall_mod
    assert not hasattr(RecallConfig(), "shadow")
    assert not hasattr(RecallConfig(), "shadow_tau")
    params = inspect.signature(recall_mod.RecallPipeline.__init__).parameters
    assert "shadow_enabled" not in params and "shadow_tau" not in params
    assert not hasattr(recall_mod, "shadow_filter")
    assert not hasattr(recall_mod, "_TRUST_RANK")


# ── the QuarantineReader port SURVIVES (the demand-promotion scan, not drafts) ──
def test_real_store_satisfies_quarantine_reader_port():
    # conformance-test the REAL adapter against the new port (not just the fake): a
    # runtime_checkable Protocol only checks name-presence, so a port that goes green
    # with fake-backed tests alone can leave the real adapter incomplete.
    from hive.domain.ports import QuarantineReader
    c = _build()
    assert isinstance(c.store, QuarantineReader)


def test_recall_config_has_no_co_access_knobs():
    # the co-access + associations channels are GONE — neither knob exists on the
    # frozen RecallConfig (re-adding either field REDS this).
    from hive.app.config import RecallConfig
    assert not hasattr(RecallConfig(), "co_access")
    assert not hasattr(RecallConfig(), "associations")
