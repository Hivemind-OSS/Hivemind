"""CT-14 — a legacy (v2) store is refused at boot; schema v3 is a clean start
(plan §4 intent 14, §3.5, D7).

Boot refusal triggers on the PRESENCE of the legacy ``episodes`` columns
(``anchor``/``provenance``) — never serve a mixed-schema store; the message is
the clear no-migration message; a restored v2 backup refuses identically
(EX_SOFTWARE at the entrypoint); a fresh store boots with the full v3 table set.
"""

from __future__ import annotations

import sqlite3

import pytest

from hive.app.config import Config
from tests.contract.conftest import call, payload, table_exists

# The v2 episodes table VERBATIM as it ships today (with the legacy
# anchor/provenance/approver/tags columns) — deliberately a frozen snapshot, so a
# v3 store class can never accidentally create it.
V2_EPISODES_DDL = """
CREATE TABLE episodes(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id TEXT NOT NULL,
  text TEXT NOT NULL, value BLOB,
  weight REAL NOT NULL, ts INTEGER NOT NULL,
  provenance TEXT NOT NULL DEFAULT 'agent_reasoned',
  tags TEXT,
  content_hash TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('pending','approved')),
  proposed_by TEXT, approved_by TEXT, approved_ts INTEGER,
  version INTEGER NOT NULL DEFAULT 0,
  trust TEXT NOT NULL DEFAULT 'quarantined',
  superseded_by INTEGER,
  last_active_ts INTEGER NOT NULL DEFAULT 0,
  polarity TEXT NOT NULL DEFAULT 'neutral' CHECK(polarity IN ('do','dont','neutral')),
  kind TEXT NOT NULL DEFAULT 'note',
  anchor TEXT NOT NULL DEFAULT '',
  meta TEXT NOT NULL DEFAULT '');
"""

V3_REQUIRED_TABLES = (
    "episodes",
    "episode_anchors",
    "repos",
    "anchor_drift",
    "ref_requests",
    "evidence_events",
    "recall_misses",
    "ingested_ranges",
    "meta",
    "blobs",
    "exposure",
    "conflict_flags",
)

NO_MIGRATION_PATTERN = "migration"


def _write_v2_store(path) -> None:
    conn = sqlite3.connect(str(path))
    conn.executescript(V2_EPISODES_DDL)
    conn.commit()
    conn.close()


def _boot(db_path: str):
    from hive.app.container import build_container
    from tests.fakes._fakes import FakeClock, FakeWarmProvider

    cfg = Config.load(db_path=db_path, env={})
    container = build_container(
        cfg,
        tenant_id="ct",
        agent_id="ct-agent",
        embedder=FakeWarmProvider(d=8),
        clock=FakeClock(1000),
    )
    container.migrate()
    container.build_index()
    container.warm_embedder()
    return container


def test_v2_store_file_is_refused(tmp_path):
    db = tmp_path / "v2.db"
    _write_v2_store(db)
    with pytest.raises(RuntimeError, match=NO_MIGRATION_PATTERN) as excinfo:
        _boot(str(db))
    msg = str(excinfo.value)
    assert "clean store" in msg or "clean" in msg, (
        f"the refusal tells the operator to start from a clean store: {msg}"
    )


def test_restored_v2_backup_refused_with_the_same_message(tmp_path):
    # a `hive restore` of a v2 snapshot = the v2 file landing at the live path
    live = tmp_path / "restored.db"
    _write_v2_store(live)
    with pytest.raises(RuntimeError, match=NO_MIGRATION_PATTERN) as first:
        _boot(str(live))

    other = tmp_path / "restored-again.db"
    _write_v2_store(other)
    with pytest.raises(RuntimeError, match=NO_MIGRATION_PATTERN) as second:
        _boot(str(other))
    assert str(first.value) == str(second.value), (
        "every v2 refusal carries the SAME clear no-migration message"
    )


def test_entrypoint_maps_the_refusal_to_ex_software(tmp_path):
    from hive.app.container import build_container
    from hive.tools import entrypoint
    from tests.fakes._fakes import FakeWarmProvider

    db = tmp_path / "v2.db"
    _write_v2_store(db)

    def build_boot(cfg, *, tenant_id, agent_id):
        return build_container(
            cfg,
            tenant_id=tenant_id,
            agent_id=agent_id,
            embedder=FakeWarmProvider(d=8),
        )

    rc = entrypoint.main(
        [],
        env={"HIVE_STORE__DB_PATH": str(db)},
        build_boot=build_boot,
        serve=lambda s: None,
    )
    assert rc == 70, f"a legacy store at boot is EX_SOFTWARE (70), got {rc}"


def test_fresh_store_boots_with_the_full_v3_table_set(tmp_path):
    db = str(tmp_path / "fresh.db")
    container = _boot(db)
    env = payload(call(container.make_server(), "hive_health"))
    assert env.get("ok") is True
    for name in V3_REQUIRED_TABLES:
        assert table_exists(container.conn, name), (
            f"a fresh v3 boot creates {name!r} (plan §3.5)"
        )
