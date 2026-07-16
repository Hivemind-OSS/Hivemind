"""P1.13 / M12 — the entrypoint boot state machine (driving adapter).

The strict order config→migrate→index→warm→serve, the fail-fast exit codes
(78 EX_CONFIG / 70 EX_SOFTWARE / 69 EX_UNAVAILABLE), the embedder-resident gate, and the
"serve is UNREACHABLE unless every prior step succeeded" invariant are pinned here against
an injected fake boot — no Docker required. Two deliberate mutations live in this module
(swallow-embedder-exception, migrate-failure return).

Part S: the HTTP listen port (8765) and the rate-limit belt are FIXED; the one surviving
boot knob is `HIVE_HTTP_MAX_BODY_BYTES` (malformed→EX_CONFIG before assembly, never a raise
out of boot). `_make_http_serve` constructs a real `TokenBucketLimiter` from its fixed
defaults and threads `limiter=`/`max_body_bytes=` into `run_http_dual`.
"""
from __future__ import annotations

import pytest

from hive.app.rate_limit import TokenBucketLimiter
from hive.tools import entrypoint as E


# ── injected fakes ────────────────────────────────────────────────────────────
class _Embedder:
    def __init__(self, loaded: bool = True) -> None:
        self.loaded = loaded


class _MetaStore:
    def __init__(self) -> None:
        self.meta: dict[str, str] = {}

    def meta_set(self, key: str, value: str) -> None:
        self.meta[key] = value


class _TokenStore:
    """Boot.token_store stub — the HTTP daemon's verify seam. Present so the fake conforms to
    the widened Boot Protocol; the injected-serve paths never call it."""
    def verify(self, token):
        return None


class _RecordingBoot:
    """Records the order of boot calls; `fail_on` makes one step raise; `embedder`
    controls the warm result (loaded True/False)."""
    def __init__(self, calls: list, *, embedder=None, fail_on=None, store=None) -> None:
        self.calls = calls
        self._embedder = embedder if embedder is not None else _Embedder(loaded=True)
        self._fail_on = set(fail_on or ())
        self.store = store if store is not None else _MetaStore()
        self.token_store = _TokenStore()
        self.server = object()

    def migrate(self) -> None:
        self.calls.append("migrate")
        if "migrate" in self._fail_on:
            raise RuntimeError("migrate boom")

    def build_index(self) -> None:
        self.calls.append("build_index")
        if "build_index" in self._fail_on:
            raise RuntimeError("index boom")

    def warm_embedder(self):
        self.calls.append("warm")
        if "warm" in self._fail_on:
            raise RuntimeError("embedder dead")
        return self._embedder

    def make_server(self):
        self.calls.append("make_server")
        if "make_server" in self._fail_on:
            raise RuntimeError("assemble boom")
        return self.server


def _boot_factory(boot, captured=None):
    def build_boot(cfg, *, tenant_id, agent_id):
        if captured is not None:
            captured.update(cfg=cfg, tenant_id=tenant_id, agent_id=agent_id)
        return boot
    return build_boot


def _serve_recorder(calls, *, require_marker_store=None):
    def serve(server):
        calls.append("serve")
        if require_marker_store is not None:   # markers MUST be set before serve blocks
            assert require_marker_store.meta.get(E.MARK_EMBEDDER_LOADED) == "1"
    return serve


_OK_ENV = {"HIVE_TENANT_ID": "acme"}


# ── config.loaded / zero-config tenant default (boots, EX_OK) ─────────────────
@pytest.mark.parametrize("env", [{}, {"HIVE_TENANT_ID": ""}, {"HIVE_TENANT_ID": "   "}])
def test_missing_tenant_defaults_and_boots(env):
    # tenant_id is a constant label, not a required var: absent/blank ⇒ "default", boot proceeds.
    calls: list = []
    captured: dict = {}
    rc = E.main(env=env, build_boot=_boot_factory(_RecordingBoot(calls), captured),
                serve=_serve_recorder(calls))
    assert rc == E.EX_OK == 0
    assert calls == ["migrate", "build_index", "warm", "make_server", "serve"]
    assert captured["tenant_id"] == "default"


