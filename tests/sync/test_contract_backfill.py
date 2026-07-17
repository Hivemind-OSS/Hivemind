"""CT-7 — the backfill-mint contract (intent 7).

A completed sync fills ABSENT fingerprint keys on approved code-anchored episodes:
the sweep selects carriers lacking ``combdrift/fp``, the mirror worktree is synced
to the tracked tip, and each anchor is minted through the REAL ``hive-edge`` CLI
subprocess (the one mint owner fleet-wide), merging under the store's absent-only
guard with ``hive-sync/minted`` provenance in the exact
``hive-sync-minted/1:server@<tip> <ref>`` format. Tagged episodes are untouched
byte-for-byte; unresolvable anchors skip silently with the loop alive; the cap
carries over; backfilled keys are BYTE-EQUAL to a direct edge mint on the same
tree — asserted against a second real ``hive-edge mint`` subprocess, never a mock.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from tests.sync.conftest import (
    ANCHOR, harness_env, make_service, meta, seed_episode,
)

META_LAST_ERROR = "sync:last_error"
META_BACKFILLED_TOTAL = "sync:backfilled_total"


def _edge_cli() -> str:
    sibling = Path(sys.executable).parent / "hive-edge"
    return str(sibling) if sibling.exists() else "hive-edge"


def episode_meta(store, eid: int) -> dict:
    raw = store.conn.execute(
        "SELECT meta FROM episodes WHERE id=?", (eid,)).fetchone()["meta"]
    return json.loads(raw) if raw else {}


def direct_mint(repo: Path, anchor: str, home: Path) -> dict:
    """The reference mint: the same real CLI an edge teammate runs, with its OWN
    state home — fully independent of the server-side code path under test."""
    env = dict(harness_env())
    env["HIVE_EDGE_HOME"] = str(home)
    proc = subprocess.run([_edge_cli(), "mint", "--repo", str(repo),
                           "--anchor", anchor],
                          capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_fills_absent(origin, store, tmp_path):
    eid = seed_episode(store, "greet growls on empty names", ANCHOR)
    before = store.conn.execute(
        "SELECT text, content_hash, value FROM episodes WHERE id=?", (eid,)).fetchone()
    svc = make_service(origin, store, tmp_path)
    svc.tick()
    tip = origin.origin_sha("refs/heads/main")

    filled = episode_meta(store, eid)
    assert filled["combdrift/fp"].startswith("combdrift-fp/")
    assert filled["matrix/subgraph_fp"].startswith("matrix-subgraph-fp/")
    # the provenance rides in the exact format — character for character
    assert filled["hive-sync/minted"] == f"hive-sync-minted/1:server@{tip} main"
    after = store.conn.execute(
        "SELECT text, content_hash, value FROM episodes WHERE id=?", (eid,)).fetchone()
    assert (after["text"], after["content_hash"], after["value"]) == \
        (before["text"], before["content_hash"], before["value"])   # meta ONLY
    assert meta(store, META_BACKFILLED_TOTAL) == "1"
    assert meta(store, META_LAST_ERROR) is None


def test_never_overwrites(origin, store, tmp_path):
    pinned = '{"combdrift/fp":"combdrift-fp/1:human-pinned"}'
    tagged = seed_episode(store, "tagged memory", ANCHOR, meta=pinned)
    partial_pin = "matrix-subgraph-fp/1:" + "0" * 64
    partial = seed_episode(
        store, "partially tagged memory", ANCHOR,
        meta=json.dumps({"matrix/subgraph_fp": partial_pin}, separators=(",", ":")))
    svc = make_service(origin, store, tmp_path)
    svc.tick()

    # carrying combdrift/fp ⇒ not swept at all: the carrier is byte-identical —
    # no re-mint, no provenance stamp, nothing
    raw = store.conn.execute(
        "SELECT meta FROM episodes WHERE id=?", (tagged,)).fetchone()["meta"]
    assert raw == pinned
    # a swept row's PRESENT key survives byte-for-byte while absent keys fill —
    # the absent-only merge guard (breaking it in fill_absent_meta reds here)
    got = episode_meta(store, partial)
    assert got["matrix/subgraph_fp"] == partial_pin
    assert got["combdrift/fp"].startswith("combdrift-fp/")
    assert got["hive-sync/minted"].startswith("hive-sync-minted/1:server@")
    assert meta(store, META_LAST_ERROR) is None


def test_skips_unresolvable(origin, store, tmp_path):
    ghost = seed_episode(store, "anchored to code that is gone", "no/such/file.py:ghost")
    live = seed_episode(store, "greet growls on empty names", ANCHOR)
    svc = make_service(origin, store, tmp_path)
    svc.tick()

    assert episode_meta(store, ghost) == {}              # skipped silently: no keys,
    assert meta(store, META_LAST_ERROR) is None          # no provenance, no error
    assert "combdrift/fp" in episode_meta(store, live)   # the loop stayed alive past it
    svc.tick()                                           # and the skip never wedges
    assert episode_meta(store, ghost) == {}
    assert meta(store, META_LAST_ERROR) is None


def test_matches_edge_mint(origin, store, tmp_path):
    """Byte-equivalence: the fp keys the backfill writes are EXACTLY what a direct
    edge mint yields on the same tree — including after the tip MOVES, so the
    server must mint the TIP tree, never the clone-time checkout."""
    svc = make_service(origin, store, tmp_path)
    svc.tick()                                           # clone + baseline at seed tip
    origin.commit(
        "app.py",
        'def greet(name, punct):\n    return "hi " + name + punct\n', "widen greet")
    origin.push()
    tip = origin.origin_sha("refs/heads/main")
    eid = seed_episode(store, "greet growls on empty names", ANCHOR)
    svc.tick()                                           # fetch + backfill at the NEW tip

    reference = direct_mint(origin.work, ANCHOR, tmp_path / "edge-home-direct")
    assert reference, "the reference anchor must resolve"
    got = episode_meta(store, eid)
    for key, value in reference.items():
        assert got[key] == value                         # byte-equal, key by key
    assert got["hive-sync/minted"] == f"hive-sync-minted/1:server@{tip} main"
    assert meta(store, META_LAST_ERROR) is None


def test_cap_carries_over(origin, store, tmp_path, monkeypatch):
    import hive.app.sync as sync_mod
    monkeypatch.setattr(sync_mod, "_BACKFILL_PER_TICK", 1)
    first = seed_episode(store, "first anchored memory", ANCHOR)
    second = seed_episode(store, "second anchored memory", ANCHOR)
    svc = make_service(origin, store, tmp_path)

    svc.tick()                                           # one slot: lowest id fills
    assert "combdrift/fp" in episode_meta(store, first)
    assert episode_meta(store, second) == {}             # bounded — the rest waits
    svc.tick()                                           # the remainder carries over
    assert "combdrift/fp" in episode_meta(store, second)
    assert meta(store, META_BACKFILLED_TOTAL) == "2"
    assert meta(store, META_LAST_ERROR) is None
