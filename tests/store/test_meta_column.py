"""The `meta` carrier column — roundtrip, DDL default, and dedup no-merge.

v3 migration posture: the ONE additive `meta` migration is GONE — every pre-meta table
shape also carries the retired anchor/provenance/tags columns, so it now refuses at
construction under the no-migration rule (a v3 store is a clean start; `hive restore`
of a pre-v3 backup refuses identically). Connections come from the prod ``connect()``
factory (deferred-isolation hand-rolled conns diverge from the BEGIN IMMEDIATE
discipline the store is written against)."""

from __future__ import annotations

import pytest

from hive.adapters.sqlite_db import connect
from hive.adapters.store_sqlite import SqliteEpisodeStore
from hive.domain.models import content_hash

# The lifecycle-current episodes table as it shipped BEFORE the meta column — verbatim
# snapshot (deliberately not imported/derived). In v2 this shape was migrated additively;
# in v3 it REFUSES (it carries the retired columns — no in-place path exists).
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


def _cols(conn) -> dict[str, dict]:
    return {r["name"]: dict(r) for r in conn.execute("PRAGMA table_info(episodes)")}


# ── roundtrip + DDL default ───────────────────────────────────────────────────
def test_episode_meta_roundtrip_and_default():
    conn = connect(":memory:")
    s = SqliteEpisodeStore(conn)
    carrier = '{"combdrift/fp":"combdrift-fp/1:abc"}'
    eid, deduped = s.stage(text="with meta", weight=1.0, proposed_by="a", meta=carrier)
    assert not deduped
    assert s.get_episode(eid).meta == carrier  # byte-equal roundtrip
    eid2, _ = s.stage(text="without meta", weight=1.0, proposed_by="a")
    assert s.get_episode(eid2).meta == ""  # absent ⇒ the "" default
    # the DDL default holds on a raw INSERT that names no meta column
    conn.execute(
        "INSERT INTO episodes(tenant_id, text, weight, ts, content_hash, status) "
        "VALUES('default', 'raw row', 1.0, 0, ?, 'pending')",
        (content_hash("raw row"),),
    )
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
    eid, _ = s.stage(
        text="same text", weight=1.0, proposed_by="a", meta='{"a/b":"first"}'
    )
    eid2, deduped = s.stage(
        text="same text", weight=1.0, proposed_by="b", meta='{"a/b":"second"}'
    )
    assert (eid2, deduped) == (eid, True)
    assert s.get_episode(eid).meta == '{"a/b":"first"}'


# ── the v3 refusal posture on the pre-meta shape ──────────────────────────────
def test_pre_meta_table_is_refused_not_migrated():
    conn = connect(":memory:")
    conn.executescript(_PRE_META_EPISODES_SCHEMA)
    text = "an old row from before the meta column"
    conn.execute(
        "INSERT INTO episodes(tenant_id, text, weight, ts, content_hash, status) "
        "VALUES('default', ?, 1.0, 5, ?, 'approved')",
        (text, content_hash(text)),
    )
    with pytest.raises(RuntimeError, match="migration"):
        SqliteEpisodeStore(conn)  # the additive path is DEAD
    assert "meta" not in _cols(conn)  # refused untouched, no bolt-on