def test_config_validation_failure_exits_config():
    # a known-bad config field (tau_serve=0 disables the never-hallucinate floor) ⇒ 78,
    # and the booter is NEVER built (we fail before assembly).
    calls: list = []
    env = {"HIVE_TENANT_ID": "acme", "HIVE_RECALL__TAU_SERVE": "0"}
    assembled: list = []

    def build_boot(cfg, *, tenant_id, agent_id):
        assembled.append(True)
        return _RecordingBoot(calls)

    rc = E.main(env=env, build_boot=build_boot, serve=_serve_recorder(calls))
    assert rc == E.EX_CONFIG
    assert assembled == [] and calls == []


def test_empty_env_boot_does_not_pollute_stdout(capsys):
    E.main(env={}, build_boot=_boot_factory(_RecordingBoot([])), serve=lambda s: None)
    # stdout is the JSON-RPC channel — the zero-config boot must write NOTHING to it.
    assert capsys.readouterr().out == ""


# ── boot ORDER + happy path ───────────────────────────────────────────────────
def test_boot_runs_migration_then_index_then_serves():
    calls: list = []
    boot = _RecordingBoot(calls)
    captured: dict = {}
    rc = E.main(env=_OK_ENV, build_boot=_boot_factory(boot, captured),
                serve=_serve_recorder(calls), pid=4242)
    assert rc == E.EX_OK == 0
    assert calls == ["migrate", "build_index", "warm", "make_server", "serve"]
    assert captured["tenant_id"] == "acme"


def test_ready_markers_written_before_serve():
    calls: list = []
    store = _MetaStore()
    boot = _RecordingBoot(calls, store=store)
    rc = E.main(env=_OK_ENV, build_boot=_boot_factory(boot),
                serve=_serve_recorder(calls, require_marker_store=store), pid=4242)
    assert rc == E.EX_OK
    assert store.meta[E.MARK_EMBEDDER_LOADED] == "1"
    assert store.meta[E.MARK_SERVE_PID] == "4242"
    assert E.MARK_SERVE_STARTTIME in store.meta          # incarnation identity recorded


def test_stale_ready_marker_cleared_at_boot_start():
    # a restarted container's persistent DB carries a stale embedder_loaded='1'; the boot
    # must invalidate it BEFORE migrate, so a boot that never reaches serve.ready (here:
    # migrate fails) ends RED ('0'), not stale-GREEN ('1').
    calls: list = []
    store = _MetaStore()
    store.meta[E.MARK_EMBEDDER_LOADED] = "1"             # stale marker from a prior boot
    boot = _RecordingBoot(calls, fail_on={"migrate"}, store=store)
    rc = E.main(env=_OK_ENV, build_boot=_boot_factory(boot), serve=_serve_recorder(calls))
    assert rc == E.EX_SOFTWARE
    assert store.meta[E.MARK_EMBEDDER_LOADED] == "0"     # cleared at boot start, never re-set
    assert "serve" not in calls


def test_db_path_and_agent_defaults_bridged():
    captured: dict = {}
    boot = _RecordingBoot([])
    E.main(env=_OK_ENV, build_boot=_boot_factory(boot, captured), serve=lambda s: None)
    # default db_path + agent_id flow through to Config / the assembler
    assert captured["agent_id"] == "default-agent"
    assert captured["cfg"].runtime.db_path == "/data/shared.db"


# ── serve UNREACHABLE unless every prior step succeeded ───────────────────────
def test_serve_unreachable_when_migration_fails():
    calls: list = []
    boot = _RecordingBoot(calls, fail_on={"migrate"})
    rc = E.main(env=_OK_ENV, build_boot=_boot_factory(boot), serve=_serve_recorder(calls))
    assert rc == E.EX_SOFTWARE == 70
    assert "serve" not in calls                # the whole point
    assert calls == ["migrate"]                # stopped at the failed step


def test_index_failure_exits_software_before_serve():
    calls: list = []
    boot = _RecordingBoot(calls, fail_on={"build_index"})
    rc = E.main(env=_OK_ENV, build_boot=_boot_factory(boot), serve=_serve_recorder(calls))
    assert rc == E.EX_SOFTWARE
    assert "serve" not in calls and "warm" not in calls


