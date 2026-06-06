"""P1.13 / M12 — the entrypoint boot state machine (driving adapter).

The strict order config→migrate→index→warm→serve, the fail-fast exit codes
(78 EX_CONFIG / 70 EX_SOFTWARE / 69 EX_UNAVAILABLE), the embedder-resident gate, and the
"serve is UNREACHABLE unless every prior step succeeded" invariant are pinned here against
an injected fake boot — no Docker required. Three of the four RULE-2 mutations live in
this module (missing-env guard, swallow-embedder-exception, migrate-failure return).
"""
from __future__ import annotations

import pytest

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

    def meta_get(self, key: str):
        return self.meta.get(key)


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


# ── config.loaded / missing-env (EX_CONFIG = 78) ──────────────────────────────
@pytest.mark.parametrize("env", [{}, {"HIVE_TENANT_ID": ""}, {"HIVE_TENANT_ID": "   "}])
def test_missing_required_env_exits_config(env):
    calls: list = []
    rc = E.main(env=env, build_boot=_boot_factory(_RecordingBoot(calls)),
                serve=_serve_recorder(calls))
    assert rc == E.EX_CONFIG == 78
    assert calls == []                         # never assembled, never served


def test_config_validation_failure_exits_config():
    # a known-bad config field (epsilon_explore=0 violates the guardrail-1 floor) ⇒ 78,
    # and the booter is NEVER built (we fail before assembly).
    calls: list = []
    env = {"HIVE_TENANT_ID": "acme", "HIVE_RECALL__EPSILON_EXPLORE": "0"}
    assembled: list = []

    def build_boot(cfg, *, tenant_id, agent_id):
        assembled.append(True)
        return _RecordingBoot(calls)

    rc = E.main(env=env, build_boot=build_boot, serve=_serve_recorder(calls))
    assert rc == E.EX_CONFIG
    assert assembled == [] and calls == []


def test_missing_env_does_not_pollute_stdout(capsys):
    E.main(env={}, build_boot=_boot_factory(_RecordingBoot([])), serve=lambda s: None)
    # stdout is the JSON-RPC channel — a boot failure must write NOTHING to it.
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


# ── default HTTP serve path + port resolution (the §6 path injected-serve never exercises) ──
def test_make_http_serve_wires_run_http_with_port_and_verify():
    """_make_http_serve builds the default serve from the injectable run_http, the resolved
    port, and the token store's verify CALLABLE (no SQLite class leaks into the transport).
    Mutating verify=boot.token_store.verify makes this red."""
    captured: dict = {}

    def fake_run_http(server, *, host, port, verify, lock):
        captured.update(server=server, host=host, port=port, verify=verify, lock=lock)

    boot = _RecordingBoot([])
    serve = E._make_http_serve(boot, 9999, run_http=fake_run_http)
    sentinel = object()
    serve(sentinel)
    assert captured["server"] is sentinel
    assert captured["host"] == "0.0.0.0"
    assert captured["port"] == 9999
    assert captured["verify"] == boot.token_store.verify   # the verify SEAM, not the class
    assert captured["lock"] is not None


def test_main_default_serve_is_http_with_default_port(monkeypatch):
    """When `serve` is NOT injected, main() builds the HTTP daemon via _make_http_serve with
    the resolved default port (8765) AND calls it — the boot completes, THEN serves."""
    captured: dict = {}

    def fake_make_http_serve(boot, port, **kw):
        captured["port"] = port
        return lambda s: captured.update(served=True)

    monkeypatch.setattr(E, "_make_http_serve", fake_make_http_serve)
    calls: list = []
    rc = E.main(env=_OK_ENV, build_boot=_boot_factory(_RecordingBoot(calls)))   # serve NOT injected
    assert rc == E.EX_OK
    assert captured["port"] == 8765 and captured["served"] is True
    assert calls == ["migrate", "build_index", "warm", "make_server"]           # full boot, then HTTP serve


def test_main_default_serve_uses_env_http_port(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(E, "_make_http_serve",
                        lambda boot, port, **kw: (captured.update(port=port), lambda s: None)[1])
    rc = E.main(env={"HIVE_TENANT_ID": "acme", "HIVE_HTTP_PORT": "9000"},
                build_boot=_boot_factory(_RecordingBoot([])))
    assert rc == E.EX_OK
    assert captured["port"] == 9000


@pytest.mark.parametrize("bad", ["not-a-port", "0", "70000", "-1"])
def test_invalid_http_port_exits_config(bad):
    # a malformed/out-of-range HIVE_HTTP_PORT FAILS FAST (EX_CONFIG) before assembly —
    # never raises out of the boot path. Resolved before build_boot ⇒ never assembled/served.
    calls: list = []
    env = {"HIVE_TENANT_ID": "acme", "HIVE_HTTP_PORT": bad}
    rc = E.main(env=env, build_boot=_boot_factory(_RecordingBoot(calls)),
                serve=_serve_recorder(calls))
    assert rc == E.EX_CONFIG
    assert calls == []
