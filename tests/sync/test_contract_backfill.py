"""The mint-backfill contract (v3 §6 step 5, CT-5's unit twin).

A tick fills ABSENT fingerprint carriers on approved anchor bindings of the
repo: the sweep is ``anchors_lacking_fp`` (EMPTY-carrier rows only — a pinned
carrier is structurally out of the sweep), the mirror worktree is synced to the
canonical tip, and each anchor is minted through the REAL ``hive-edge`` CLI
subprocess (the one mint owner fleet-wide), merging under the store's
absent-only guard with ``hive-sync/minted`` provenance in the exact
``hive-sync-minted/1:server@<tip> <ref>`` format into
``episode_anchors.fp_meta``. Pinned carriers are untouched byte-for-byte;
unresolvable anchors skip silently with the loop alive; the cap carries over;
backfilled keys are BYTE-EQUAL to a direct edge mint on the same tree —
asserted against a second real ``hive-edge mint`` subprocess, never a mock.

NOTE: the FROZEN suite (tests/contract/test_server_mint.py) imports
``direct_mint`` from this module — it may grow but never rename.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from hive.app.census_health import census_health_report

from tests.sync.conftest import (
    ANCHOR,
    REPO,
    anchor_fp,
    harness_env,
    make_service,
    meta,
    register_repo,
    seed_episode,
)

META_LAST_ERROR = f"sync:{REPO}:last_error"
# per-repo, not a fleet global: the counter is what census_health serves in THIS
# repo's block, so it must be keyed by the repo that earned it (BUG-059).
META_BACKFILLED_TOTAL = f"sync:{REPO}:backfilled_total"
FP_KEY = "combdrift/fp"
SUBGRAPH_KEY = "matrix/subgraph_fp"
PROVENANCE_KEY = "hive-sync/minted"


def _edge_cli() -> str:
    sibling = Path(sys.executable).parent / "hive-edge"
    return str(sibling) if sibling.exists() else "hive-edge"


def direct_mint(repo: Path, anchor: str, home: Path) -> dict:
    """The reference mint: the same real CLI an edge teammate runs, with its OWN
    state home — fully independent of the server-side code path under test."""
    env = dict(harness_env())
    env["HIVE_EDGE_HOME"] = str(home)
    proc = subprocess.run(
        [_edge_cli(), "mint", "--repo", str(repo), "--anchor", anchor],
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _armed(origin, store, tmp_path, **kw):
    register_repo(store, REPO, origin.url)
    return make_service(store, tmp_path, **kw)


def test_fills_absent_carrier(origin, store, tmp_path):
    eid = seed_episode(store, "greet growls on empty names")
    before = store.conn.execute(
        "SELECT text, content_hash, value FROM episodes WHERE id=?", (eid,)
    ).fetchone()
    svc = _armed(origin, store, tmp_path)
    svc.tick()
    tip = origin.origin_sha("refs/heads/main")

    filled = anchor_fp(store, eid)
    assert filled[FP_KEY].startswith("combdrift-fp/")
    assert filled[SUBGRAPH_KEY].startswith("matrix-subgraph-fp/")
    # the provenance rides in the exact format — character for character
    assert filled[PROVENANCE_KEY] == f"hive-sync-minted/1:server@{tip} main"
    after = store.conn.execute(
        "SELECT text, content_hash, value FROM episodes WHERE id=?", (eid,)
    ).fetchone()
    assert (after["text"], after["content_hash"], after["value"]) == (
        before["text"],
        before["content_hash"],
        before["value"],
    )  # anchor carrier ONLY
    assert meta(store, META_BACKFILLED_TOTAL) == "1"
    assert meta(store, META_LAST_ERROR) is None


def test_real_tick_populates_every_served_health_field(origin, store, tmp_path):
    """The writer→reader seam END TO END (BUG-059), the one check neither suite made:
    run a REAL tick, then read the block an operator actually reads. Every advertised
    field must carry data the daemon itself wrote — no field seeded by this test."""
    seed_episode(store, "greet growls on empty names")  # gives the mint leg work
    _armed(origin, store, tmp_path).tick()

    sync_block = census_health_report(store)[REPO]["sync"]
    assert sync_block["configured"] is True
    assert sync_block["tracked_ref"] == "main"
    assert sync_block["last_tip"] == origin.origin_sha("refs/heads/main")
    assert sync_block["last_sync_ts"] is not None
    assert sync_block["last_error"] is None
    assert sync_block["backfilled_total"] == 1
    # and nothing advertised is left structurally empty — the bug's exact signature
    assert not [
        field
        for field, value in sync_block.items()
        if field != "last_error" and value in (None, 0)
    ]


def test_pinned_carrier_never_swept_or_displaced(origin, store, tmp_path):
    """A row carrying ANY fp_meta is out of the sweep entirely (the v3
    empty-carrier-only selection): a full pin AND a partial pin both survive
    byte-for-byte — no re-mint, no provenance stamp, nothing."""
    full_pin = '{"combdrift/fp":"combdrift-fp/1:human-pinned"}'
    partial_pin = json.dumps(
        {SUBGRAPH_KEY: "matrix-subgraph-fp/1:" + "0" * 64}, separators=(",", ":")
    )
    tagged = seed_episode(store, "tagged memory")
    partial = seed_episode(store, "partially tagged memory")
    store.conn.execute(
        "UPDATE episode_anchors SET fp_meta=? WHERE episode_id=?", (full_pin, tagged)
    )
    store.conn.execute(
        "UPDATE episode_anchors SET fp_meta=? WHERE episode_id=?",
        (partial_pin, partial),
    )
    svc = _armed(origin, store, tmp_path)
    svc.tick()

    raw_tagged = store.conn.execute(
        "SELECT fp_meta FROM episode_anchors WHERE episode_id=?", (tagged,)
    ).fetchone()["fp_meta"]
    raw_partial = store.conn.execute(
        "SELECT fp_meta FROM episode_anchors WHERE episode_id=?", (partial,)
    ).fetchone()["fp_meta"]
    assert raw_tagged == full_pin  # byte-identical: never revisited
    assert raw_partial == partial_pin
    assert meta(store, META_BACKFILLED_TOTAL) is None
    assert meta(store, META_LAST_ERROR) is None


def test_skips_unresolvable_silently_loop_alive(origin, store, tmp_path):
    ghost = seed_episode(
        store, "anchored to code that is gone", "no/such/file.py::ghost"
    )
    live = seed_episode(store, "greet growls on empty names")
    svc = _armed(origin, store, tmp_path)
    svc.tick()

    assert anchor_fp(store, ghost, REPO, "no/such/file.py::ghost") == {}
    assert meta(store, META_LAST_ERROR) is None  # no provenance, no error
    assert FP_KEY in anchor_fp(store, live)  # the loop stayed alive past it
    svc.tick()  # and the skip never wedges
    assert anchor_fp(store, ghost, REPO, "no/such/file.py::ghost") == {}
    assert meta(store, META_LAST_ERROR) is None


def test_matches_edge_mint_at_the_moved_tip(origin, store, tmp_path):
    """Byte-equivalence: the fp keys the backfill writes are EXACTLY what a direct
    edge mint yields on the same tree — including after the tip MOVES, so the
    server must mint the TIP tree, never the clone-time checkout."""
    svc = _armed(origin, store, tmp_path)
    svc.tick()  # clone + baseline at seed tip
    origin.commit(
        "app.py",
        'def greet(name, punct):\n    return "hi " + name + punct\n',
        "widen greet",
    )
    origin.push()
    tip = origin.origin_sha("refs/heads/main")
    eid = seed_episode(store, "greet growls on empty names")
    svc.tick()  # fetch + backfill at the NEW tip

    reference = direct_mint(origin.work, ANCHOR, tmp_path / "edge-home-direct")
    assert reference, "the reference anchor must resolve"
    got = anchor_fp(store, eid)
    for key, value in reference.items():
        assert got[key] == value  # byte-equal, key by key
    assert got[PROVENANCE_KEY] == f"hive-sync-minted/1:server@{tip} main"
    assert meta(store, META_LAST_ERROR) is None


def test_cap_carries_over(origin, store, tmp_path):
    first = seed_episode(store, "first anchored memory")
    second = seed_episode(store, "second anchored memory")
    svc = _armed(origin, store, tmp_path, backfill_per_tick=1)

    svc.tick()  # one slot: lowest id fills
    assert FP_KEY in anchor_fp(store, first)
    assert anchor_fp(store, second) == {}  # bounded — the rest waits
    svc.tick()  # the remainder carries over
    assert FP_KEY in anchor_fp(store, second)
    assert meta(store, META_BACKFILLED_TOTAL) == "2"
    assert meta(store, META_LAST_ERROR) is None