def test_assemble_failure_exits_software():
    calls: list = []

    def build_boot(cfg, *, tenant_id, agent_id):
        raise RuntimeError("assembler exploded")

    rc = E.main(env=_OK_ENV, build_boot=build_boot, serve=_serve_recorder(calls))
    assert rc == E.EX_SOFTWARE and calls == []


def test_make_server_failure_exits_software_before_serve():
    calls: list = []
    boot = _RecordingBoot(calls, fail_on={"make_server"})
    rc = E.main(env=_OK_ENV, build_boot=_boot_factory(boot), serve=_serve_recorder(calls))
    assert rc == E.EX_SOFTWARE
    assert "serve" not in calls
    assert calls == ["migrate", "build_index", "warm", "make_server"]


# ── embedder.warm (EX_UNAVAILABLE = 69) ───────────────────────────────────────
def test_embedder_warm_failure_exits_69():
    calls: list = []
    boot = _RecordingBoot(calls, fail_on={"warm"})
    rc = E.main(env=_OK_ENV, build_boot=_boot_factory(boot), serve=_serve_recorder(calls))
    assert rc == E.EX_UNAVAILABLE == 69
    assert "serve" not in calls and "make_server" not in calls


def test_embedder_not_resident_exits_69():
    # warm "succeeds" but the model is not actually in RAM (loaded=False) ⇒ still 69:
    # healthy ≡ resident, so a cold server must never reach serve.
    calls: list = []
    boot = _RecordingBoot(calls, embedder=_Embedder(loaded=False))
    rc = E.main(env=_OK_ENV, build_boot=_boot_factory(boot), serve=_serve_recorder(calls))
    assert rc == E.EX_UNAVAILABLE
    assert "serve" not in calls


# ── default dual-door serve path + port resolution (injected-serve never exercises it) ──
def test_make_http_serve_wires_run_http_dual_with_ports_and_verify():
    """_make_http_serve builds the default serve from the injectable run_http_dual, BOTH
    resolved ports, and the token store's verify CALLABLE (no SQLite class leaks into the
    transport). It must ALSO construct a TokenBucketLimiter and thread it + max_body_bytes
    through (defaults: 120/60s, 1 MiB). Mutating verify=boot.token_store.verify makes this red."""
    captured: dict = {}

    def fake_run_http_dual(server, *, host, loopback_port, tunnel_port, verify, lock,
                           limiter, max_body_bytes, webhook_secret, webhook_nudge):
        captured.update(server=server, host=host, loopback_port=loopback_port,
                        tunnel_port=tunnel_port, verify=verify, lock=lock,
                        limiter=limiter, max_body_bytes=max_body_bytes)

    boot = _RecordingBoot([])
    serve = E._make_http_serve(boot, 9999, 9998, run_http_dual=fake_run_http_dual)
    sentinel = object()
    serve(sentinel)
    assert captured["server"] is sentinel
    assert captured["host"] == "0.0.0.0"
    assert captured["loopback_port"] == 9999               # the tokenless host-published door
    assert captured["tunnel_port"] == 9998                 # the token-required compose-internal door
    assert captured["verify"] == boot.token_store.verify   # the verify SEAM, not the class
    assert captured["lock"] is not None
    assert isinstance(captured["limiter"], TokenBucketLimiter)
    assert captured["max_body_bytes"] == 1 << 20           # the generous default cap


def test_make_http_serve_threads_one_lock_and_one_limiter():
    """The single-writer pin across two doors: the lock + limiter are built ONCE in
    _make_http_serve (not per serve call), so a second serve() reuses the SAME objects.
    Mutation — build the limiter INSIDE serve() — reds this."""
    seen: list = []

    def fake_run_http_dual(server, *, host, loopback_port, tunnel_port, verify, lock,
                           limiter, max_body_bytes, webhook_secret, webhook_nudge):
        seen.append((lock, limiter))

    serve = E._make_http_serve(_RecordingBoot([]), 8765, 8766,
                               run_http_dual=fake_run_http_dual)
    serve(object())
    serve(object())                                        # a second serve reuses the SAME objects
    (lock1, lim1), (lock2, lim2) = seen
    assert lock1 is lock2                                  # ONE lock
    assert lim1 is lim2 and isinstance(lim1, TokenBucketLimiter)   # ONE limiter


