"""P1.14 — hive.app.container.build_container: the composition-root wiring contract.

Fast (a fake warm embedder, no model load): proves the Boot conformance, the Phase-1
utility-inert policy, the floor-by-identity gate wiring, the warm step, the
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
    return build_container(
        _cfg(tmp_path, **over),
        tenant_id="t1",
        agent_id="a1",
        embedder=embedder or FakeWarmProvider(d=768),
    )


def _call(server, name, args):
    return server.handle(MCPRequest(1, "tools/call", {"name": name, "arguments": args}))


def _content(resp) -> dict:
    return json.loads(resp.result["content"][0]["text"])


# ── Boot conformance + Phase-1 policy ─────────────────────────────────────────
def test_build_container_is_boot_conformant():
    c = _build()
    # token_store added to the Boot surface (the HTTP daemon's verify seam) — the REAL adapter
    # (Container) must carry it, not only the entrypoint fake (the protocol-widening trap).
    for attr in (
        "store",
        "token_store",
        "migrate",
        "build_index",
        "warm_embedder",
        "make_server",
    ):
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


def test_verified_promotion_wiring_follows_the_flag():
    # default ON (graduated on the L4 powered gate, §9.9): the store itself is the
    # verified-wins reader. Explicit opt-OUT ⇒ no handle ⇒ the rung is unreachable (byte-inert).
    c = _build()
    assert c.lifecycle._verified_reader is c.store
    off = _build(autonomy={"verified_promotion": False})
    assert off.lifecycle._verified_reader is None


def test_gate_floor_is_wired_by_identity():
    """The frozen cfg.recall object is handed to the gate BY IDENTITY (CONFIG_DRIFT killed
    structurally) — not a copied float."""
    c = _build()
    assert c.recall.gate._recall is c.cfg.recall


def test_recall_top_n_from_config():
    c = _build(recall={"recall_top_n": 7})
    assert c.recall.recall_top_n == 7


def test_select_served_wired_from_config():
    # decorrelated selection is UNCONDITIONAL (no `select` knob); the near-dup floor is
    # single-owned by ConflictConfig.tau (dup_tau); the recall conflict carrier is gated by
    # conflict.enabled. The overscan pool size is the one selection knob.
    c = _build()
    assert not hasattr(c.recall, "select")  # the select toggle is gone
    assert c.recall.overscan == c.cfg.recall.overscan
    assert c.recall.dup_tau == c.cfg.conflict.tau
    assert c.recall.conflict_enabled == c.cfg.conflict.enabled
    assert c.recall.conflict_top_n == c.cfg.conflict.top_n
    # naming the removed select knob fails fast as an unknown override field
    with pytest.raises(ValueError, match=r"select|unknown config override"):
        _build(recall={"select": False})


def test_conflict_config_has_no_suppress_knob():
    # the serve-time suppress switch was folded into the default-ON select_served stage —
    # ConflictConfig no longer carries it (re-adding the field REDS this).
    from hive.app.config import ConflictConfig

    assert not hasattr(ConflictConfig(), "suppress")


def test_identity_carries_tenant_and_agent():
    c = _build()
    assert (c.identity.tenant_id, c.identity.agent_id) == ("t1", "a1")


# ── the AGI sentinel + global canonical_ref wiring are GONE (v3, U4-THIN-AGENT) ──
def test_agi_wiring_is_gone():
    # no cfg group, no server attribute, and an explicit override fails fast as an
    # unknown group — re-adding any of the three REDS this.
    c = _build()
    assert not hasattr(c.cfg, "agi")
    assert not hasattr(c.make_server(), "agi_mode")
    with pytest.raises(ValueError, match="unknown config group"):
        _build(agi={"mode": True})


def test_no_global_canonical_ref_threads_to_the_server():
    # per-repo canonical refs live in the repo registry rows; the container passes no
    # global one, so the server's last_verified rider keeps its unscoped default ("").
    c = _build()
    assert not hasattr(c.cfg, "census")
    assert c.make_server().canonical_ref == ""


# ── secret floor threads cfg → scanner + make_server (default ON; OFF only when opted out) ──
def test_secret_floor_on_by_default():
    from hive.domain.secret_scan import REFUSE

    c = _build()
    assert c.cfg.secret_scan.enabled is True
    assert c.make_server().secret_scan_enabled is True
    # the wired scanner actually refuses a credential — the default floor is live, not nominal
    assert c.scanner.scan("AKIAIOSFODNN7EXAMPLE").action == REFUSE


def test_build_container_threads_secret_scan_disabled():
    from hive.domain.secret_scan import CLEAN

    c = _build(secret_scan={"enabled": False})
    assert c.make_server().secret_scan_enabled is False
    # the one flag flows to the wired scanner: a credential scans CLEAN (floor bypassed)
    assert c.scanner.scan("AKIAIOSFODNN7EXAMPLE").action == CLEAN


# ── ephemeral-store boot WARN (the :memory: posture loses memory on restart — never silent) ──
def test_build_container_warns_on_ephemeral_store(caplog):
    """A store left at the :memory: default loses all captured memory on restart. Like the
    disabled secret floor, this loss-prone posture is surfaced loudly at boot (a WARN)."""
    import logging

    with caplog.at_level(logging.WARNING, logger="hive.container"):
        _build()  # default cfg db = ":memory:"
    assert any("store_ephemeral" in r.getMessage() for r in caplog.records), (
        "an in-memory store must warn loudly at boot (memory is lost on restart)"
    )


def test_build_container_no_ephemeral_warn_on_persistent_store(tmp_path, caplog):
    """A persistent db_path (a file in the volume) is the intended posture — no WARN fires."""
    import logging

    with caplog.at_level(logging.WARNING, logger="hive.container"):
        _build(tmp_path)  # _cfg(tmp_path) → a real file db
    assert not any("store_ephemeral" in r.getMessage() for r in caplog.records)


# ── warm / migrate / index (the boot steps) ───────────────────────────────────
def test_warm_embedder_sets_loaded(tmp_path):
    c = _build(tmp_path)
    assert c.embedder.loaded is False
    c.warm_embedder()
    assert c.embedder.loaded is True


def test_migrate_ok_and_wal_active_on_file_db(tmp_path):
    c = _build(tmp_path)
    c.migrate()  # no raise → schema complete
    assert c.conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_migrate_raises_on_missing_table(tmp_path):
    """A botched migration (a required table absent) fails LOUD at boot (→ EX_SOFTWARE),
    never a silent serve over a half-built store."""
    c = _build(tmp_path)
    c.conn.execute("DROP TABLE exposure")
    with pytest.raises(RuntimeError, match="migration incomplete"):
        c.migrate()


def test_migrate_raises_on_missing_ingested_ranges_table(tmp_path):
    """The census range ledger is boot-checked like every required table — a store
    missing it would silently re-ingest every duplicate range."""
    c = _build(tmp_path)
    c.conn.execute("DROP TABLE ingested_ranges")
    with pytest.raises(RuntimeError, match="migration incomplete"):
        c.migrate()


@pytest.mark.parametrize(
    "table", ["episode_anchors", "repos", "anchor_drift", "ref_requests"]
)
def test_migrate_asserts_the_v3_partition_tables(tmp_path, table):
    """The v3 repo-partition set (anchor bindings, repo registry, drift cache,
    branch-demand list) is boot-asserted like every required table — a store missing
    one would limp to a cryptic mid-query crash at first recall/sync instead."""
    c = _build(tmp_path)
    c.conn.execute(f"DROP TABLE {table}")
    with pytest.raises(RuntimeError, match="migration incomplete"):
        c.migrate()


def test_migrate_passes_with_the_full_v3_table_set(tmp_path):
    # the positive twin: a fresh store carries every asserted table, migrate is clean.
    c = _build(tmp_path)
    c.migrate()  # no raise
    present = {
        r["name"]
        for r in c.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    for required in ("episode_anchors", "repos", "anchor_drift", "ref_requests"):
        assert required in present


def test_migrate_passes_dim_guard_on_fresh_store(tmp_path):
    """A fresh store (no persisted vectors) trivially passes the stored-vector dim guard —
    the fresh-start path the swap targets."""
    c = _build(tmp_path)
    c.migrate()  # no raise


def test_migrate_raises_on_stored_vector_dim_mismatch(tmp_path):
    """A persisted episode vector whose width != the embedder's native dim means the store was
    written under a DIFFERENT geometry (a forgotten `hive reset` after a model/dim swap). Boot
    fails fast (→ EX_SOFTWARE) rather than silently serving/searching mixed-dim garbage
    (Law 5 + Law 6)."""
    import numpy as np

    c = _build(
        tmp_path
    )  # FakeWarmProvider d=768: an arbitrary test width, not native 1024
    wrong = np.zeros(
        256, dtype=np.float32
    ).tobytes()  # 256-wide blob != the embedder's 768
    c.conn.execute(
        "INSERT INTO episodes (tenant_id, text, value, weight, ts, content_hash, status) "
        "VALUES ('default','t',?,1.0,0,'h','approved')",
        (wrong,),
    )
    with pytest.raises(RuntimeError, match="different geometry|stored vector dim"):
        c.migrate()


def test_build_index_warms_from_servable_only():
    c = _build()
    c.warm_embedder()
    c.admission.write(
        "a durable memory about cache eviction", proposed_by="a1"
    )  # v3: lands provisional, servable NOW
    c.index.rebuild_from_store([])  # clear the warm cache
    assert c.index.size() == 0
    c.build_index()  # rebuild from the store
    assert c.index.size() == 1


# ── make_server end-to-end (the wiring round-trip) ────────────────────────────
def test_make_server_round_trips_write_recall():
    c = _build()
    c.warm_embedder()
    server = c.make_server()
    assert isinstance(server, HiveMCPServer)
    text = "the WAL transaction runs under one BEGIN IMMEDIATE"
    w = _content(
        _call(server, "hive_write", {"text": text})
    )  # v3: no approver anywhere
    assert w["status"] == "approved"  # servable NOW (provisional)
    c.build_index()
    r = _content(
        _call(server, "hive_recall", {"query": text})
    )  # identical text ⇒ confident hit
    assert r["abstained"] is False
    assert any(h["text"] == text for h in r["reference_context"])


def test_index_dim_follows_injected_embedder():
    """The store/index vector width is sourced from the embedder's native ``d`` (the SINGLE
    source), so they can never disagree — an injected embedder of any dim sizes the index.
    (Re-introducing a separate geometry-dim source would let these diverge again.)"""
    c = _build(embedder=FakeWarmProvider(d=128))
    assert c.index.dim == 128


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


def test_recall_config_is_the_absolute_relevance_knob_set():
    # the entropy-gate knobs are GONE; the absolute-relevance gate carries exactly tau_serve
    # + k_min (re-adding a deleted knob, or dropping a new one, REDS this).
    from hive.app.config import RecallConfig

    rc = RecallConfig()
    for gone in ("H_frac_max", "softmax_beta", "tau_top1", "tau_thresh"):
        assert not hasattr(rc, gone), gone
    assert hasattr(rc, "tau_serve") and hasattr(rc, "k_min")
