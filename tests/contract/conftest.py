"""Shared substrate for the U4-THIN-AGENT frozen contract suite (CT-1..CT-14).

The suite drives the REAL surfaces only:
  - ``HiveMCPServer.handle`` assembled by the real ``build_container`` over a temp
    SQLite store with the deterministic fake embedder (existing convention),
  - ``SyncService.tick`` with the injectable scripted ``Run`` seam (existing
    ``tests/sync`` convention) plus REAL tmp git origins,
  - the ctl / CLI / boot mains (``hive.tools.repoctl`` / ``hive.tools.cli`` /
    ``hive.tools.entrypoint``).

RED-FIRST MECHANICS: this suite is authored against the UN-BUILT v3 system and must
be red as FAILING ASSERTIONS, never import/collection errors. Every v3-only module,
store method, table, or signature is therefore reached through a ``require_*`` /
``load_module`` guard that turns "not built yet" into a clean assertion failure.
No later chunk may edit tests/contract/** — the guards double as the pinned v3
surface names (module paths, store methods, meta keys, table DDL) implementers
must conform to.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import inspect
import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional, Sequence

import numpy as np
import pytest

from hive.adapters.sqlite_db import connect
from hive.adapters.store_sqlite import SqliteEpisodeStore
from hive.app.config import Config
from hive.app.mcp_server import HiveMCPServer, MCPRequest, ServerIdentity
from hive.domain.change_evidence import ChangeEvidenceService
from tests.fakes._fakes import FakeClock, FakeClusterProvider, FakeWarmProvider

# The real-git repo factory, REUSED from the sync suite (grounded: Origin + git()
# exist there today) — never a mock of git.
from tests.sync.conftest import ANCHOR, Origin, git, harness_env  # noqa: F401

DIM = 32  # container-fixture embedder width (small, fast, deterministic)
SYNC_DIM = 4  # sync-fixture vector width (mirrors tests/sync)

DAY_S = 86_400

# ── the v3 wire vocabularies (plan §3.4, normative) ───────────────────────────
DRIFT_FRESH = "fresh"
DRIFT_ANCHOR_CHANGED = "anchor_changed"
DRIFT_ANCHOR_MISSING = "anchor_missing"
DRIFT_BLAST_RADIUS = "blast_radius_changed"
DRIFT_BRANCH_SCOPED = "branch_scoped"
DRIFT_UNVERIFIABLE = "unverifiable"
DRIFT_NA = "n/a"
DRIFT_TYPES = (
    DRIFT_FRESH,
    DRIFT_ANCHOR_CHANGED,
    DRIFT_ANCHOR_MISSING,
    DRIFT_BLAST_RADIUS,
    DRIFT_BRANCH_SCOPED,
    DRIFT_UNVERIFIABLE,
    DRIFT_NA,
)
# most-severe-wins aggregation order (§3.4): index 0 = most severe
DRIFT_SEVERITY = (
    DRIFT_ANCHOR_MISSING,
    DRIFT_ANCHOR_CHANGED,
    DRIFT_BLAST_RADIUS,
    DRIFT_BRANCH_SCOPED,
    DRIFT_UNVERIFIABLE,
    DRIFT_FRESH,
)

# §3.2 normative noop wording (asserted literally where the plan demands it)
NOOP_REASON = "no qualifying machine signal"

# a text every named-rule scanner refuses (openai_key: sk- + 20+ chars)
SECRET_TEXT = "the deploy key is sk-ABCDEFGHIJKLMNOPQRSTUVWX12345678 keep it safe"

# ── module guards (v3-only modules; assert-not-import so red = assertion) ─────


def load_module(dotted: str):
    """Import a v3-only module or return None when it does not exist yet."""
    try:
        if importlib.util.find_spec(dotted) is None:
            return None
    except (ImportError, ModuleNotFoundError):
        return None
    return importlib.import_module(dotted)


def importlib_import_module(dotted: str):  # tiny alias to keep lint happy
    import importlib

    return importlib.import_module(dotted)


def require_module(dotted: str):
    mod = load_module(dotted)
    assert mod is not None, f"v3 module {dotted!r} does not exist yet"
    return mod


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def require_table(store: SqliteEpisodeStore, name: str) -> None:
    assert table_exists(store.conn, name), (
        f"v3 schema table {name!r} is missing — schema v3 (plan §3.5) not built"
    )


def require_method(obj: Any, name: str):
    assert hasattr(obj, name), (
        f"v3 surface {type(obj).__name__}.{name} does not exist yet"
    )
    return getattr(obj, name)


# ── the container rig (build_container + temp SQLite + fake embedder) ─────────


@dataclass
class Rig:
    container: Any
    server: HiveMCPServer
    clock: FakeClock
    store: SqliteEpisodeStore
    embedder: Any
    db_path: str


def make_rig(
    tmp_path,
    *,
    embedder: Any = None,
    t0: int = 1_000_000,
    db_name: str = "hive.db",
    **cfg_overrides: Mapping[str, Any],
) -> Rig:
    """Assemble the REAL system: ``build_container`` on a temp SQLite file with a
    deterministic fake embedder + fake clock, booted in the production order
    (migrate → build_index → warm_embedder), then ``make_server``."""
    from hive.app.container import build_container

    db_path = str(tmp_path / db_name)
    cfg = Config.load(db_path=db_path, env={}, **cfg_overrides)
    embedder = embedder or FakeWarmProvider(d=DIM)
    clock = FakeClock(t0)
    container = build_container(
        cfg, tenant_id="ct", agent_id="ct-agent", embedder=embedder, clock=clock
    )
    container.migrate()
    container.build_index()
    container.warm_embedder()
    server = container.make_server()
    return Rig(
        container=container,
        server=server,
        clock=clock,
        store=container.store,
        embedder=embedder,
        db_path=db_path,
    )


@pytest.fixture
def rig(tmp_path) -> Rig:
    return make_rig(tmp_path)


@pytest.fixture
def cluster_rig(tmp_path) -> Rig:
    """A rig whose embedder clusters by ``cid=<n>`` text tags — the controllable
    near-duplicate substrate (conflict / partition / competitor scenarios)."""
    return make_rig(tmp_path, embedder=FakeClusterProvider(d=DIM))


# ── MCP call helpers ──────────────────────────────────────────────────────────


def ident(agent_id: str) -> ServerIdentity:
    return ServerIdentity(tenant_id="ct", agent_id=agent_id)


def call(
    server: HiveMCPServer,
    name: str,
    args: Optional[dict] = None,
    *,
    identity: Optional[ServerIdentity] = None,
    req_id: int = 1,
):
    req = MCPRequest(req_id, "tools/call", {"name": name, "arguments": args or {}})
    return server.handle(req, identity=identity)


def payload(resp) -> dict:
    """The tool-result content payload as a dict. NEVER raises on today's shapes:
    a JSON-RPC error becomes ``{"_error": ...}``, a bare-string tool error becomes
    ``{"_raw": ...}`` — so a v3 assertion against it FAILS instead of erroring."""
    if resp.error is not None:
        return {"_error": resp.error}
    content = (resp.result or {}).get("content") or [{}]
    text = content[0].get("text", "")
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return {"_raw": text}
    return parsed if isinstance(parsed, dict) else {"_raw": text}


def is_error(resp) -> bool:
    if resp.error is not None:
        return True
    return bool((resp.result or {}).get("isError"))


def write(
    server: HiveMCPServer,
    text: str,
    *,
    identity: Optional[ServerIdentity] = None,
    **kw,
) -> dict:
    """The v3 write: ``hive_write {text, ...}`` — NO approver field exists."""
    return payload(call(server, "hive_write", {"text": text, **kw}, identity=identity))


def write_ok(server: HiveMCPServer, text: str, **kw) -> int:
    env = write(server, text, **kw)
    assert env.get("status") in ("approved", "redacted"), (
        f"v3 hive_write did not land: {env}"
    )
    assert isinstance(env.get("id"), int), f"v3 hive_write returned no id: {env}"
    return env["id"]


def capture(
    server: HiveMCPServer,
    text: str,
    *,
    identity: Optional[ServerIdentity] = None,
    **kw,
) -> dict:
    return payload(
        call(server, "hive_capture", {"text": text, **kw}, identity=identity)
    )


def recall(
    server: HiveMCPServer,
    query: str,
    *,
    repos: Optional[Sequence[str]] = None,
    anchor_prefix: Optional[str] = None,
    identity: Optional[ServerIdentity] = None,
) -> dict:
    args: dict = {"query": query}
    if repos is not None:
        args["repos"] = list(repos)
    if anchor_prefix is not None:
        args["anchor_prefix"] = anchor_prefix
    return payload(call(server, "hive_recall", args, identity=identity))


def served_ids(env: dict) -> list[int]:
    return [h.get("episode_id") for h in env.get("reference_context", [])]


def hit_for(env: dict, episode_id: int) -> dict:
    for h in env.get("reference_context", []):
        if h.get("episode_id") == episode_id:
            return h
    raise AssertionError(
        f"episode {episode_id} not among served hits {served_ids(env)}"
    )


def trust_of(store: SqliteEpisodeStore, episode_id: int) -> str:
    ep = store.get_episode(episode_id)
    assert ep is not None, f"episode {episode_id} vanished"
    return ep.trust


def evidence_rows(
    store: SqliteEpisodeStore, episode_id: int, kind: Optional[str] = None
) -> list[dict]:
    sql = "SELECT episode_id, kind, actor, ts, payload FROM evidence_events WHERE episode_id=?"
    args: list = [episode_id]
    if kind is not None:
        sql += " AND kind=?"
        args.append(kind)
    return [dict(r) for r in store.conn.execute(sql + " ORDER BY id", args)]


def n_episodes(server: HiveMCPServer) -> int:
    return payload(call(server, "hive_health"))["n_episodes"]


# ── the v3 repo registry / drift-cache seams (plan-pinned store surface) ──────


def register_repo(
    store: SqliteEpisodeStore,
    name: str,
    url: str,
    *,
    canonical_ref: str = "",
    token_env: str = "",
    ts: int = 0,
) -> None:
    """Register a repo through the v3 store surface (``repo_add`` — plan §6 step 2)."""
    add = require_method(store, "repo_add")
    add(
        name=name,
        url=url,
        canonical_ref=canonical_ref,
        token_env=token_env,
        added_ts=int(ts),
    )


def registry_rows(store: SqliteEpisodeStore) -> dict[str, dict]:
    require_table(store, "repos")
    return {
        r["name"]: dict(r)
        for r in store.conn.execute("SELECT * FROM repos ORDER BY name")
    }


def set_canonical_tip(store: SqliteEpisodeStore, repo: str, tip_sha: str) -> None:
    """The per-repo sync watermark meta key (§3.5: ``sync:<name>:last_tip``)."""
    store.meta_set(f"sync:{repo}:last_tip", tip_sha)


def meta_value(store: SqliteEpisodeStore, key: str) -> Optional[str]:
    row = store.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return None if row is None else str(row[0])


def drift_put(store: SqliteEpisodeStore, rows: Iterable[tuple]) -> None:
    """Materialize drift verdicts through the v3 store surface. Row shape mirrors
    the §3.5 DDL column order: (repo, tip_sha, anchor, verdict, detail_json, ts)."""
    put = require_method(store, "drift_put")
    put(list(rows))


def anchor_rows(store: SqliteEpisodeStore, episode_id: int) -> list[dict]:
    require_table(store, "episode_anchors")
    return [
        dict(r)
        for r in store.conn.execute(
            "SELECT * FROM episode_anchors WHERE episode_id=? ORDER BY repo, anchor",
            (episode_id,),
        )
    ]


def anchor_fp(
    store: SqliteEpisodeStore, episode_id: int, repo: str, anchor: str
) -> dict:
    require_table(store, "episode_anchors")
    row = store.conn.execute(
        "SELECT fp_meta FROM episode_anchors WHERE episode_id=? AND repo=? AND anchor=?",
        (episode_id, repo, anchor),
    ).fetchone()
    assert row is not None, (
        f"no episode_anchors row for ({episode_id}, {repo!r}, {anchor!r})"
    )
    raw = row["fp_meta"]
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def drift_rows(
    store: SqliteEpisodeStore, repo: str, tip_sha: Optional[str] = None
) -> list[dict]:
    require_table(store, "anchor_drift")
    sql = "SELECT * FROM anchor_drift WHERE repo=?"
    args: list = [repo]
    if tip_sha is not None:
        sql += " AND tip_sha=?"
        args.append(tip_sha)
    return [dict(r) for r in store.conn.execute(sql + " ORDER BY anchor", args)]


def ref_request_rows(store: SqliteEpisodeStore) -> list[dict]:
    require_table(store, "ref_requests")
    return [
        dict(r)
        for r in store.conn.execute("SELECT * FROM ref_requests ORDER BY repo, ref")
    ]


# ── v3 store-level episode seeding (for the server-less sync fixtures) ────────

_vec_provider = None


def _unit_vec(text: str, d: int) -> np.ndarray:
    from tests.fakes._fakes import FakeProvider

    global _vec_provider
    if _vec_provider is None or _vec_provider.d != d:
        _vec_provider = FakeProvider(d=d)
    return _vec_provider.encode(text)


def seed_scoped_episode(
    store: SqliteEpisodeStore,
    text: str,
    *,
    anchors: Sequence[tuple[str, str]] = (),
    repos: Sequence[str] = (),
    trust: str = "provisional",
    polarity: str = "neutral",
    ts: int = 10,
    proposed_by: str = "seeder",
    d: int = SYNC_DIM,
) -> int:
    """Stage + complete one episode through the v3 store signature:
    ``stage(..., anchors=[(repo, anchor), ...], repos=[...])`` (plan §6 step 2 —
    the ``tags``/``provenance`` params are gone)."""
    params = inspect.signature(store.stage).parameters
    assert "anchors" in params and "repos" in params, (
        "v3 stage(anchors=, repos=) signature not implemented (plan §6 step 2)"
    )
    assert "provenance" not in params and "tags" not in params, (
        "v3 stage still carries the dropped provenance/tags params"
    )
    eid, _deduped = store.stage(
        text=text,
        weight=1.0,
        proposed_by=proposed_by,
        ts=ts,
        polarity=polarity,
        anchors=[tuple(a) for a in anchors],
        repos=list(repos),
    )
    ok = store.complete(
        eid,
        _unit_vec(text, d),
        expected_version=0,
        trust=trust,
        last_active_ts=ts,
    )
    assert ok, f"complete() failed for seeded episode {eid}"
    return eid


# ── the v3 sync rig (registry-driven SyncService over real git origins) ───────


@dataclass
class SyncRig:
    service: Any
    store: SqliteEpisodeStore
    mirror_base: Any  # Path — mirrors live at <mirror_base>/<name>


def make_syncer(
    store: SqliteEpisodeStore,
    tmp_path,
    *,
    run=None,
    now=None,
    interval_s: int = 5,
) -> SyncRig:
    """A v3 ``SyncService`` wired the entrypoint way: the real store as
    reader/appender/ranges, one fresh global lock, and the v3 ``SyncConfig``
    surface only ({interval_s, webhook_secret, mirror_dir} — no repo_url, no
    token, no verify_candidates). Repos come from the STORE REGISTRY, read each
    tick. Mirrors land at ``<mirror_dir>/<name>`` (plan §6 step 5)."""
    import threading

    from hive.app.config import SyncConfig
    from hive.app.sync import SyncService

    mirror_base = tmp_path / "mirrors"
    cfg = SyncConfig(interval_s=interval_s, mirror_dir=str(mirror_base))
    evidence = ChangeEvidenceService(
        reader=store, appender=store, now=lambda: 424_242, ranges=store
    )
    kwargs: dict = {}
    if run is not None:
        kwargs["run"] = run
    if now is not None:
        kwargs["now"] = now
    service = SyncService(cfg, store, evidence, threading.Lock(), **kwargs)
    return SyncRig(service=service, store=store, mirror_base=mirror_base)


@pytest.fixture
def sync_store() -> SqliteEpisodeStore:
    # prod factory + prod thread posture (mirrors tests/sync/conftest.py)
    return SqliteEpisodeStore(connect(":memory:", check_same_thread=False))


def mirror_is_git(mirror_base, name: str) -> bool:
    path = mirror_base / name
    if not path.exists():
        return False
    proc = git(path, "rev-parse", "--git-dir", check=False)
    return proc.returncode == 0


class RecordingRun:
    """The scripted ``Run`` seam: records every spawn; answers from ``script``
    ((predicate, CompletedProcess-or-callable) pairs, first match wins), else
    delegates to the REAL ``default_run``."""

    def __init__(self, script: Sequence[tuple] = ()) -> None:
        self.calls: list[list[str]] = []
        self.script = list(script)

    def __call__(self, argv, env=None, timeout=None):
        from hive.app.sync import default_run

        argv = [str(a) for a in argv]
        self.calls.append(argv)
        for pred, result in self.script:
            if pred(argv):
                return result(argv) if callable(result) else result
        return default_run(argv, env=env, timeout=timeout)


def completed(argv=(), rc: int = 0, stdout: str = "", stderr: str = ""):
    import subprocess

    return subprocess.CompletedProcess(list(argv), rc, stdout=stdout, stderr=stderr)


def is_mint_argv(argv: Sequence[str]) -> bool:
    return any("hive-edge" in a for a in argv[:1]) and "mint" in argv


# ── token / fingerprint helpers ───────────────────────────────────────────────

_VERSION_TOKEN = re.compile(r"^([A-Za-z0-9_.-]+/)(\d+)(:.*)$", re.DOTALL)


def bump_token_version(token: str, to: int = 999) -> str:
    """Rewrite a versioned fingerprint token's envelope (``name/N:body`` →
    ``name/999:body``) so a version-gated comparator must treat it as
    incomparable (BUG-037)."""
    m = _VERSION_TOKEN.match(token)
    assert m is not None, f"token {token[:40]!r} does not carry a version envelope"
    return f"{m.group(1)}{to}{m.group(3)}"


def insert_verify_audit(
    store: SqliteEpisodeStore,
    episode_id: int,
    kind: str,
    *,
    ts: int,
    head_sha: str = "cafe" * 10,
    ref: str = "main",
) -> None:
    """A ledger row shaped like the census ingest's verify/verified rows (the
    server-written evidence the retirement gate + established rung read)."""
    body = {
        "schema": "verify/v1" if kind.startswith("verify") else "outcome_verified/v1",
        "stamp": {"head_sha": head_sha},
        "ref": ref,
    }
    store.insert_audit(episode_id, kind, "census", ts, json.dumps(body))


def dataclass_field_names(cls) -> set[str]:
    return {f.name for f in dataclasses.fields(cls)}
