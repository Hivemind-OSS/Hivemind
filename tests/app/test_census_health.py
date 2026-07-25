"""The passive census staleness signal — hive_health(include_census_health=true):
``{"repos": per-repo blocks keyed by registry name, "fleet": the tick shell's state}``.

Per registered repo: a fail-open report of how long ago the last SHA-bound
change_outcome evidence row landed FOR THAT REPO (attribution via the canonical
payload's ``repo`` key; the raw day-count, no invented staleness threshold — THEORY §9
#14), plus — when the sync daemon has left ``sync:<name>:*`` meta — a ``sync``
observability block. With sync configured, darkness reads ``status: "no change_outcome
evidence yet"`` — the measured fact, never a daemon verdict this reader cannot observe.
An empty registry serves ``repos == {}`` (never the old flat single-repo shape).

The ``fleet`` block is the home for what belongs to no repo (BUG-062): the daemon's own
``last_sync_ts`` / ``last_error``, read from the 2-part ``sync:<field>`` keys. It is
always present and each field is null exactly when its meta is absent. The two legs are
independent and both fail open (Law 6 — the side-channel never breaks hive_health): a
registry fault empties ``repos`` while ``fleet`` still serves the fault that caused it.
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

from hive.adapters.sqlite_db import connect
from hive.adapters.store_sqlite import SqliteEpisodeStore
from hive.app.census_health import census_health_report
from hive.domain.evidence_kinds import EK_CHANGE_OUTCOME, EK_OUTCOME_HELPED
from tests.mcp._helpers import (
    build_real_server,
    content,
    register_repo,
    tool_call,
)

_DAY_S = 86_400


def _store(*repos: str) -> SqliteEpisodeStore:
    store = SqliteEpisodeStore(connect(":memory:"))
    for i, name in enumerate(repos):
        store.repo_add(name=name, url=f"https://example.invalid/{name}.git", added_ts=i)
    return store


def _repos(store: Any) -> dict:
    return census_health_report(store)["repos"]


def _fleet(store: Any) -> dict:
    return census_health_report(store)["fleet"]


def _outcome_payload(repo: str | None) -> str:
    body = {"schema": "change_outcome/v1", "verdict": "pass"}
    if repo is not None:
        body["repo"] = repo
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


def _insert_outcome(store, ts: int, repo: str | None, kind: str = EK_CHANGE_OUTCOME):
    store.insert_audit(1, kind, "census", ts, _outcome_payload(repo))


# ── the two-slot report shape ─────────────────────────────────────────────────
def test_report_has_a_repos_map_and_a_fleet_block():
    # BUG-062: every key of the repo map IS a repo name, so a fleet fact needs a slot
    # of its own — a reserved sentinel key inside the map could collide with a real
    # block (`_fleet`, `daemon` and `sync` all pass the registry slug gate).
    store = _store("alpha")
    report = census_health_report(store)
    assert set(report) == {"repos", "fleet"}
    assert set(report["repos"]) == {"alpha"}


def test_a_repo_named_like_a_sentinel_cannot_collide_with_the_fleet_block():
    store = _store("_fleet", "sync", "daemon")
    report = census_health_report(store)
    assert set(report["repos"]) == {"_fleet", "sync", "daemon"}
    assert set(report["fleet"]) == {"last_sync_ts", "last_error"}


def test_empty_registry_serves_empty_block():
    store = _store()
    assert _repos(store) == {}, "an EMPTY registry serves {}, never the old flat shape"


def test_one_block_per_registered_repo_dark_by_default():
    store = _store("alpha", "beta")
    report = _repos(store)
    assert set(report) == {"alpha", "beta"}
    for block in report.values():
        assert block == {"days_since_last_change_outcome": None}


def test_change_outcome_attributes_to_its_payload_repo():
    store = _store("alpha", "beta")
    now = int(time.time())
    _insert_outcome(store, now - 3 * _DAY_S, "alpha")
    report = _repos(store)
    assert report["alpha"]["days_since_last_change_outcome"] == 3
    assert report["beta"]["days_since_last_change_outcome"] is None


def test_newest_change_outcome_wins_per_repo():
    store = _store("alpha")
    now = int(time.time())
    _insert_outcome(store, now - 10 * _DAY_S, "alpha")
    _insert_outcome(store, now - 2 * _DAY_S, "alpha")  # MAX(ts) wins
    assert _repos(store)["alpha"]["days_since_last_change_outcome"] == 2


def test_repo_less_legacy_row_attributes_to_nobody():
    # a pre-v3 payload with no "repo" key cannot be attributed — honest under-claim.
    store = _store("alpha")
    _insert_outcome(store, int(time.time()), None)
    assert _repos(store)["alpha"]["days_since_last_change_outcome"] is None


def test_only_change_outcome_kind_counts():
    store = _store("alpha")
    _insert_outcome(
        store, int(time.time()) - 5 * _DAY_S, "alpha", kind=EK_OUTCOME_HELPED
    )
    assert _repos(store)["alpha"]["days_since_last_change_outcome"] is None


# ── the per-repo sync observability block ─────────────────────────────────────
def test_sync_block_absent_without_per_repo_sync_meta():
    store = _store("alpha")
    assert "sync" not in _repos(store)["alpha"]


def test_two_part_sync_keys_belong_to_no_repo():
    # a 2-part key (sync:last_tip) carries no repo name — ignored by the per-repo
    # grouping, never misattributed to some repo.
    store = _store("alpha")
    store.meta_set("sync:last_tip", "a" * 40)
    assert "sync" not in _repos(store)["alpha"]


def test_sync_block_serves_the_six_keys_when_configured():
    store = _store("alpha")
    _insert_outcome(store, int(time.time()) - _DAY_S, "alpha")
    store.meta_set("sync:alpha:tracked_ref", "master")
    store.meta_set("sync:alpha:last_tip", "a" * 40)
    store.meta_set("sync:alpha:last_sync_ts", "111000")
    store.meta_set("sync:alpha:last_error", "mirror: _SyncFault: git clone failed")
    store.meta_set("sync:alpha:backfilled_total", "7")
    block = _repos(store)["alpha"]
    assert block["days_since_last_change_outcome"] == 1
    assert block["sync"] == {
        "configured": True,
        "tracked_ref": "master",
        "last_tip": "a" * 40,
        "last_sync_ts": "111000",  # served as stamped (the writer's seam-clock string)
        "last_error": "mirror: _SyncFault: git clone failed",
        "backfilled_total": 7,
    }


def test_reregistered_repo_serves_no_stale_sync_block():
    # BUG-060: re-registering a name (a documented operator flow) must not re-attach
    # the PREVIOUS incarnation's block — that reads as a passing connection the
    # daemon has never attempted.
    store = _store("alpha")
    store.meta_set("sync:alpha:tracked_ref", "master")
    store.meta_set("sync:alpha:last_tip", "a" * 40)
    store.meta_set("sync:alpha:last_sync_ts", "1000")
    store.repo_remove("alpha")
    store.repo_add(name="alpha", url="https://example.invalid/alpha.git", added_ts=9)
    block = _repos(store)["alpha"]
    assert "sync" not in block, (
        "a re-registered repo has no feed state until its first tick"
    )


def test_sync_meta_is_scoped_to_its_own_repo():
    store = _store("alpha", "beta")
    store.meta_set("sync:alpha:last_tip", "a" * 40)
    report = _repos(store)
    assert "sync" in report["alpha"]
    assert "sync" not in report["beta"]


def test_tracked_ref_and_last_sync_ts_null_only_when_meta_absent():
    store = _store("alpha")
    store.meta_set("sync:alpha:last_tip", "a" * 40)
    block = _repos(store)["alpha"]["sync"]
    assert block["tracked_ref"] is None
    assert block["last_sync_ts"] is None


def test_dark_sync_block_status_string():
    # the WIRE string: it names the fact the reader measured (no change_outcome row
    # for this repo yet), never a verdict about a daemon it never observed.
    store = _store("alpha")
    store.meta_set("sync:alpha:last_tip", "a" * 40)
    block = _repos(store)["alpha"]
    assert block["days_since_last_change_outcome"] is None
    assert block["sync"]["status"] == "no change_outcome evidence yet"


def test_live_and_dark_block_carries_no_fault_claim():
    # the observed false-alarm shape: watermark advancing, no error, counter climbing
    # — and still no change_outcome row. Nothing here measured daemon liveness, so
    # nothing here may allege a stall.
    store = _store("alpha")
    store.meta_set("sync:alpha:last_tip", "a" * 40)
    store.meta_set("sync:alpha:last_sync_ts", str(int(time.time())))
    store.meta_set("sync:alpha:backfilled_total", "17")
    sync = _repos(store)["alpha"]["sync"]
    assert sync["last_error"] is None and sync["backfilled_total"] == 17
    assert "stall" not in " ".join(str(v) for v in sync.values()).lower()


def test_status_is_present_only_when_dark():
    # status is present-only-when-dark (an unconditional attach is the mutation target).
    store = _store("alpha")
    _insert_outcome(store, int(time.time()), "alpha")
    store.meta_set("sync:alpha:last_tip", "a" * 40)
    assert "status" not in _repos(store)["alpha"]["sync"]


def test_sticky_last_error_does_not_resurrect_a_stall_claim():
    # last_error is sticky (no clean tick clears it), so it can never become the
    # condition for a verdict — it is served verbatim and the key stays factual.
    store = _store("alpha")
    store.meta_set("sync:alpha:last_tip", "a" * 40)
    store.meta_set("sync:alpha:last_error", "fetch: transient DNS failure")
    sync = _repos(store)["alpha"]["sync"]
    assert sync["last_error"] == "fetch: transient DNS failure"
    assert sync["status"] == "no change_outcome evidence yet"


def test_sync_counters_absent_or_garbage_read_null_never_zero():
    # BUG-059: a confident `0` for an unwired counter is indistinguishable from a
    # measured zero — the shape that hid the bug on a live server. No readable count
    # ⇒ null.
    store = _store("alpha")
    store.meta_set("sync:alpha:last_tip", "a" * 40)
    store.meta_set("sync:alpha:backfilled_total", "not-a-number")
    assert _repos(store)["alpha"]["sync"]["backfilled_total"] is None
    store.meta_set("sync:alpha:backfilled_total", "0")  # a REAL measured zero survives
    assert _repos(store)["alpha"]["sync"]["backfilled_total"] == 0


# ── the fleet block: the tick shell's own state ───────────────────────────────
def test_fleet_block_serves_null_until_the_daemon_stamps_anything():
    # honest absence, not an invented zero or empty string: null means "no such meta",
    # and the operator reads that as "the daemon has never got that far".
    store = _store("alpha")
    assert _fleet(store) == {"last_sync_ts": None, "last_error": None}


def test_fleet_block_is_present_even_on_an_empty_registry():
    # "is the daemon alive" is answerable with no repo registered — and a repo-less
    # tick is inert, so it never stamps last_sync_ts. That null is honest.
    assert _fleet(_store()) == {"last_sync_ts": None, "last_error": None}


def test_fleet_block_serves_the_tick_shell_keys_verbatim():
    store = _store("alpha")
    store.meta_set("sync:last_sync_ts", "111000")
    store.meta_set(
        "sync:last_error", "registry: OperationalError: no such table: repos"
    )
    assert _fleet(store) == {
        "last_sync_ts": "111000",  # as stamped (the writer's seam-clock string)
        "last_error": "registry: OperationalError: no such table: repos",
    }


def test_fleet_block_never_reads_a_repos_key():
    # the per-repo keys are 3-part; a fleet field must not pick one up by prefix.
    store = _store("alpha")
    store.meta_set("sync:alpha:last_error", "mirror: _SyncFault: git clone failed")
    store.meta_set("sync:alpha:last_sync_ts", "999")
    assert _fleet(store) == {"last_sync_ts": None, "last_error": None}


def test_fleet_and_repo_faults_are_reported_side_by_side():
    # the two surfaces answer different questions and neither substitutes for the
    # other: one repo failing is not the daemon failing, and vice versa.
    store = _store("alpha")
    store.meta_set("sync:alpha:last_error", "mirror: _SyncFault: git clone failed")
    store.meta_set("sync:last_error", "prune[gone]: OSError: device busy")
    report = census_health_report(store)
    assert report["repos"]["alpha"]["sync"]["last_error"].startswith("mirror:")
    assert report["fleet"]["last_error"].startswith("prune[gone]:")


# ── fail-open legs (Law 6: the side-channel never raises) ─────────────────────
class _RaisingRegistryStore:
    conn = None

    def repo_registry(self):
        raise RuntimeError("registry read exploded")


class _RaisingConnStore:
    """A registry that answers but a conn whose every read faults — each per-repo
    leg degrades independently (day-count None, no sync block)."""

    class _BadConn:
        def execute(self, *a, **k):
            raise sqlite3.OperationalError("conn is gone")

    conn = _BadConn()

    def repo_registry(self):
        class Row:
            name = "alpha"

        return [Row()]


def test_registry_fault_degrades_to_empty_repos_but_still_serves_fleet():
    # the leg independence that BUG-062 needs: a registry read that explodes is
    # precisely the fault the FLEET block exists to carry, so it must survive the leg
    # that the same fault emptied.
    report = census_health_report(_RaisingRegistryStore())
    assert report["repos"] == {}
    assert set(report["fleet"]) == {"last_sync_ts", "last_error"}


def test_conn_fault_degrades_to_dark_blocks():
    report = census_health_report(_RaisingConnStore())
    assert report["repos"] == {"alpha": {"days_since_last_change_outcome": None}}
    assert report["fleet"] == {"last_sync_ts": None, "last_error": None}


def test_closed_conn_degrades_to_empty_never_raises():
    store = _store("alpha")
    store.conn.close()
    assert census_health_report(store) == {  # the registry read faults first
        "repos": {},
        "fleet": {"last_sync_ts": None, "last_error": None},
    }


# ── the MCP surface (hive_health rides the report) ────────────────────────────
def test_health_serves_per_repo_blocks_over_mcp():
    server, _ = build_real_server()
    register_repo(server, "alpha")
    register_repo(server, "beta")
    server.store.meta_set("sync:alpha:last_tip", "c" * 40)
    snap = content(tool_call(server, "hive_health", {"include_census_health": True}))
    block = snap["census_health"]
    assert set(block) == {"repos", "fleet"}
    repos = block["repos"]
    assert set(repos) == {"alpha", "beta"}
    assert repos["alpha"]["sync"]["configured"] is True
    assert repos["alpha"]["sync"]["last_tip"] == "c" * 40
    assert repos["alpha"]["sync"]["status"] == "no change_outcome evidence yet"
    assert "sync" not in repos["beta"]
    assert "days_since_last_change_outcome" not in repos, (
        "the flat single-repo shape is gone"
    )


def test_health_serves_the_fleet_block_over_mcp():
    server, _ = build_real_server()
    register_repo(server, "alpha")
    server.store.meta_set("sync:last_error", "registry: RuntimeError: store is gone")
    block = content(tool_call(server, "hive_health", {"include_census_health": True}))[
        "census_health"
    ]
    assert block["fleet"]["last_error"] == "registry: RuntimeError: store is gone"
    assert block["fleet"]["last_sync_ts"] is None


def test_health_empty_registry_serves_empty_block_over_mcp():
    server, _ = build_real_server()
    snap = content(tool_call(server, "hive_health", {"include_census_health": True}))
    assert snap["census_health"]["repos"] == {}
    assert snap["census_health"]["fleet"] == {"last_sync_ts": None, "last_error": None}


def test_health_omits_census_health_by_default():
    # Byte-inert: flag omitted ⇒ key absent (dropping the flag gate ⇒ the always-emits mutation).
    server, _ = build_real_server()
    assert "census_health" not in content(tool_call(server, "hive_health", {}))


def test_census_health_flag_is_advertised():
    # advertised == enforced: the flag rides the description and the inputSchema, and
    # the (capped) description still fits under METADATA_FIELD_LIMIT.
    from hive.app.contract import METADATA_FIELD_LIMIT
    from hive.app.tool_defs import TOOL_DEFINITIONS

    health = next(t for t in TOOL_DEFINITIONS if t["name"] == "hive_health")
    assert "include_census_health" in health["description"]
    assert health["inputSchema"]["properties"]["include_census_health"] == {
        "type": "boolean"
    }
    assert len(health["description"]) <= METADATA_FIELD_LIMIT
