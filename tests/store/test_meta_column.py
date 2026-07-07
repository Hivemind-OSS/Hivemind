"""The `meta` carrier column — roundtrip, DDL default, dedup no-merge, and the ONE
explicit additive migration (`ALTER TABLE episodes ADD COLUMN meta ...`).

Migration posture: the refuse-sentinel for PRE-LIFECYCLE tables is UNCHANGED (those
stores still need a clean start), but a lifecycle-current table that merely predates
`meta` is migrated in place — additive, defaulted, loudly logged, one column, one
direction. Refuse-and-reset here would brick `hive restore` of any pre-meta backup
(the restored file would re-trigger the refusal), so the additive path is the
correctness fix, not a convenience. Connections come from the prod ``connect()``
factory (deferred-isolation hand-rolled conns diverge from the BEGIN IMMEDIATE
discipline the store is written against).
"""
from __future__ import annotations

import logging

import pytest

from hive.adapters.sqlite_db import connect
from hive.adapters.store_sqlite import SqliteEpisodeStore
from hive.domain.models import content_hash

# The lifecycle-current episodes table as it shipped BEFORE the meta column —
# verbatim snapshot (deliberately not imported/derived) used ONLY to prove the
# additive migration fires on a real pre-meta store shape.
_PRE_META_EPISODES_SCHEMA = """
CREATE TABLE IF NOT EXISTS episodes(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id TEXT NOT NULL,
  text TEXT NOT NULL, value BLOB,
  weight REAL NOT NULL, ts INTEGER NOT NULL,
  provenance TEXT NOT NULL DEFAULT 'agent_reasoned'
    CHECK(provenance IN ('agent_reasoned', 'artifact_ingested', 'human')),
  tags TEXT,
  content_hash TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('pending','approved')),
  proposed_by TEXT, approved_by TEXT, approved_ts INTEGER,
  version INTEGER NOT NULL DEFAULT 0,
  trust TEXT NOT NULL DEFAULT 'quarantined',
  superseded_by INTEGER,
  last_active_ts INTEGER NOT NULL DEFAULT 0,
  polarity TEXT NOT NULL DEFAULT 'neutral' CHECK(polarity IN ('do','dont','neutral')),
  kind TEXT NOT NULL DEFAULT 'note' CHECK(kind IN ('bug', 'contract', 'convention',
    'dead_end', 'design_choice', 'env_fact', 'gotcha', 'note')),
  anchor TEXT NOT NULL DEFAULT '');
"""

# The pre-trust episodes table (no lifecycle columns) — the shape the sentinel REFUSES.
_PRE_LIFECYCLE_EPISODES_SCHEMA = """
CREATE TABLE IF NOT EXISTS episodes(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id TEXT NOT NULL, text TEXT NOT NULL, value BLOB,
  weight REAL NOT NULL, ts INTEGER NOT NULL, source TEXT, tags TEXT,
  content_hash TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('pending','approved')),
  proposed_by TEXT, approved_by TEXT, approved_ts INTEGER,
  version INTEGER NOT NULL DEFAULT 0);
"""


def _cols(conn) -> dict[str, dict]:
    return {r["name"]: dict(r) for r in conn.execute("PRAGMA table_info(episodes)")}


# ── roundtrip + DDL default ───────────────────────────────────────────────────
def test_episode_meta_roundtrip_and_default():
    conn = connect(":memory:")
    s = SqliteEpisodeStore(conn)
    carrier = '{"combdrift/fp":"combdrift-fp/1:abc"}'
    eid, deduped = s.stage(text="with meta", weight=1.0, tags="",
                           proposed_by="a", meta=carrier)
    assert not deduped
    assert s.get_episode(eid).meta == carrier            # byte-equal roundtrip
    eid2, _ = s.stage(text="without meta", weight=1.0, tags="", proposed_by="a")
    assert s.get_episode(eid2).meta == ""                # absent ⇒ the "" default
    # the DDL default holds on a raw INSERT that names no meta column
    conn.execute(
        "INSERT INTO episodes(tenant_id, text, weight, ts, content_hash, status) "
        "VALUES('default', 'raw row', 1.0, 0, ?, 'pending')",
        (content_hash("raw row"),))
    r = conn.execute("SELECT meta FROM episodes WHERE text='raw row'").fetchone()
    assert r["meta"] == ""
    m = _cols(conn)["meta"]
    assert m["dflt_value"] == "''" and m["notnull"] == 1


def test_dedup_recapture_preserves_existing_meta_unmerged():
    # identity is the text hash alone: a re-stage of identical text returns the
    # existing row UNCHANGED — meta is never merged/overwritten (immutability; a
    # new meta needs new text or supersession).
    conn = connect(":memory:")
    s = SqliteEpisodeStore(conn)
    eid, _ = s.stage(text="same text", weight=1.0, tags="", proposed_by="a",
                     meta='{"a/b":"first"}')
    eid2, deduped = s.stage(text="same text", weight=1.0, tags="", proposed_by="b",
                            meta='{"a/b":"second"}')
    assert (eid2, deduped) == (eid, True)
    assert s.get_episode(eid).meta == '{"a/b":"first"}'


# ── the additive migration ────────────────────────────────────────────────────
class _ListHandler(logging.Handler):
    """Captures records directly on the store logger — immune to whatever
    propagate/handler state other tests leave on the ``hive`` logger tree."""
    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def test_store_migrates_pre_meta_table_additively():
    conn = connect(":memory:")
    conn.executescript(_PRE_META_EPISODES_SCHEMA)
    text = "an old row from before the meta column"
    conn.execute(
        "INSERT INTO episodes(tenant_id, text, weight, ts, content_hash, status) "
        "VALUES('default', ?, 1.0, 5, ?, 'approved')", (text, content_hash(text)))
    log = logging.getLogger("hive.store")
    handler = _ListHandler()
    log.addHandler(handler)
    try:
        s = SqliteEpisodeStore(conn)                     # must NOT raise
        assert any("migrate_add_meta" in r.getMessage() for r in handler.records)
        m = _cols(conn)["meta"]
        assert m["dflt_value"] == "''" and m["notnull"] == 1
        ep = s.get_episode(1)
        assert ep is not None and ep.meta == ""          # existing rows read back ""
        # idempotent: a second construction migrates nothing and logs nothing
        handler.records.clear()
        SqliteEpisodeStore(conn)
        assert not any("migrate_add_meta" in r.getMessage() for r in handler.records)
    finally:
        log.removeHandler(handler)


def test_pre_lifecycle_table_still_refused_and_never_migrated():
    # regression pin on the sentinel: the refusal fires BEFORE the additive
    # migration, so a pre-lifecycle table is refused untouched (no meta bolted on).
    conn = connect(":memory:")
    conn.executescript(_PRE_LIFECYCLE_EPISODES_SCHEMA)
    with pytest.raises(RuntimeError, match="clean"):
        SqliteEpisodeStore(conn)
    assert "meta" not in _cols(conn)
