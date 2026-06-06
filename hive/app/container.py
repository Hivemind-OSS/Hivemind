"""M12 / P1.14 — the composition root: ``build_container(cfg)`` wires every REAL adapter
behind the Phase-0/Phase-1 validated ports into ONE ``Container`` the M12 entrypoint drives
as a ``Boot``.

This is the single swap-point switch (hexagonal): the registry selects the embedder /
index / producer adapters by config key; this root constructs the rest and threads the
runtime deps. It owns the assembly ORDER (the one wiring fact tests can't get from the
adapters) and the Phase-1 policy: **utility is OBSERVED-NOT-APPLIED** —
``surfacer.enabled=False`` (byte-identical recall passthrough). Posteriors accrue from the
producer's verifiable clawback-settled stream; ranking does NOT move until a Phase-2 KEEP.

The ``Boot`` contract (consumed by ``hive.tools.entrypoint``):
    store               — the durable store (the entrypoint stamps readiness markers on it)
    migrate()           — ensure/verify the schema is migrated (WAL + tables) — EX_SOFTWARE
    build_index()       — warm the in-RAM index from the store's approved-only rows
    warm_embedder()     — load the model + freeze the head (resident) — EX_UNAVAILABLE
    make_server()       — assemble the HiveMCPServer (embedder resident)

Critical ordering (made structural here, not hoped-for): ``SqliteUtilityStore(conn)`` is
constructed BEFORE ``SqliteEpisodeStore(conn, isolation_frac=...)`` because the episode
store fails fast if the co-located ``utility`` table is absent when guardrail-2 is on
(the held-out slice must be stampable atomically at approve()).
"""
from __future__ import annotations

import logging
import random
from typing import Any, Optional

from hive.adapters.clock_system import SystemClock
from hive.adapters.scanner_regex import DefaultSecretScanner
from hive.adapters.sqlite_db import connect
from hive.adapters.store_sqlite import SqliteEpisodeStore
from hive.adapters.utility_store_sqlite import SqliteUtilityStore
from hive.app import registry
from hive.app.config import Config
from hive.app.health import health
from hive.app.mcp_server import HiveMCPServer, ServerIdentity
from hive.app.onboard import InstallPlanner
from hive.domain.admission import AdmissionService
from hive.domain.attribution import Attributor
from hive.domain.join import OutcomeJoiner
from hive.domain.models import SourcePoll
from hive.domain.produce import OutcomeProducer
from hive.domain.recall import RecallPipeline
from hive.domain.surfacer import UtilitySurfacer

_log = logging.getLogger("hive.container")

_MEMORY_DB = ":memory:"

# The §11 settlement schedule + reward magnitudes. These are pure credit POLICY (not part
# of M11's ProducerConfig surface in v-min); pinned here as documented defaults — the one
# place they enter the wiring, single-sourced so the query/credit families stay comparable.
_SETTLE_DAYS = 7
_PROVISIONAL_REWARD = 1.0
_CLAWBACK_REWARD = -1.0

# Deterministic RNG seeds: the surfacer ε-explore and the joiner association-jitter are
# seeded from fixed constants so a container build is reproducible (a test can assert a
# stable recall order; the Phase-1 surfacer is inert anyway).
_SURFACER_SEED = 1
_JOINER_SEED = 2

# Persisted PCA geometry (so a restart reuses the SAME basis ⇒ byte-stable recall). Stored
# in the episode store's meta kv under the strict-prefix discipline.
_HEAD_BYTES_KEY = "embedding:head:bytes"
_HEAD_WVER_KEY = "embedding:head:w_version"

# The tables migrate() asserts are present — a missing one is a botched migration (EX_SOFTWARE),
# caught at boot rather than at first recall.
_REQUIRED_TABLES = frozenset({
    "blobs", "episodes", "exposure", "task_outcomes", "meta",
    "utility", "utility_sources", "utility_layer",
})


class _EmptySource:
    """The ``OutcomeSource`` for the no-``watch_repos`` idle state: polls OK with zero
    commits, so the producer tick is a clean no-op (the loop is idle, not broken — the
    health probe already WARNs that crediting is starved without watched repos)."""

    def __init__(self, repo_remote: str = "") -> None:
        self._repo_remote = str(repo_remote)

    def poll(self) -> SourcePoll:
        return SourcePoll(repo_remote=self._repo_remote, ok=True)


