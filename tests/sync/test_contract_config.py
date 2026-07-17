"""CT-1 — the sync arming contract (intent 1).

Given env with repo_url[+credential] ⇒ sync arms (a live daemon thread through the
REAL ``start_sync``); a half-set credential without repo_url ⇒ EX_CONFIG at boot,
the refusal naming the vars on OPERATOR-VISIBLE STDERR at the real boot path (never
the credential value); NO ``HIVE_SYNC__*`` at all ⇒ the boot is byte-inert — same
thread set, no census/matrix import, ``start_sync`` → None, a clean stdout (the
JSON-RPC channel), the config group at pure defaults. The byte-inert case is the
MASTER check for the whole subsystem.
"""
from __future__ import annotations

import sys
import threading
import time
import types
from pathlib import Path

import pytest

import hive.app.sync as sync_mod
from hive.app.config import Config, SyncConfig
from hive.domain.change_evidence import ChangeEvidenceService
from hive.tools import entrypoint as E

from tests.sync.conftest import meta


# ── a minimal conformant Boot fake (the entrypoint test-double idiom) ──────────
class _MetaStore:
    def __init__(self) -> None:
        self.meta: dict[str, str] = {}

    def meta_set(self, key: str, value: str) -> None:
        self.meta[key] = value


class _FakeBoot:
    def __init__(self) -> None:
        self.store = _MetaStore()
        self.token_store = types.SimpleNamespace(verify=lambda tok: None)
        self.server = object()

    def migrate(self) -> None: ...
    def build_index(self) -> None: ...

    def warm_embedder(self):
        return types.SimpleNamespace(loaded=True)

    def make_server(self):
        return self.server


def _boot_factory(boot):
    def build_boot(cfg, *, tenant_id, agent_id):
        return boot
    return build_boot


# ── armed: repo_url [+credential] ⇒ a live sync daemon ────────────────────────
def test_armed(origin, store, tmp_path):
    """repo_url (+ a credential, legal WITH repo_url) arms sync: start_sync returns a
    live daemon thread whose first tick clones the mirror; the thread carries its
    control events (the nudge/stop seam the webhook door will use)."""
    mirror = tmp_path / "mirror"
    env = {"HIVE_SYNC__REPO_URL": origin.url,
           "HIVE_SYNC__TOKEN": "seat-credential",
           "HIVE_SYNC__MIRROR_DIR": str(mirror),
           "HIVE_SYNC__INTERVAL_S": "5"}
    cfg = Config.load(db_path=":memory:", env=env)
    assert cfg.sync.repo_url == origin.url          # the config layer is armed
    evidence = ChangeEvidenceService(reader=store, appender=store,
                                     now=lambda: 424242, ranges=store)
    thread = sync_mod.start_sync(cfg, store, evidence, threading.Lock())
    try:
        assert isinstance(thread, threading.Thread)
        assert thread.daemon and thread.is_alive()
        deadline = time.time() + 30
        while time.time() < deadline and not (mirror / ".git").exists():
            time.sleep(0.05)
        assert (mirror / ".git").exists()           # the first tick ran: mirror cloned
    finally:
        thread.sync_stop.set()
        thread.sync_nudge.set()
        thread.join(timeout=30)
    assert not thread.is_alive()


# ── half-set: a credential without repo_url refuses at boot (EX_CONFIG) ───────
@pytest.mark.parametrize("halfset,secret", [
    ({"HIVE_SYNC__TOKEN": "tok-credential"}, "tok-credential"),
    ({"HIVE_SYNC__WEBHOOK_SECRET": "hook-credential"}, "hook-credential"),
])
def test_partial_fails_ex_config(halfset, secret, capsys):
    assembled: list = []

    def build_boot(cfg, *, tenant_id, agent_id):
        assembled.append(True)
        return _FakeBoot()

    rc = E.main(env=halfset, build_boot=build_boot, serve=lambda s: None)
    assert rc == E.EX_CONFIG == 78
    assert assembled == []                          # refused BEFORE assembly
    # the BOOT-path refusal reaches operator-visible STDERR naming both vars — the
    # missing one to set AND the offending one to unset (an operator staring at a
    # container exiting 78 must know which var to fix); the credential VALUE never
    # rides the line.
    err = capsys.readouterr().err
    (offending_var,) = halfset
    assert "entrypoint.config_invalid" in err
    assert "HIVE_SYNC__REPO_URL" in err
    assert offending_var in err
    assert secret not in err
    # and the in-process raise (the boot line's source) carries the same naming
    with pytest.raises(ValueError, match="HIVE_SYNC__REPO_URL") as ei:
        Config.load(db_path=":memory:", env=halfset)
    assert secret not in str(ei.value)


# ── unset: the MASTER byte-inert check ─────────────────────────────────────────
def test_unset_byte_inert(monkeypatch, capsys):
    """A boot with NO HIVE_SYNC__* is byte-identical to the pre-sync build: the real
    start_sync returns None (no thread object at all), the process thread set gains
    nothing, no census/matrix/verifier module is imported by the boot, stdout (the
    JSON-RPC channel) stays clean, and the sync config group sits at pure defaults."""
    returned: list = []
    real_start_sync = sync_mod.start_sync

    def recording(cfg, store, evidence, lock):
        out = real_start_sync(cfg, store, evidence, lock)
        returned.append(out)
        return out

    monkeypatch.setattr(sync_mod, "start_sync", recording)
    threads_before = set(threading.enumerate())
    modules_before = set(sys.modules)
    rc = E.main(env={}, build_boot=_boot_factory(_FakeBoot()), serve=lambda s: None)
    assert rc == E.EX_OK
    assert returned == [None]                       # unarmed ⇒ None, never a thread
    assert set(threading.enumerate()) - threads_before == set()
    grown = {m for m in set(sys.modules) - modules_before
             if m.startswith(("hive.census", "hive.verifier", "matrix", "combdrift"))}
    assert grown == set()                           # no census machinery was imported
    assert capsys.readouterr().out == ""            # stdout is the JSON-RPC channel
    cfg = Config.load(db_path=":memory:", env={})
    assert cfg.sync == SyncConfig()                 # pure defaults; repo_url "" = unarmed