def test_make_http_serve_constructs_limiter_from_resolved_values():
    """The knobs actually BITE: a limiter built with rate_limit=5 allows exactly 5 checks
    on one label, and the explicit max_body_bytes reaches run_http_dual verbatim."""
    captured: dict = {}

    def fake_run_http_dual(server, *, host, loopback_port, tunnel_port, verify, lock,
                           limiter, max_body_bytes, webhook_secret, webhook_nudge):
        captured.update(limiter=limiter, max_body_bytes=max_body_bytes)

    serve = E._make_http_serve(_RecordingBoot([]), 8765, 8766, rate_limit=5, rate_window_s=2.5,
                               max_body_bytes=512, run_http_dual=fake_run_http_dual)
    serve(object())
    assert captured["max_body_bytes"] == 512
    limiter = captured["limiter"]
    assert isinstance(limiter, TokenBucketLimiter)
    assert all(limiter.check("dev").allowed for _ in range(5))
    assert not limiter.check("dev").allowed                # the 6th — limit=5 was wired in


def test_make_http_serve_threads_webhook_kwargs_verbatim():
    """The webhook wiring is pass-through: an explicit secret + nudge reach run_http_dual
    verbatim, and an unmodified call site threads the inert defaults ('' / None) — the
    tunnel door's webhook branch stays dead code unless the operator armed it."""
    captured: dict = {}

    def fake_run_http_dual(server, *, host, loopback_port, tunnel_port, verify, lock,
                           limiter, max_body_bytes, webhook_secret, webhook_nudge):
        captured.update(webhook_secret=webhook_secret, webhook_nudge=webhook_nudge)

    def nudge() -> None:
        pass

    serve = E._make_http_serve(_RecordingBoot([]), 8765, 8766,
                               run_http_dual=fake_run_http_dual,
                               webhook_secret="hook-s", webhook_nudge=nudge)
    serve(object())
    assert captured["webhook_secret"] == "hook-s"
    assert captured["webhook_nudge"] is nudge              # the callable, passed verbatim
    serve = E._make_http_serve(_RecordingBoot([]), 8765, 8766,
                               run_http_dual=fake_run_http_dual)
    serve(object())
    assert captured["webhook_secret"] == ""                # defaults stay inert
    assert captured["webhook_nudge"] is None


def test_main_default_serve_is_http_with_default_ports(monkeypatch):
    """When `serve` is NOT injected, main() builds the dual-door HTTP daemon via _make_http_serve
    with the resolved default ports (8765 loopback + 8766 tunnel) AND calls it — the boot
    completes, THEN serves."""
    captured: dict = {}

    def fake_make_http_serve(boot, loopback_port, tunnel_port, **kw):
        captured["loopback_port"] = loopback_port
        captured["tunnel_port"] = tunnel_port
        return lambda s: captured.update(served=True)

    monkeypatch.setattr(E, "_make_http_serve", fake_make_http_serve)
    calls: list = []
    rc = E.main(env=_OK_ENV, build_boot=_boot_factory(_RecordingBoot(calls)))   # serve NOT injected
    assert rc == E.EX_OK
    assert captured["loopback_port"] == 8765 and captured["tunnel_port"] == 8766
    assert captured["served"] is True
    assert calls == ["migrate", "build_index", "warm", "make_server"]           # full boot, then HTTP serve


# ── _resolve_max_body: the one surviving boot knob (port + rate are now fixed) ──
def test_resolve_max_body_default_and_override():
    assert E._resolve_max_body({}) == 1 << 20
    assert E._resolve_max_body({"HIVE_HTTP_MAX_BODY_BYTES": "2048"}) == 2048


@pytest.mark.parametrize("bad", ["abc", "-1", "0", "1.5"])
def test_resolve_max_body_malformed_is_none(bad):
    # a zero/negative cap would reject EVERY body — config error, not a knob setting.
    assert E._resolve_max_body({"HIVE_HTTP_MAX_BODY_BYTES": bad}) is None