class Container:
    """The assembled, wired system. Implements the ``Boot`` protocol the entrypoint drives;
    also exposes every domain object so acceptance/e2e tests drive the true end-to-end path
    (write→approve→recall, producer tick, re-embed) without re-deriving the wiring."""

    def __init__(
        self, *, cfg: Config, conn, index, store, utility_store, embedder, scanner,
        clock, gate, surfacer, attributor, joiner, source, producer, recall,
        admission, install_planner, identity: ServerIdentity, started_ts: int,
    ) -> None:
        self.cfg = cfg
        self.conn = conn
        self.index = index
        self.store = store                  # Boot.store — the entrypoint stamps markers here
        self.utility_store = utility_store
        self.embedder = embedder
        self.scanner = scanner
        self.clock = clock
        self.gate = gate
        self.surfacer = surfacer
        self.attributor = attributor
        self.joiner = joiner
        self.source = source
        self.producer = producer
        self.recall = recall
        self.admission = admission
        self.install_planner = install_planner
        self.identity = identity
        self.started_ts = int(started_ts)

    # ── Boot protocol (the strict boot order the entrypoint enforces) ─────────────
    def migrate(self) -> None:
        """Verify the store is migrated: every required table present (a missing one is a
        botched migration ⇒ raise ⇒ EX_SOFTWARE) and, for a persistent file DB, WAL active
        (the §6 defensive boot assert; :memory: reports 'memory', not 'wal', so skip it)."""
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
        _log.info("container.migrate_done tables=%d db_path=%s",
                  len(present), self.cfg.db_path)

    def build_index(self) -> None:
        """Warm the in-RAM index from the store's APPROVED-only rows (the §4.3 divergence
        recovery: durable truth is status='approved'; the cache is rebuilt from it)."""
        self.store.rebuild_index_from_store()
        _log.info("container.index_built size=%d", self.index.size())

    def warm_embedder(self) -> Any:
        """Bring the embedder RESIDENT (model loaded + head frozen) and persist the frozen
        head bytes so a restart reuses the SAME geometry. Returns the embedder; the
        entrypoint maps a raise / not-loaded to EX_UNAVAILABLE."""
        self.embedder.load()
        head_fn = getattr(self.embedder, "head_bytes", None)
        if head_fn is not None:
            try:
                self.store.meta_set(_HEAD_BYTES_KEY, head_fn().hex())
                self.store.meta_set(_HEAD_WVER_KEY, str(int(self.embedder.w_version)))
            except Exception as exc:        # noqa: BLE001 — persistence is best-effort
                _log.warning("container.head_persist_failed kind=%s", type(exc).__name__)
        _log.info("container.embedder_warm name=%s w_version=%s loaded=%s",
                  getattr(self.embedder, "name", "?"), self.embedder.w_version,
                  getattr(self.embedder, "loaded", "?"))
        return self.embedder

    def make_server(self) -> HiveMCPServer:
        db_path = "" if self.cfg.db_path == _MEMORY_DB else self.cfg.db_path
        return HiveMCPServer(
            admission=self.admission, recall=self.recall, store=self.store,
            embedder=self.embedder, install_planner=self.install_planner,
            identity=self.identity, now=self.clock.now, started_ts=self.started_ts,
            db_path=db_path, trailer_key=self.cfg.producer.stamp_trailer)

    # ── convenience surfaces (health probe + clean shutdown) ──────────────────────
    def health(self, *, repo_path: Optional[str] = None):
        return health(self.cfg, self.store, self.embedder, self.producer,
                      repo_path=repo_path,
                      link_status=getattr(self.install_planner, "link_status", None))

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:                   # noqa: BLE001 — shutdown best-effort
            _log.warning("container.close_failed")


def _load_head_bytes(store, cfg: Config) -> Optional[bytes]:
    """The persisted PCA head for THIS geometry (W_version), if any — so a restart reuses
    the frozen basis instead of re-fitting (and recall stays byte-stable). A W_version bump
    invalidates it (different geometry ⇒ refit at warm)."""
    try:
        raw = store.meta_get(_HEAD_BYTES_KEY)
        wver = store.meta_get(_HEAD_WVER_KEY)
    except Exception:                       # noqa: BLE001 — a meta read fault ⇒ refit
        return None
    if not raw or wver is None or int(wver) != int(cfg.geometry.W_version):
        return None
    try:
        return bytes.fromhex(raw)
    except ValueError:
        _log.warning("container.head_bytes_corrupt — refitting")
        return None


