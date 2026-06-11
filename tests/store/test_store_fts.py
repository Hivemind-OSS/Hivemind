"""Store-owned FTS5 mirror of the servable set: the probe, the four in-tx sync
sites (complete / set_trust / supersede / sweep_decayed), ``search_text`` (the
LexicalIndex port), and ``rebuild_fts`` (boot self-heal).

The mirror is asserted exclusively through ``search_text`` after each trust
transition — membership IS the observable contract (AC4). Hostile FTS5-syntax
queries must never raise (AC7); a stripped-SQLite store degrades to
``fts_enabled=False`` with every write path intact.
"""
from __future__ import annotations

import sqlite3

import numpy as np

from hive.adapters.index_exhaustive import ExhaustiveCosineIndex
from hive.adapters.sqlite_db import connect
from hive.adapters.store_sqlite import SqliteEpisodeStore
from hive.domain.lifecycle import (
    DEPRECATED, ESTABLISHED, PROVISIONAL, QUARANTINED,
)
from hive.domain.ports import LexicalIndex

DIM = 4
NOW = 1_000
Q_TTL, P_TTL = 100, 200

_VECS = [np.eye(DIM, dtype=np.float32)[i] for i in range(DIM)]


def _store() -> SqliteEpisodeStore:
    return SqliteEpisodeStore(connect(":memory:"), index=ExhaustiveCosineIndex(DIM))


def _materialize(s: SqliteEpisodeStore, text: str, *, trust: str, vec=None,
                 ts: int = 10, last_active=None) -> int:
    eid, _ = s.stage(text=text, weight=1.0, source="m", tags="", proposed_by="writer", ts=ts)
    ok = s.complete(eid, vec if vec is not None else _VECS[0], expected_version=0,
                    trust=trust, approved_ts=ts,
                    last_active_ts=ts if last_active is None else last_active)
    assert ok
    return eid


def _fts_ids(s: SqliteEpisodeStore, query: str, k: int = 10) -> list[int]:
    return [eid for eid, _score in s.search_text(query, k)]


# ── the four sync sites, asserted via search_text after each transition (AC4) ──
def test_fts_mirrors_servable_membership():
    s = _store()
    assert s.fts_enabled is True

    # complete: quarantined ⇒ absent; established/provisional ⇒ present
    q = _materialize(s, "quarantined capacitor insight", trust=QUARANTINED, vec=_VECS[0])
    assert q not in _fts_ids(s, "capacitor")
    e = _materialize(s, "established capacitor wisdom", trust=ESTABLISHED, vec=_VECS[1])
    assert e in _fts_ids(s, "capacitor")
    p = _materialize(s, "provisional capacitor note", trust=PROVISIONAL, vec=_VECS[2])
    assert p in _fts_ids(s, "capacitor")

    # promote quarantined → provisional ⇒ present
    assert s.set_trust(q, PROVISIONAL, now=NOW) is True
    assert q in _fts_ids(s, "capacitor")

    # demote provisional → quarantined ⇒ absent
    assert s.set_trust(p, QUARANTINED, now=NOW) is True
    assert p not in _fts_ids(s, "capacitor")

    # supersede ⇒ target absent
    assert s.supersede(e, q, actor="human", ts=NOW) is True
    assert e not in _fts_ids(s, "capacitor")
    assert s.get_episode(e).trust == DEPRECATED

    # sweep: the promoted provisional (liveness NOW) lapses past its TTL ⇒ absent
    swept = s.sweep_decayed(now=NOW + P_TTL + 1, q_ttl_s=Q_TTL, p_ttl_s=P_TTL)
    assert swept[PROVISIONAL] >= 1
    assert q not in _fts_ids(s, "capacitor")
    assert _fts_ids(s, "capacitor") == []


