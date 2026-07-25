"""The v3 sync arming contract (§6 step 5, D2).

WHICH repos the daemon feeds is OPERATIONAL data (the store registry), not boot
config: ``SyncConfig`` carries only the loop's own knobs ({interval_s,
webhook_secret, mirror_dir} — repo_url/token/verify_candidates are GONE), a
leftover ``HIVE_SYNC__REPO_URL``/``TOKEN``/``VERIFY_CANDIDATES`` or
``HIVE_CENSUS__CANONICAL_REF`` env var never REACHES ``SyncConfig`` (it degrades
to the unknown-field/group WARN — never a live switch here, never a crash), and
``webhook_secret`` is STANDALONE-valid (no pairing rule). What the config surface
cannot say is that such a key is inert everywhere: ``HIVE_SYNC__TOKEN`` is read
per tick by ``sync.py:_resolve_token`` as the fleet-default git credential, on a
path that never travels through ``Config`` at all. ``start_sync`` ALWAYS starts
the daemon thread — an empty registry is an inert tick, not an unarmed daemon, so
registering the first repo needs no restart.
"""

from __future__ import annotations

import dataclasses
import threading
import time

import pytest

import hive.app.sync as sync_mod
from hive.app.config import Config, SyncConfig
from hive.domain.change_evidence import ChangeEvidenceService

from tests.sync.conftest import meta, register_repo


def _root_cfg(tmp_path, **sync_kw) -> Config:
    return Config.load(
        db_path=":memory:",
        env={},
        sync={"mirror_dir": str(tmp_path / "mirrors"), **sync_kw},
    )


def _evidence(store) -> ChangeEvidenceService:
    return ChangeEvidenceService(
        reader=store, appender=store, now=lambda: 424242, ranges=store
    )


# ── the config group: v3 fields only, deleted knobs degrade to WARN ───────────
def test_sync_config_v3_surface():
    assert {f.name for f in dataclasses.fields(SyncConfig)} == {
        "interval_s",
        "webhook_secret",
        "mirror_dir",
        "drift_per_tick",
        "backfill_per_tick",
        "workers",
    }
    with pytest.raises(ValueError):
        SyncConfig(interval_s=4)  # the floor is 5


def test_capacity_knobs_default_and_validate():
    """The three throughput knobs: shipped defaults, a floor of 1 each, and env
    coercion through the HIVE_SYNC__ namespace. They bound spawn/thread COUNT
    only — no verdict rides them (a capped tick carries the rest over)."""
    cfg = SyncConfig()
    assert (cfg.drift_per_tick, cfg.backfill_per_tick, cfg.workers) == (200, 200, 1)
    for knob in ("drift_per_tick", "backfill_per_tick", "workers"):
        with pytest.raises(ValueError):
            SyncConfig(**{knob: 0})
    loaded = Config.load(
        db_path=":memory:",
        env={
            "HIVE_SYNC__DRIFT_PER_TICK": "500",
            "HIVE_SYNC__BACKFILL_PER_TICK": "400",
            "HIVE_SYNC__WORKERS": "8",
        },
    )
    assert loaded.sync.drift_per_tick == 500
    assert loaded.sync.backfill_per_tick == 400
    assert loaded.sync.workers == 8
    # webhook_secret is standalone-valid — no repo_url pairing rule exists
    cfg = Config.load(
        db_path=":memory:", env={"HIVE_SYNC__WEBHOOK_SECRET": "hook-credential"}
    )
    assert cfg.sync.webhook_secret == "hook-credential"


def test_deleted_env_vars_never_reach_syncconfig():
    """The v2 arming vars are structurally gone FROM THE CONFIG SURFACE: they
    coerce to nothing here, flip nothing here, and the config loads at pure
    defaults. This says nothing about whether another component reads one —
    ``HIVE_SYNC__TOKEN`` is live on the daemon's own env path."""
    cfg = Config.load(
        db_path=":memory:",
        env={
            "HIVE_SYNC__REPO_URL": "https://example.invalid/o/r.git",
            "HIVE_SYNC__TOKEN": "tok-credential",
            "HIVE_SYNC__VERIFY_CANDIDATES": "true",
            "HIVE_CENSUS__CANONICAL_REF": "main",
        },
    )
    assert cfg.sync == SyncConfig()
    assert not hasattr(cfg.sync, "repo_url")
    assert not hasattr(cfg.sync, "token")
    assert not hasattr(cfg.sync, "verify_candidates")
    assert not hasattr(cfg, "census")


# ── start_sync: always a thread; the registry is the only arming ──────────────
def test_start_sync_always_starts_thread_empty_registry_inert(store, tmp_path):
    """No registry rows: the thread still starts (registering a repo later needs
    no restart) and its ticks are inert — no mirror dir ever appears."""
    thread = sync_mod.start_sync(
        _root_cfg(tmp_path), store, _evidence(store), threading.Lock()
    )
    try:
        assert isinstance(thread, threading.Thread)
        assert thread.daemon and thread.is_alive()
        thread.sync_nudge.set()  # force one extra tick
        time.sleep(0.2)
        assert not (tmp_path / "mirrors").exists()  # inert: nothing cloned
    finally:
        thread.sync_stop.set()
        thread.sync_nudge.set()
        thread.join(timeout=30)
    assert not thread.is_alive()


def test_registered_repo_is_cloned_by_the_running_daemon(origin, store, tmp_path):
    """The registry arms the daemon: a registered repo's mirror appears at
    ``<mirror_dir>/<name>-<url-digest>`` and its watermark lands, driven by the REAL
    ``start_sync`` thread (control events carried for the webhook door)."""
    register_repo(store, "alpha", origin.url)
    thread = sync_mod.start_sync(
        _root_cfg(tmp_path, interval_s=5), store, _evidence(store), threading.Lock()
    )
    try:
        mirror = tmp_path / "mirrors" / sync_mod.mirror_dirname("alpha", origin.url)
        deadline = time.time() + 30
        while time.time() < deadline and meta(store, "sync:alpha:last_tip") is None:
            time.sleep(0.05)
        assert (mirror / ".git").exists()  # the first tick cloned it
        assert meta(store, "sync:alpha:last_tip") == origin.origin_sha(
            "refs/heads/main"
        )
    finally:
        thread.sync_stop.set()
        thread.sync_nudge.set()
        thread.join(timeout=30)
    assert not thread.is_alive()


def test_start_sync_threads_lifecycle_through(store, tmp_path):
    """The optional promotion-sweep handle rides start_sync into the service."""
    marker = object()
    thread = sync_mod.start_sync(
        _root_cfg(tmp_path), store, _evidence(store), threading.Lock(), lifecycle=marker
    )
    try:
        assert thread.sync_service._lifecycle is marker
    finally:
        thread.sync_stop.set()
        thread.sync_nudge.set()
        thread.join(timeout=30)