def test_invalid_max_body_exits_config_before_assembly():
    # a malformed HIVE_HTTP_MAX_BODY_BYTES FAILS FAST (EX_CONFIG) before assembly.
    calls: list = []
    rc = E.main(env={"HIVE_HTTP_MAX_BODY_BYTES": "nope"},
                build_boot=_boot_factory(_RecordingBoot(calls)),
                serve=_serve_recorder(calls))
    assert rc == E.EX_CONFIG
    assert calls == []                             # fail-fast BEFORE build_boot


def test_main_serve_fixed_ports_and_threaded_max_body(monkeypatch):
    """main() serves on the FIXED ports (8765 loopback + 8766 tunnel) and threads only the
    resolved max_body knob into _make_http_serve — the rate-limit belt uses its fixed defaults
    (no operator knob)."""
    captured: dict = {}

    def fake_make_http_serve(boot, loopback_port, tunnel_port, **kw):
        captured.update(loopback_port=loopback_port, tunnel_port=tunnel_port, **kw)
        return lambda s: None

    monkeypatch.setattr(E, "_make_http_serve", fake_make_http_serve)
    rc = E.main(env={"HIVE_HTTP_MAX_BODY_BYTES": "4096"},
                build_boot=_boot_factory(_RecordingBoot([])))
    assert rc == E.EX_OK
    assert captured["loopback_port"] == 8765 and captured["tunnel_port"] == 8766
    assert captured["max_body_bytes"] == 4096
    assert "rate_limit" not in captured            # main() no longer threads rate knobs


# ── auth is a property of the socket: no posture to resolve, injected serve wins ──
def test_main_injected_serve_takes_precedence(monkeypatch):
    """An injected serve (every unit test path) wins — main() does not force the prod dual-door
    serve builder. There is no auth posture to resolve (auth is a property of the listening
    socket). The injected serve runs and main returns EX_OK."""
    served: list = []
    rc = E.main(env=_OK_ENV, build_boot=_boot_factory(_RecordingBoot([])),
                serve=lambda s: served.append(True))
    assert rc == E.EX_OK and served == [True]


def test_main_stale_auth_env_is_ignored_not_a_crash(monkeypatch):
    """A leftover HIVE_AUTH__MODE in an operator .env after the collapse is an ignored
    unknown-group env key (WARN, not crash) — boot still reaches the dual-door serve."""
    captured: dict = {}

    def fake_make_http_serve(boot, loopback_port, tunnel_port, **kw):
        captured.update(loopback_port=loopback_port, tunnel_port=tunnel_port)
        return lambda s: None

    monkeypatch.setattr(E, "_make_http_serve", fake_make_http_serve)
    rc = E.main(env={"HIVE_TENANT_ID": "acme", "HIVE_AUTH__MODE": "open"},
                build_boot=_boot_factory(_RecordingBoot([])))
    assert rc == E.EX_OK                               # a stale auth env never crashes boot
    assert captured["loopback_port"] == 8765 and captured["tunnel_port"] == 8766


# ── the sync side-channel: ONE lock in main(), start after ready, fail-open ────
def _sync_module():
    import hive.app.sync as sync_mod
    return sync_mod


def test_main_shares_one_lock_across_serve_and_sync(monkeypatch):
    """main() owns ONE threading.Lock: the SAME object reaches _make_http_serve (both
    HTTP doors) AND start_sync — the single-writer invariant now spans the sync thread."""
    import threading
    captured: dict = {}

    def fake_make_http_serve(boot, loopback_port, tunnel_port, **kw):
        captured["serve_lock"] = kw.get("lock")
        return lambda s: None

    def fake_start_sync(cfg, store, evidence, lock):
        captured["sync_lock"] = lock
        return None

    monkeypatch.setattr(E, "_make_http_serve", fake_make_http_serve)
    monkeypatch.setattr(_sync_module(), "start_sync", fake_start_sync)
    rc = E.main(env=_OK_ENV, build_boot=_boot_factory(_RecordingBoot([])))
    assert rc == E.EX_OK
    assert captured["serve_lock"] is not None
    assert captured["serve_lock"] is captured["sync_lock"]      # ONE lock, every writer
    assert isinstance(captured["sync_lock"], type(threading.Lock()))