def test_rebuild_fts_self_heals():
    s = _store()
    e = _materialize(s, "the durable flux memo", trust=ESTABLISHED, vec=_VECS[0])

    # drift one way: a servable row missing from the mirror (raw delete)
    s.conn.execute("DELETE FROM episodes_fts")
    assert e not in _fts_ids(s, "flux")

    # drift the other way: a quarantined row present in the mirror (raw insert)
    q = _materialize(s, "quarantined flux noise", trust=QUARANTINED, vec=_VECS[1])
    s.conn.execute(
        "INSERT INTO episodes_fts(rowid, text) SELECT id, text FROM episodes WHERE id=?",
        (q,))
    assert q in _fts_ids(s, "flux")

    # a provisional whose liveness lapsed before NOW must not be re-inserted
    lp = _materialize(s, "lapsed provisional flux", trust=PROVISIONAL,
                      vec=_VECS[2], last_active=0)

    s.rebuild_fts(now=NOW, provisional_ttl_s=P_TTL)
    ids = _fts_ids(s, "flux")
    assert e in ids and q not in ids and lp not in ids


def test_search_text_hostile_query_safe():
    s = _store()
    e = _materialize(s, "a real x memory about parens", trust=ESTABLISHED)
    for hostile in ('"a AND (', '-x', '"unbalanced', 'NOT', 'a* OR (b',
                    '(((', '"', '^x', 'col:val', '*'):
        out = s.search_text(hostile, 5)             # must never raise (AC7)
        assert isinstance(out, list)
    # the syntax is inert but the TOKENS still search: '-x AND (' → ["x","AND"]
    assert e in _fts_ids(s, "-x AND (")
    # token-less queries ⇒ [] (no MATCH issued)
    assert s.search_text("", 5) == []
    assert s.search_text("!!! ???", 5) == []
    # token-cap: a 200-token query is capped, never an engine error
    assert isinstance(s.search_text(" ".join(f"tok{i}" for i in range(200)), 5), list)


def test_store_satisfies_lexical_port():
    # the REAL adapter conforms — never only a fake (the protocol-widening lesson)
    s = _store()
    assert isinstance(s, LexicalIndex)
    a = _materialize(s, "cache eviction cache eviction cache", trust=ESTABLISHED,
                     vec=_VECS[0])
    b = _materialize(s, "cache mentioned once in a long note about other things",
                     trust=ESTABLISHED, vec=_VECS[1])
    out = s.search_text("cache", 5)
    assert [eid for eid, _ in out][0] == a          # higher tf ⇒ better BM25 ⇒ first
    assert b in [eid for eid, _ in out]
    assert all(isinstance(eid, int) and isinstance(sc, float) for eid, sc in out)
    scores = [sc for _eid, sc in out]
    assert scores == sorted(scores, reverse=True)   # (id, score) score-DESCENDING
    # k truncates
    assert len(s.search_text("cache", 1)) == 1


# ── stripped-SQLite degradation: fts_enabled=False, all paths intact ──────────
class _NoFtsConn:
    """The prod-factory connection with the FTS5 module 'stripped': the CREATE
    VIRTUAL TABLE probe raises, everything else passes through."""

    def __init__(self, real: sqlite3.Connection) -> None:
        self._real = real

    def execute(self, sql, *args):
        if "USING fts5" in sql:
            raise sqlite3.OperationalError("no such module: fts5")
        return self._real.execute(sql, *args)

    def executescript(self, script):
        return self._real.executescript(script)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_stripped_sqlite_degrades_to_disabled():
    s = SqliteEpisodeStore(_NoFtsConn(connect(":memory:")),
                           index=ExhaustiveCosineIndex(DIM))
    assert s.fts_enabled is False
    # every sync site is guarded: the full lifecycle runs with no FTS table
    e = _materialize(s, "life without fts", trust=ESTABLISHED)
    p = _materialize(s, "promoted later", trust=QUARANTINED, vec=_VECS[1])
    assert s.set_trust(p, PROVISIONAL, now=NOW) is True
    assert s.supersede(e, p, actor="h", ts=NOW) is True
    s.sweep_decayed(now=NOW + P_TTL + 1, q_ttl_s=Q_TTL, p_ttl_s=P_TTL)
    s.rebuild_fts(now=NOW, provisional_ttl_s=P_TTL)   # no-op, no raise
    assert s.search_text("life", 5) == []             # fail-open to dense


def test_search_text_disabled_returns_empty():
    s = _store()
    _materialize(s, "present but unreachable", trust=ESTABLISHED)
    s.fts_enabled = False
    assert s.search_text("unreachable", 5) == []
