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


# ── hybrid recall wiring (lexical channel = the store, behind recall.hybrid) ──
def test_hybrid_off_wires_no_lexical_index():
    c = _build()
    assert c.recall.lexical_index is None
    assert c.recall.hybrid_enabled is False


def test_hybrid_wires_lexical_index_to_store():
    # the store IS the LexicalIndex adapter (same idiom as reader=store/ledger=store)
    c = _build(recall={"hybrid": True})
    assert c.recall.lexical_index is c.store
    assert c.recall.hybrid_enabled is True


# ── self-quarantine resurfacing wiring (draft channel = the store, behind recall.drafts) ──
def test_drafts_off_wires_no_draft_reader():
    c = _build()
    assert c.recall.draft_reader is None
    assert c.recall.drafts_enabled is False


def test_drafts_wires_draft_reader_to_store():
    # the store IS the QuarantineReader adapter (same reader=store/ledger=store idiom);
    # the TTL is sourced from the autonomy knob so a draft never outlives quarantine.
    c = _build(recall={"drafts": True})
    assert c.recall.draft_reader is c.store
    assert c.recall.drafts_enabled is True
    assert c.recall.draft_tau == 0.6
    assert c.recall.quarantine_ttl_s == c.cfg.autonomy.quarantine_ttl_days * 86_400


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


def test_hybrid_requires_fts5(monkeypatch):
    """hybrid=true on a stripped SQLite (no FTS5) fails FAST at boot; the same
    stripped build with hybrid off boots fine (silent degrade)."""
    import hive.app.container as container_mod
    from hive.adapters.store_sqlite import SqliteEpisodeStore

    class _NoFtsStore(SqliteEpisodeStore):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.fts_enabled = False

    monkeypatch.setattr(container_mod, "SqliteEpisodeStore", _NoFtsStore)
    with pytest.raises(RuntimeError, match="FTS5"):
        build_container(_cfg(recall={"hybrid": True}), tenant_id="t1", agent_id="a1",
                        embedder=FakeWarmProvider(d=256))
    c = build_container(_cfg(), tenant_id="t1", agent_id="a1",
                        embedder=FakeWarmProvider(d=256))
    assert c.recall.lexical_index is None


def test_build_index_self_heals_fts():
    """build_index() rebuilds the FTS mirror after the dense rebuild — drift
    introduced behind the store's back is healed at boot."""
    c = _build(recall={"hybrid": True})
    c.warm_embedder()
    c.admission.write("a memory about fts healing", proposed_by="a1",
                      approved_by="user")
    c.conn.execute("DELETE FROM episodes_fts")            # simulate mirror drift
    assert c.store.search_text("healing", 5) == []
    c.build_index()
    assert [eid for eid, _sc in c.store.search_text("healing", 5)]