def test_sync_starts_after_ready_markers_and_before_serve(monkeypatch):
    """start_sync runs strictly AFTER _mark_ready (markers already '1') and before
    serve blocks; it receives boot.store and a wired ChangeEvidenceService."""
    from hive.domain.change_evidence import ChangeEvidenceService
    calls: list = []
    store = _MetaStore()
    boot = _RecordingBoot(calls, store=store)

    def fake_start_sync(cfg, store_, evidence, lock):
        calls.append("start_sync")
        assert store.meta.get(E.MARK_EMBEDDER_LOADED) == "1"    # ready markers first
        assert store_ is store
        assert isinstance(evidence, ChangeEvidenceService)
        return None

    monkeypatch.setattr(_sync_module(), "start_sync", fake_start_sync)
    rc = E.main(env=_OK_ENV, build_boot=_boot_factory(boot), serve=_serve_recorder(calls))
    assert rc == E.EX_OK
    assert calls == ["migrate", "build_index", "warm", "make_server",
                     "start_sync", "serve"]


def test_sync_start_failure_logs_and_serves_anyway(monkeypatch):
    """The side-channel fails open: a start_sync explosion never blocks the serve
    (rc stays EX_OK, the injected serve still runs)."""
    def boom(cfg, store, evidence, lock):
        raise RuntimeError("sync exploded")

    monkeypatch.setattr(_sync_module(), "start_sync", boom)
    served: list = []
    rc = E.main(env=_OK_ENV, build_boot=_boot_factory(_RecordingBoot([])),
                serve=lambda s: served.append(True))
    assert rc == E.EX_OK and served == [True]


# ── the webhook nudge wiring: main() hands the LIVE sync wake Event to the tunnel door ──
def test_main_threads_webhook_secret_and_live_sync_nudge_into_default_serve(monkeypatch):
    """With sync armed, main() builds the default serve with cfg.sync.webhook_secret and
    the started sync thread's wake Event's `set` as webhook_nudge — calling the threaded
    nudge sets THAT Event, so a webhook 204 wakes the real loop."""
    import threading
    captured: dict = {}
    ev = threading.Event()

    class _SyncThread:                      # the started daemon thread, attribute contract only
        sync_nudge = ev

    def fake_make_http_serve(boot, loopback_port, tunnel_port, **kw):
        captured.update(kw)
        return lambda s: None

    monkeypatch.setattr(E, "_make_http_serve", fake_make_http_serve)
    monkeypatch.setattr(_sync_module(), "start_sync",
                        lambda cfg, store, evidence, lock: _SyncThread())
    env = {"HIVE_TENANT_ID": "acme",
           "HIVE_SYNC__REPO_URL": "https://example.invalid/repo.git",
           "HIVE_SYNC__WEBHOOK_SECRET": "hook-secret"}
    rc = E.main(env=env, build_boot=_boot_factory(_RecordingBoot([])))
    assert rc == E.EX_OK
    assert captured["webhook_secret"] == "hook-secret"     # cfg.sync.webhook_secret, verbatim
    assert not ev.is_set()
    captured["webhook_nudge"]()                            # the nudge IS the live Event's set
    assert ev.is_set()


def test_main_without_sync_thread_passes_inert_webhook_wiring(monkeypatch):
    """Sync unarmed (no HIVE_SYNC__REPO_URL ⇒ real start_sync returns None): the default
    serve gets webhook_secret='' and webhook_nudge=None — the tunnel door's webhook branch
    is dead code, byte-identical to the pre-webhook build."""
    captured: dict = {}

    def fake_make_http_serve(boot, loopback_port, tunnel_port, **kw):
        captured.update(kw)
        return lambda s: None

    monkeypatch.setattr(E, "_make_http_serve", fake_make_http_serve)
    rc = E.main(env=_OK_ENV, build_boot=_boot_factory(_RecordingBoot([])))
    assert rc == E.EX_OK
    assert captured["webhook_secret"] == ""
    assert captured["webhook_nudge"] is None
