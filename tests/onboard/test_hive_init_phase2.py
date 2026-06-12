"""P1.12 / M07 — hive_init phase-2: the lie-proof, idempotent, content-hash confirm.

``test_phase2_stale_hash_refused`` ★ is the A-invariant B lacked (mutation: flip the
``!=``→``==`` compare → red). The link persists via the existing meta kv UPSERT (no new
table).
"""
from __future__ import annotations

from hive.adapters.sqlite_db import connect
from hive.adapters.store_sqlite import SqliteEpisodeStore
from hive.app.onboard import HOOK_MANIFEST, InstallPlanner


def _store():
    return SqliteEpisodeStore(connect(":memory:"))


def _planner(store, *, trailer="Hive-Trace"):
    return InstallPlanner(store, stamp_trailer=trailer)


def _link_key(repo):
    return f"hive_init:link:{repo}"


def test_phase2_good_hash_links(tmp_path):
    store = _store()
    p = _planner(store)
    plan = p.plan(str(tmp_path), "generic")
    res = p.confirm(str(tmp_path), plan.expected_confirm_hash)
    assert res["linked"] is True
    assert res["link"]["block_hash"] == plan.expected_confirm_hash.hex()
    assert res["link"]["trailer_key"] == "Hive-Trace"
    # persisted as ONE namespaced kv blob (no new table)
    assert store.meta_get(_link_key(tmp_path)) is not None
    tables = {r["name"] for r in store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "bootstrap" not in tables and "links" not in tables   # no dedicated table


def test_phase2_stale_hash_refused(tmp_path):
    store = _store()
    p = _planner(store)
    res = p.confirm(str(tmp_path), b"\x00" * 32)          # a hash that was never rendered
    assert res["linked"] is False
    assert res["error"] == "stale_or_wrong_block"
    assert store.meta_get(_link_key(tmp_path)) is None    # ZERO rows written on mismatch


def test_phase2_idempotent_relink(tmp_path):
    store = _store()
    p = _planner(store)
    plan = p.plan(str(tmp_path), "generic")
    r1 = p.confirm(str(tmp_path), plan.expected_confirm_hash)
    blob1 = store.meta_get(_link_key(tmp_path))
    r2 = p.confirm(str(tmp_path), plan.expected_confirm_hash)
    blob2 = store.meta_get(_link_key(tmp_path))
    assert r1["linked"] is True and r2["linked"] is True
    assert blob1 == blob2                                 # re-link is a no-op UPSERT


def test_link_status_roundtrip(tmp_path):
    store = _store()
    p = _planner(store)
    assert p.link_status(str(tmp_path)) == (False, None)
    plan = p.plan(str(tmp_path), "generic")
    p.confirm(str(tmp_path), plan.expected_confirm_hash)
    linked, link = p.link_status(str(tmp_path))
    assert linked is True and link["block_version"] == 1


def test_phase2_distinct_repos_independent(tmp_path):
    store = _store()
    p = _planner(store)
    r1 = (tmp_path / "r1"); r1.mkdir()
    r2 = (tmp_path / "r2"); r2.mkdir()
    plan1 = p.plan(str(r1), "generic")
    p.confirm(str(r1), plan1.expected_confirm_hash)
    # r2 not confirmed yet ⇒ independent unlinked
    assert p.link_status(str(r1))[0] is True
    assert p.link_status(str(r2)) == (False, None)


def test_phase2_link_stamps_manifest_version_and_tier(tmp_path):
    import json
    store = _store()
    p = _planner(store)
    plan = p.plan(str(tmp_path), "claude-code")
    res = p.confirm(str(tmp_path), plan.expected_confirm_hash, "claude-code")
    assert res["linked"] is True
    assert res["link"]["manifest_version"] == HOOK_MANIFEST.manifest_version
    assert res["link"]["tier"] == 2 and res["link"]["harness"] == "claude-code"
    # the manifest-version-and-tier stamp lands in the persisted meta blob too (no new table)
    blob = json.loads(store.meta_get(_link_key(tmp_path)))
    assert blob["tier"] == 2 and blob["manifest_version"] == HOOK_MANIFEST.manifest_version