def _build_source(cfg: Config):
    """The real ``OutcomeSource``: a GitCliSource over the first watched repo (multi-repo
    fan-out is a later registry concern), or an idle ``_EmptySource`` when none are watched."""
    repos = tuple(cfg.producer.watch_repos)
    if not repos:
        return _EmptySource()
    from hive.adapters.git_source import GitCliSource  # noqa: PLC0415 — subprocess adapter, lazy
    return GitCliSource(repos[0], stamp_trailer=cfg.producer.stamp_trailer,
                        bugfix_pattern=cfg.producer.bugfix_pattern)


def build_container(cfg: Config, *, tenant_id: str, agent_id: str,
                    embedder: Any = None, source: Any = None, clock: Any = None) -> Container:
    """Assemble the full system for ``cfg``. ``embedder`` / ``source`` / ``clock`` are
    injection seams (the entrypoint passes none ⇒ real adapters; tests inject doubles /
    fakes / a deterministic clock). Returns a ``Container`` ready for the boot sequence
    ``migrate → build_index → warm_embedder → make_server``.

    // construction is torch-cheap: the heavy SentenceTransformer load + PCA fit are
    deferred to ``warm_embedder``; here we only open the DB, apply schema, and wire."""
    conn = connect(cfg.db_path)
    index = registry.build_index(cfg)
    # ORDER: utility store first (creates the `utility` table) so the episode store's
    # guardrail-2 fail-fast (isolation_frac > 0 requires that table) is satisfied.
    utility_store = SqliteUtilityStore(conn)
    store = SqliteEpisodeStore(conn, index=index, isolation_frac=cfg.utility.isolation_frac)
    scanner = DefaultSecretScanner()
    clock = clock or SystemClock()

    if embedder is None:
        head_bytes = _load_head_bytes(store, cfg)
        embedder = registry.build_embedder(cfg, head_bytes=head_bytes)
    if int(getattr(embedder, "d", cfg.geometry.d)) != int(cfg.geometry.d):
        raise ValueError(
            f"embedder.d={getattr(embedder, 'd', None)} != geometry.d={cfg.geometry.d} "
            "— the index/store dim and the embedder projection must agree")

    gate = registry.build_gate(cfg)
    # Phase-1 INERT surfacer: enabled=False ⇒ byte-identical passthrough (utility observed,
    # not applied). Flipping this default to True is the §6.1 RULE-2 mutation.
    surfacer = UtilitySurfacer(
        enabled=False, epsilon_explore=cfg.recall.epsilon_explore,
        f_min=cfg.utility.f_min, f_max=cfg.utility.f_max, rng=random.Random(_SURFACER_SEED))
    recall = RecallPipeline(
        embedder=embedder, index=index, gate=gate, surfacer=surfacer,
        ledger=store, reader=store, utility_store=utility_store,
        recall_top_n=cfg.recall.recall_top_n, now=clock.now)
    admission = AdmissionService(store, scanner, embedder, now=clock.now)

    attributor = Attributor()
    joiner = OutcomeJoiner(
        assoc_window_s=cfg.producer.assoc_window_s, settle_days=_SETTLE_DAYS,
        provisional_reward=_PROVISIONAL_REWARD, clawback_reward=_CLAWBACK_REWARD,
        require_stamp=cfg.producer.require_stamp, assoc_epsilon=cfg.producer.assoc_epsilon,
        rng=random.Random(_JOINER_SEED))
    source = source or _build_source(cfg)
    producer = OutcomeProducer(
        source=source, joiner=joiner, attributor=attributor, store=store,
        utility_store=utility_store, clock=clock, source_agent=agent_id)

    install_planner = InstallPlanner(
        store, stamp_trailer=cfg.producer.stamp_trailer,
        watch_repos=cfg.producer.watch_repos, block_version=1)
    identity = ServerIdentity(tenant_id=tenant_id, agent_id=agent_id)

    _log.info("container.assembled tenant_id=%s db_path=%s d=%d backend=%s provider=%s",
              tenant_id, cfg.db_path, cfg.geometry.d, cfg.index.backend, cfg.embedding.provider)
    return Container(
        cfg=cfg, conn=conn, index=index, store=store, utility_store=utility_store,
        embedder=embedder, scanner=scanner, clock=clock, gate=gate, surfacer=surfacer,
        attributor=attributor, joiner=joiner, source=source, producer=producer,
        recall=recall, admission=admission, install_planner=install_planner,
        identity=identity, started_ts=int(clock.now()))
