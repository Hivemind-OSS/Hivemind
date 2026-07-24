"""SqliteEpisodeStore range-ledger seam (RangeLedger port): the census feed's durable
``(repo, base_sha, head_sha, phase)``-keyed range dedupe. Exact-key equality ONLY —
overlapping ranges are distinct keys (legacy A..B + B..C alongside a sync A..C all land;
transition noise is tolerated, never reasoned about) — and ``record_ingested_range`` is
idempotent-bool: True iff a NEW row landed; the first write stands, its ts never
refreshed. Durability across restarts is the point of the table: a re-ingest after a
process restart must still be skipped."""

from __future__ import annotations

from hive.adapters.sqlite_db import connect
from hive.adapters.store_sqlite import SqliteEpisodeStore
from hive.domain.ports import RangeLedger

BASE, HEAD, NEXT = "a" * 40, "b" * 40, "c" * 40
REPO = "https://github.com/acme/widgets"


def _store(path: str = ":memory:") -> SqliteEpisodeStore:
    return SqliteEpisodeStore(connect(str(path)))  # ledger-only: no index needed


# ── port conformance (the Law-7 isinstance idiom) ─────────────────────────────


def test_real_store_satisfies_range_ledger():
    assert isinstance(_store(), RangeLedger)


# ── the exact-key membership read ──────────────────────────────────────────────


def test_unknown_range_is_not_already_ingested():
    assert _store().already_ingested_range(REPO, BASE, HEAD, "post_merge") is False


def test_record_then_already_ingested_is_true():
    s = _store()
    assert s.record_ingested_range(REPO, BASE, HEAD, "post_merge", 100) is True
    assert s.already_ingested_range(REPO, BASE, HEAD, "post_merge") is True


def test_every_key_axis_discriminates():
    s = _store()
    s.record_ingested_range(REPO, BASE, HEAD, "post_merge", 100)
    assert not s.already_ingested_range("other-repo", BASE, HEAD, "post_merge")
    assert not s.already_ingested_range(REPO, NEXT, HEAD, "post_merge")
    assert not s.already_ingested_range(REPO, BASE, NEXT, "post_merge")
    assert not s.already_ingested_range(REPO, BASE, HEAD, "pre_merge")


# ── record: idempotent-bool, first write stands ────────────────────────────────


def test_record_is_idempotent_bool_and_first_write_stands():
    s = _store()
    assert s.record_ingested_range(REPO, BASE, HEAD, "post_merge", 100) is True
    assert s.record_ingested_range(REPO, BASE, HEAD, "post_merge", 999) is False
    rows = s.conn.execute("SELECT ts FROM ingested_ranges").fetchall()
    assert [r["ts"] for r in rows] == [100]  # never refreshed, never doubled


def test_ff_merge_pre_and_post_phases_are_distinct_keys():
    # an FF merge yields the SAME (base, head) range pre- AND post-merge — phase in
    # the key keeps both receipts landing instead of the second being absorbed.
    s = _store()
    assert s.record_ingested_range(REPO, BASE, HEAD, "pre_merge", 100) is True
    assert s.record_ingested_range(REPO, BASE, HEAD, "post_merge", 101) is True


def test_overlapping_ranges_are_distinct_keys_no_subsumption():
    # legacy hooks feed A..B then B..C while sync feeds A..C: three distinct keys, all
    # land — the ledger holds NO range-subsumption logic (transition noise tolerated).
    s = _store()
    assert s.record_ingested_range(REPO, BASE, HEAD, "post_merge", 1) is True  # A..B
    assert s.record_ingested_range(REPO, HEAD, NEXT, "post_merge", 2) is True  # B..C
    assert s.already_ingested_range(REPO, BASE, NEXT, "post_merge") is False  # A..C new
    assert s.record_ingested_range(REPO, BASE, NEXT, "post_merge", 3) is True


def test_legacy_empty_repo_is_its_own_identity():
    # a legacy receipt carries no repo key ⇒ repo "" — it dedupes against other ""
    # receipts and never collides with a repo-identified range.
    s = _store()
    assert s.record_ingested_range("", BASE, HEAD, "post_merge", 5) is True
    assert s.already_ingested_range("", BASE, HEAD, "post_merge") is True
    assert s.already_ingested_range(REPO, BASE, HEAD, "post_merge") is False
    assert s.record_ingested_range("", BASE, HEAD, "post_merge", 6) is False


# ── durability: the skip survives a process restart ───────────────────────────


def test_recorded_range_survives_a_restart(tmp_path):
    db = str(tmp_path / "ledger.db")
    s1 = _store(db)
    assert s1.record_ingested_range(REPO, BASE, HEAD, "post_merge", 100) is True
    s1.conn.close()
    s2 = _store(db)  # a fresh process, same file
    assert s2.already_ingested_range(REPO, BASE, HEAD, "post_merge") is True
    assert s2.record_ingested_range(REPO, BASE, HEAD, "post_merge", 200) is False
