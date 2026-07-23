"""M12 / P1.14 — the composition root: ``build_container(cfg)`` wires every REAL adapter
behind the Phase-0/Phase-1 validated ports into ONE ``Container`` the M12 entrypoint drives
as a ``Boot``.

This is the single swap-point switch (hexagonal): the registry selects the embedder /
index adapters by config key; this root constructs the rest and threads the runtime deps.
It owns the assembly ORDER (the one wiring fact tests can't get from the adapters) and the
Recall serves the dense+gate order as-is — there is no utility rerank (the surfacer and
the utility store were removed). Reinforcement is demand/exposure-only.

The ``Boot`` contract (consumed by ``hive.tools.entrypoint``):
    store               — the durable store (the entrypoint stamps readiness markers on it)
    migrate()           — ensure/verify the schema is migrated (WAL + tables) — EX_SOFTWARE
    build_index()       — warm the in-RAM index from the store's approved-only rows
    warm_embedder()     — load the model + verify the native dim (resident) — EX_UNAVAILABLE
    make_server()       — assemble the HiveMCPServer (embedder resident)
"""
from __future__ import annotations

import logging
from typing import Any

from hive.adapters.auth_store_sqlite import SqliteTokenStore
from hive.adapters.clock_system import SystemClock
from hive.adapters.scanner_regex import DefaultSecretScanner
from hive.adapters.sqlite_db import connect
from hive.adapters.store_sqlite import SqliteEpisodeStore
from hive.app import registry
from hive.app.config import Config
from hive.app.mcp_server import HiveMCPServer, ServerIdentity
from hive.domain.admission import AdmissionService
from hive.domain.conflict import ConflictFlagService
from hive.domain.lifecycle import DemandRule, LifecycleService
from hive.domain.recall import RecallPipeline

_log = logging.getLogger("hive.container")

_MEMORY_DB = ":memory:"
_DAY_S = 86_400

# The tables migrate() asserts are present — a missing one is a botched migration (EX_SOFTWARE),
# caught at boot rather than at first recall. Includes the v3 repo-partition set: the anchor
# bindings, the durable repo registry, the materialized drift cache, and the branch-demand list.
_REQUIRED_TABLES = frozenset({
    "blobs", "episodes", "exposure", "meta",
    "recall_misses", "evidence_events", "conflict_flags", "ingested_ranges",
    "episode_anchors", "repos", "anchor_drift", "ref_requests",
})


