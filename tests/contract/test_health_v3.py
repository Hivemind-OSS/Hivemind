"""CT-13 — health worklists in the v3 shape (plan §4 intent 13, §6 step 4).

``hive_health(include_*)``: census_health serves ``{"repos": per-repo blocks keyed by
registry name, "fleet": the sync daemon's own state}``; conflicts bucket by (repo,
anchor) and carry their repo; the suspect-consensus / stale-suspects / gaps / trends
worklists still serve; miss scope rides the gap report; every probe stays fail-open
([]/{} — never a broken health).
"""

from __future__ import annotations

from tests.contract.conftest import (
    call,
    make_rig,
    payload,
    recall,
    register_repo,
    write_ok,
)
from tests.fakes._fakes import FakeClusterProvider


def _health(rig, **flags) -> dict:
    return payload(call(rig.server, "hive_health", flags or None))


def test_base_snapshot_still_serves(rig):
    env = _health(rig)
    assert env.get("ok") is True
    for key in ("n_episodes", "trust_counts", "n_misses_7d", "uptime_s"):
        assert key in env, f"the base snapshot keeps {key!r}: {sorted(env)}"


def test_flags_stay_byte_inert_when_unset(rig):
    env = _health(rig)
    for key in (
        "gaps",
        "trends",
        "conflicts",
        "suspect_consensus",
        "stale_suspects",
        "census_health",
    ):
        assert key not in env, f"{key!r} must not serve without its flag"


def test_census_health_is_per_repo_blocks_beside_a_fleet_block(rig):
    register_repo(rig.store, "alpha", "https://example.invalid/alpha.git")
    register_repo(rig.store, "beta", "https://example.invalid/beta.git")
    env = _health(rig, include_census_health=True)
    block = env.get("census_health")
    assert isinstance(block, dict), f"census_health serves two slots: {block}"
    assert set(block) == {"repos", "fleet"}, (
        f"per-repo blocks and the daemon's own state get separate homes — every key "
        f"of the repo map is a repo name, so a fleet fact has nowhere else: "
        f"{sorted(block)}"
    )
    repos = block["repos"]
    assert set(repos) == {"alpha", "beta"}, (
        f"one block per REGISTERED repo, keyed by name: {sorted(repos)}"
    )
    for name, sub in repos.items():
        assert isinstance(sub, dict), f"{name} block is a dict: {sub}"
    assert "days_since_last_change_outcome" not in repos, (
        "the flat single-repo shape is gone"
    )
    assert set(block["fleet"]) == {"last_sync_ts", "last_error"}, (
        f"the fleet block states the tick shell's own health: {block['fleet']}"
    )


def test_census_health_empty_registry_block(rig):
    env = _health(rig, include_census_health=True)
    block = env.get("census_health")
    assert block == {
        "repos": {},
        "fleet": {"last_sync_ts": None, "last_error": None},
    }, (
        f"an EMPTY registry serves an empty per-repo map; the fleet block still "
        f"answers, null where its meta is genuinely absent: {block}"
    )


def test_conflicts_bucket_by_repo_and_anchor(tmp_path):
    rig = make_rig(tmp_path, embedder=FakeClusterProvider(d=32))
    register_repo(rig.store, "alpha", "https://example.invalid/alpha.git")
    register_repo(rig.store, "beta", "https://example.invalid/beta.git")
    write_ok(
        rig.server,
        "cid=21 do use the connection pool for reads",
        polarity="do",
        anchors=[{"repo": "alpha", "anchor": "db/pool.py::acquire"}],
    )
    write_ok(
        rig.server,
        "cid=21 dont use the connection pool for reads",
        polarity="dont",
        anchors=[{"repo": "alpha", "anchor": "db/pool.py::acquire"}],
    )
    # the SAME near-dup pair split across two repos must NOT co-bucket
    write_ok(
        rig.server,
        "cid=22 do vendor the schema file",
        polarity="do",
        anchors=[{"repo": "alpha", "anchor": "pkg/schema.py::load"}],
    )
    write_ok(
        rig.server,
        "cid=22 dont vendor the schema file",
        polarity="dont",
        anchors=[{"repo": "beta", "anchor": "pkg/schema.py::load"}],
    )
    env = _health(rig, include_conflicts=True)
    entries = env.get("conflicts")
    assert isinstance(entries, list) and entries, (
        f"the co-anchored contradiction must surface: {entries}"
    )
    for entry in entries:
        assert "repo" in entry and "anchor" in entry, (
            f"v3 conflict entries bucket by (repo, anchor) and carry both: {entry}"
        )
    assert any(
        e.get("repo") == "alpha" and e.get("anchor") == "db/pool.py::acquire"
        for e in entries
    ), f"the alpha-bucketed pair surfaces with its repo: {entries}"
    assert not any(e.get("anchor") == "pkg/schema.py::load" for e in entries), (
        f"a pair split ACROSS repos shares no (repo, anchor) bucket: {entries}"
    )


def test_miss_scope_rides_the_gap_report(rig):
    register_repo(rig.store, "alpha", "https://example.invalid/alpha.git")
    for _ in range(3):
        env = recall(
            rig.server, "how do i warm the alpha cache safely", repos=["alpha"]
        )
        assert not env.get("reference_context")
    env = _health(rig, include_gaps=True)
    gaps = env.get("gaps")
    assert isinstance(gaps, list) and gaps, f"the unmet demand must surface: {gaps}"
    assert any("repos" in g for g in gaps), (
        f"gap entries carry the misses' repo scope: {gaps}"
    )
    assert any("alpha" in (g.get("repos") or []) for g in gaps), (
        f"the alpha-scoped demand names alpha: {gaps}"
    )


def test_trends_and_suspects_still_serve(rig):
    env = _health(
        rig,
        include_trends=True,
        include_suspect_consensus=True,
        include_stale_suspects=True,
    )
    assert isinstance(env.get("trends"), dict)
    assert isinstance(env.get("suspect_consensus"), list)
    assert isinstance(env.get("stale_suspects"), list)


def test_gap_probe_fault_fails_open_to_empty(rig, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(rig.store, "misses_detail_window", boom, raising=False)
    env = _health(rig, include_gaps=True)
    assert env.get("ok") is True, "a worklist fault never breaks health"
    assert env.get("gaps") == [], "the faulted probe degrades to []"
