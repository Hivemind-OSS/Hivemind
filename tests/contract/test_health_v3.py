"""CT-13 — health worklists in the v3 shape (plan §4 intent 13, §6 step 4).

``hive_health(include_*)``: census_health serves ``{"repos": per-repo blocks keyed by
registry name, "fleet": the sync daemon's own state}``; conflicts bucket by (repo,
anchor) and carry their repo; the suspect-consensus / stale-suspects / gaps / trends
worklists still serve; miss scope rides the gap report; every probe stays fail-open
([]/{} — never a broken health).
"""

from __future__ import annotations

from hive.app.sync_keys import fleet_last_error_key

from tests.contract.conftest import (
    Origin,
    call,
    make_rig,
    make_syncer,
    meta_value,
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


# ── the per-repo block never alleges a fault it did not measure (I9) ─────────


def _dark_but_live(rig, name: str = "alpha") -> dict:
    """A repo whose daemon is demonstrably TICKING — watermark advancing, no error,
    backfill counter climbing — and which simply has no ``change_outcome`` row yet.
    The shape observed on a healthy fleet that nevertheless read as stalled."""
    register_repo(rig.store, name, f"https://example.invalid/{name}.git")
    rig.store.meta_set(f"sync:{name}:last_tip", "a" * 40)
    rig.store.meta_set(f"sync:{name}:last_sync_ts", str(int(rig.clock.now())))
    rig.store.meta_set(f"sync:{name}:backfilled_total", "17")
    env = _health(rig, include_census_health=True)
    return env["census_health"]["repos"][name]["sync"]


def test_live_daemon_with_no_change_outcome_never_reports_a_fault(rig):
    """The false alarm itself: nothing in this block was measured about the
    daemon's liveness, so nothing in it may allege the daemon has stalled."""
    sync = _dark_but_live(rig)
    assert sync["last_error"] is None and sync["backfilled_total"] == 17, (
        f"the fixture must be a demonstrably LIVE feed: {sync}"
    )
    serialized = " ".join(str(v) for v in sync.values())
    for alleged in ("stalled", "stall", "stuck", "dead", "down"):
        assert alleged not in serialized.lower(), (
            f"a block that measured only evidence-darkness cannot allege a daemon "
            f"fault ({alleged!r}): {sync}"
        )


def test_dark_block_states_the_measured_fact_not_a_verdict(rig):
    """The key keeps its slot and its present-only-when-true idiom — only the CLAIM
    changes, from a verdict about the daemon to the fact actually measured."""
    sync = _dark_but_live(rig)
    assert "status" in sync, "the key keeps its slot — the fact is still worth serving"
    assert sync["status"] == "no change_outcome evidence yet", (
        f"the string states what was measured: {sync['status']!r}"
    )


def test_a_healed_fault_serves_null_last_error_beside_a_fresh_last_sync_ts(
    rig, tmp_path
):
    """Replaces the sticky-``last_error`` pin (BUGS-098/099/100 plan, S1):
    ``last_error`` non-null ⇔ the most recent completed tick of its scope faulted.
    A repo tick that faults and then heals must serve ``last_error: null`` — the
    key is DELETED, byte-identical to never-faulted, never ``""`` — beside a fresh
    ``last_sync_ts``. The sticky design served a PAST fault as PRESENT state:
    BUG-099 was filed against a demonstrably healthy feed on exactly that misread."""
    late_root = tmp_path / "remote-late"
    register_repo(rig.store, "alpha", str(late_root / "origin.git"))
    syncer = make_syncer(rig.store, tmp_path)
    syncer.service.tick()  # the remote does not exist yet: this tick faults
    sync = _health(rig, include_census_health=True)["census_health"]["repos"]["alpha"][
        "sync"
    ]
    assert sync["last_error"], "precondition: the faulted tick surfaced its error"
    assert sync["last_sync_ts"] is None, "a faulted tick never stamps freshness"

    Origin(late_root)  # the remote comes up in place; the fault heals itself
    syncer.service.tick()  # fault-free
    sync = _health(rig, include_census_health=True)["census_health"]["repos"]["alpha"][
        "sync"
    ]
    assert sync["last_error"] is None, (
        f"a clean tick must clear its own scope's error — a served fossil asserts "
        f"an outage on a healthy feed (BUG-099): {sync}"
    )
    assert sync["last_sync_ts"] is not None, "the clean tick stamps freshness"
    assert meta_value(rig.store, "sync:alpha:last_error") is None, (
        "cleared means DELETED — an empty-string tombstone would mint a third state"
    )


def test_a_persistent_fault_serves_last_error_on_every_read(rig, tmp_path):
    """The other half of presence-means-current: while the fault PERSISTS, the key
    is re-stamped by every faulted tick — present on every poll beside a frozen
    ``last_sync_ts`` — so a live outage still reads as one."""
    register_repo(rig.store, "alpha", str(tmp_path / "never-exists.git"))
    syncer = make_syncer(rig.store, tmp_path)
    syncer.service.tick()
    syncer.service.tick()
    sync = _health(rig, include_census_health=True)["census_health"]["repos"]["alpha"][
        "sync"
    ]
    assert sync["last_error"], "a still-faulting repo keeps its error on every read"
    assert sync["last_sync_ts"] is None, "and its freshness stamp stays withheld"


def test_fleet_last_error_clears_on_a_clean_shell_independent_of_repo_faults(
    rig, tmp_path
):
    """The fleet key answers for the tick SHELL alone (registry read + prune): a
    shell-fault-free tick clears it even while a repo is faulting — the repo's own
    key still says so — or the healed-shell fossil just moves the misread one level
    up. The faulted repo still withholds the fleet freshness stamp."""
    rig.store.meta_set(
        fleet_last_error_key(), "registry: OperationalError: transient (healed since)"
    )
    register_repo(rig.store, "alpha", str(tmp_path / "missing.git"))
    make_syncer(rig.store, tmp_path).service.tick()
    block = _health(rig, include_census_health=True)["census_health"]
    assert block["fleet"]["last_error"] is None, (
        f"the shell ran fault-free — its healed fault must not survive a repo's "
        f"unrelated one: {block['fleet']}"
    )
    assert block["repos"]["alpha"]["sync"]["last_error"], (
        "the repo's own current fault still stands on the repo's own key"
    )
    assert block["fleet"]["last_sync_ts"] is None, (
        "clearing the shell's error is not claiming the fleet synced"
    )


def test_a_shell_fault_stamps_the_fleet_key_while_a_clean_repo_clears_its_own(
    rig, tmp_path, monkeypatch
):
    """The inverse independence arm: a prune fault is the SHELL's — it lands on the
    fleet key — while the registered repo's clean tick clears that repo's own healed
    residue in the same cycle. Neither scope's verdict leaks into the other."""
    import shutil

    healthy = Origin(tmp_path / "remote-b")
    register_repo(rig.store, "beta", healthy.url, canonical_ref="main")
    rig.store.meta_set("sync:beta:last_error", "mirror: residue of a healed fault")
    (tmp_path / "mirrors" / "gone").mkdir(parents=True)  # a deregistered leftover

    def boom(path, *args, **kwargs):
        raise OSError(f"device busy: {path}")

    monkeypatch.setattr(shutil, "rmtree", boom)
    make_syncer(rig.store, tmp_path).service.tick()
    block = _health(rig, include_census_health=True)["census_health"]
    fleet_error = block["fleet"]["last_error"] or ""
    assert "prune" in fleet_error, (
        f"the shell fault lands on the fleet key: {block['fleet']}"
    )
    assert block["repos"]["beta"]["sync"]["last_error"] is None, (
        f"beta's OWN tick ran clean — its healed residue is cleared regardless of "
        f"the shell's fault: {block['repos']['beta']['sync']}"
    )


def test_census_health_regression_pins_bug060_and_bug062(rig):
    """The reordered function must not silently drop its neighbours: a re-registered
    repo still serves NO sync block (BUG-060), and the fleet slot still answers
    independently of every per-repo block (BUG-062)."""
    register_repo(rig.store, "alpha", "https://example.invalid/alpha.git")
    rig.store.meta_set("sync:alpha:last_tip", "a" * 40)
    rig.store.repo_remove("alpha")
    rig.store.repo_add(
        name="alpha", url="https://example.invalid/alpha.git", added_ts=9
    )
    rig.store.meta_set("sync:last_error", "registry: RuntimeError: store is gone")
    block = _health(rig, include_census_health=True)["census_health"]
    assert "sync" not in block["repos"]["alpha"], (
        "a re-registered repo has no feed state until its first tick (BUG-060)"
    )
    assert block["fleet"]["last_error"] == "registry: RuntimeError: store is gone", (
        f"the daemon's own fault belongs to no repo and still serves (BUG-062): "
        f"{block['fleet']}"
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