class Container:
    """The assembled, wired system. Implements the ``Boot`` protocol the entrypoint drives;
    also exposes every domain object so acceptance/e2e tests drive the true end-to-end path
    (write→approve→recall) without re-deriving the wiring."""

    def __init__(
        self, *, cfg: Config, conn, index, store, token_store, embedder, scanner,
        clock, gate, recall,
        admission, identity: ServerIdentity, started_ts: int,
        lifecycle=None, flag_service=None,
    ) -> None:
        self.cfg = cfg
        self.conn = conn
        self.index = index
        self.store = store                  # Boot.store — the entrypoint stamps markers here
        self.token_store = token_store      # Boot.token_store — the HTTP daemon's verify seam
        self.embedder = embedder
        self.scanner = scanner
        self.clock = clock
        self.gate = gate
        self.recall = recall
        self.admission = admission
        self.identity = identity
        self.started_ts = int(started_ts)
        self.lifecycle = lifecycle          # LifecycleService (boot sweep + triggers)
        self.flag_service = flag_service    # ConflictFlagService (advisory hive_flag) or None

    # ── Boot protocol (the strict boot order the entrypoint enforces) ─────────────
    def migrate(self) -> None:
        """Verify the store is migrated: every required table present (a missing one is a
        botched migration ⇒ raise ⇒ EX_SOFTWARE) and, for a persistent file DB, WAL active
        (the defensive boot assert; :memory: reports 'memory', not 'wal', so skip it)."""
        present = {r["name"] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        missing = _REQUIRED_TABLES - present
        if missing:
            _log.error("container.migrate_incomplete missing=%s", sorted(missing))
            raise RuntimeError(f"migration incomplete: missing tables {sorted(missing)}")
        if self.cfg.db_path != _MEMORY_DB:
            mode = self.conn.execute("PRAGMA journal_mode").fetchone()[0]
            if str(mode).lower() != "wal":
                _log.error("container.wal_pragma_mismatch observed=%s expected=wal", mode)
                raise RuntimeError(f"WAL pragma mismatch: journal_mode={mode!r} (expected wal)")
        self._assert_stored_vectors_match_embedder_dim()
        _log.info("container.migrate_done tables=%d db_path=%s",
                  len(present), self.cfg.db_path)

    def _assert_stored_vectors_match_embedder_dim(self) -> None:
        """Law 5 + Law 6 boot guard: any persisted episode vector whose width != the embedder's
        native dim means the store was written under a DIFFERENT geometry (a forgotten `hive reset`
        after a model/dim swap). Fail fast at boot (→ EX_SOFTWARE) rather than build the index from
        mixed-dim rows and serve garbage. A fresh store (no vectors) passes trivially; values are
        float32 BLOBs, so the width is the byte length / 4."""
        row = self.conn.execute(
            "SELECT value FROM episodes WHERE value IS NOT NULL LIMIT 1").fetchone()
        if row is None or row["value"] is None:
            return
        width = len(row["value"]) // 4
        d = int(self.embedder.d)
        if width != d:
            _log.error("container.stored_dim_mismatch width=%d embedder_d=%d", width, d)
            raise RuntimeError(
                f"stored vector dim {width} != embedder dim {d} — the store was written under a "
                "different geometry; re-initialise it (hive reset) before serving")

    def build_index(self) -> None:
        """Boot order: decay sweep FIRST (materialize lazy TTL deaths so the
        rebuild never warms a dead row), THEN warm the in-RAM index from the
        SERVABLE set (the divergence recovery: the durable rows are truth; the
        cache is rebuilt from them)."""
        swept = self.lifecycle.sweep() if self.lifecycle is not None else {}
        now = int(self.clock.now())
        p_ttl_s = int(self.cfg.autonomy.provisional_ttl_days) * _DAY_S
        self.store.rebuild_index_from_store(now=now, provisional_ttl_s=p_ttl_s)
        _log.info("container.index_built size=%d swept=%s", self.index.size(), swept)

    def warm_embedder(self) -> Any:
        """Bring the embedder RESIDENT (model loaded + native dim verified). Returns the
        embedder; the entrypoint maps a raise / not-loaded to EX_UNAVAILABLE."""
        self.embedder.load()
        _log.info("container.embedder_warm name=%s d=%s loaded=%s",
                  getattr(self.embedder, "name", "?"), getattr(self.embedder, "d", "?"),
                  getattr(self.embedder, "loaded", "?"))
        return self.embedder

    def make_server(self) -> HiveMCPServer:
        # The v3 store IS the server's retirement-gate evidence feed (evidence_rows_for +
        # insert_audit), scope reader (repo_registry + scan_servable_labeled), and drift
        # source (drift_get for attach_drift) — one handle wires all three. No AGI sentinel
        # exists, and no global canonical_ref is passed: per-repo canonical refs live in the
        # repo registry, so the last_verified rider keeps its unscoped default read.
        db_path = "" if self.cfg.db_path == _MEMORY_DB else self.cfg.db_path
        return HiveMCPServer(
            admission=self.admission, recall=self.recall, store=self.store,
            embedder=self.embedder,
            identity=self.identity, now=self.clock.now, started_ts=self.started_ts,
            db_path=db_path, autonomy=self.cfg.autonomy,
            conflict=self.cfg.conflict, flag_service=self.flag_service,
            suspect_consensus=self.cfg.suspect_consensus,
            secret_scan_enabled=self.cfg.secret_scan.enabled)

    # ── convenience surface (clean shutdown) ──────────────────────────────────────
    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:                   # noqa: BLE001 — shutdown best-effort
            _log.warning("container.close_failed")


def build_container(cfg: Config, *, tenant_id: str, agent_id: str,
                    embedder: Any = None, clock: Any = None) -> Container:
    """Assemble the full system for ``cfg``. ``embedder`` / ``clock`` are injection seams
    (the entrypoint passes none ⇒ real adapters; tests inject doubles / a deterministic
    clock). Returns a ``Container`` ready for the boot sequence
    ``migrate → build_index → warm_embedder → make_server``.

    // construction is torch-cheap: the heavy SentenceTransformer load is deferred to
    ``warm_embedder``; here we only open the DB, apply schema, and wire."""
    # The store path defaults to :memory: only at the CONFIG layer (a safe default that cannot
    # corrupt a persistent store); the containerized entrypoint injects /data/shared.db when the
    # env is unset, so an ephemeral daemon requires an EXPLICIT :memory:. Either way the
    # loss-prone posture is never silent — like the disabled secret floor: a loud boot WARN
    # here, plus a hive_health key when ephemeral.
    if cfg.db_path == _MEMORY_DB:
        _log.warning("container.store_ephemeral: the store is IN-MEMORY (:memory:) and ALL "
                     "captured memory is LOST on restart. Set HIVE_STORE__DB_PATH=/data/shared.db "
                     "in .env to persist into the hive-data volume")
    conn = connect(cfg.db_path, check_same_thread=False)   # shared across HTTP handler threads (lock-serialized)
    # The embedder is built FIRST (torch-cheap — the model load is deferred to warm): its native
    # ``d`` is the SINGLE source of the store/index vector width, so they can never disagree.
    if embedder is None:
        embedder = registry.build_embedder(cfg)
    index = registry.build_index(dim=int(embedder.d))
    store = SqliteEpisodeStore(conn, index=index)
    # Auth token store: owns its own `access_tokens` table — built anywhere after conn.
    token_store = SqliteTokenStore(conn)
    # The credential secret-floor (HIVE_SECRET_SCAN__ENABLED, default ON). This ONE flag flows
    # to every scanner consumer (admission, the recall miss-floor, the conflict-flag note) via
    # the single injected adapter. Disabling it LOOSENS a safety gate, so it is never silent:
    # a loud boot WARN here, plus a hive_health key when off.
    scanner = DefaultSecretScanner(enabled=cfg.secret_scan.enabled)
    if not cfg.secret_scan.enabled:
        _log.warning("container.secret_floor_disabled: HIVE_SECRET_SCAN__ENABLED is off — "
                     "credential scanning is BYPASSED; raw text (including secrets) will be "
                     "persisted unscanned into shared memory")
    clock = clock or SystemClock()

    gate = registry.build_gate(cfg)
    # The mechanical lifecycle: pure DemandRule + the trigger service, wired into
    # admission (capture trigger + sweep piggyback) and recall (miss trigger).
    aut = cfg.autonomy
    lifecycle = LifecycleService(
        store=store, index=index,
        rule=DemandRule(demand_m=aut.demand_m, demand_tau=aut.demand_tau,
                        competitor_tau=aut.competitor_tau),
        now=clock.now,
        demand_window_s=aut.demand_window_days * _DAY_S,
        quarantine_ttl_s=aut.quarantine_ttl_days * _DAY_S,
        provisional_ttl_s=aut.provisional_ttl_days * _DAY_S,
        enabled=aut.enabled, anomaly_tau=aut.anomaly_tau,
        anomaly_min_cluster=aut.anomaly_min_cluster,
        # verified-promotion rung: OFF ⇒ no handle ⇒ unreachable, byte-inert (§9.9)
        verified_reader=store if aut.verified_promotion else None)
    recall = RecallPipeline(
        embedder=embedder, index=index, gate=gate,
        reader=store,
        recall_top_n=cfg.recall.recall_top_n,
        ledger=store, clock_now=clock.now, scanner=scanner,
        provisional_ttl_s=aut.provisional_ttl_days * _DAY_S,
        lifecycle=lifecycle, autonomy_enabled=aut.enabled,
        overscan=cfg.recall.overscan,
        dup_tau=cfg.conflict.tau,             # near-dup floor single-owned by ConflictConfig.tau
        conflict_enabled=cfg.conflict.enabled, conflict_top_n=cfg.conflict.top_n)
    admission = AdmissionService(store, scanner, embedder, now=clock.now,
                                 lifecycle=lifecycle, autonomy_enabled=aut.enabled)
    # the advisory conflict-flag service (Layer 2): reuses the store (ConflictFlagStore +
    # EpisodeReader) + scanner; gated by conflict.enabled. Holds NO retirement handle.
    flag_service = ConflictFlagService(
        flag_store=store, scanner=scanner, reader=store, now=clock.now,
        enabled=cfg.conflict.enabled)

    identity = ServerIdentity(tenant_id=tenant_id, agent_id=agent_id)

    _log.info("container.assembled tenant_id=%s db_path=%s d=%d provider=%s "
              "autonomy=%s", tenant_id, cfg.db_path, int(embedder.d),
              cfg.embedding.provider, aut.enabled)
    return Container(
        cfg=cfg, conn=conn, index=index, store=store, token_store=token_store,
        embedder=embedder, scanner=scanner, clock=clock, gate=gate,
        recall=recall, admission=admission,
        identity=identity, started_ts=int(clock.now()), lifecycle=lifecycle,
        flag_service=flag_service)
