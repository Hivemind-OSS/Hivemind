"""P0 (HOOK-RELOCATION-PLAN v3 §6) — the PER-SESSION MCP server entrypoint
``python -m hive.app.mcp_server``, the Tier-0 transport an IDE execs INSIDE the warm
container. Pins the boot ORDER (config→build_container→migrate→build_index→warm→
make_server→serve), the fail-fast sysexits codes, the embedder-resident gate, and — THE
P0 invariant — that this entry is MARKER-INERT: unlike ``hive.tools.entrypoint`` it
neither STAMPS nor CLEARS the boot readiness markers (``boot:serve_pid`` /
``boot:serve_starttime`` / ``boot:embedder_loaded``) that identify PID 1 for the container
HEALTHCHECK. A per-session exec'd process touching them would corrupt liveness identity.

Injected fakes — no Docker, no torch, no real stdio (the ``hive`` logger is snapshot/
restored by tests/container/conftest.py).
"""
from __future__ import annotations

import pytest

from hive.app import mcp_server as M
from hive.tools import entrypoint as E   # ONLY for the marker key names this entry must NOT touch

# The three readiness markers PID 1 owns and a per-session server must leave strictly alone.
_MARKER_KEYS = (E.MARK_SERVE_PID, E.MARK_SERVE_STARTTIME, E.MARK_EMBEDDER_LOADED)
_OK_ENV = {"HIVE_TENANT_ID": "acme"}


# ── injected fakes (mirror tests/container/test_entrypoint.py) ─────────────────
class _Embedder:
    def __init__(self, loaded: bool = True) -> None:
        self.loaded = loaded


class _MetaStore:
    """The Boot.store surface the marker policy writes through. The per-session entry must
    never call ``meta_set`` with a marker key — these tests assert exactly that."""
    def __init__(self) -> None:
        self.meta: dict[str, str] = {}

    def meta_set(self, key: str, value: str) -> None:
        self.meta[key] = value

    def meta_get(self, key: str):
        return self.meta.get(key)


class _RecordingContainer:
    """Records boot-call order; ``fail_on`` makes one step raise; ``embedder`` controls the
    warm result (loaded True/False). Mirrors the Container surface ``main()`` drives."""
    def __init__(self, calls: list, *, embedder=None, fail_on=None, store=None) -> None:
        self.calls = calls
        self._embedder = embedder if embedder is not None else _Embedder(loaded=True)
        self._fail_on = set(fail_on or ())
        self.store = store if store is not None else _MetaStore()
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


def _build_factory(container, captured=None):
    def build_container_fn(cfg, *, tenant_id, agent_id):
        if captured is not None:
            captured.update(cfg=cfg, tenant_id=tenant_id, agent_id=agent_id)
        return container
    return build_container_fn


def _serve_recorder(calls):
    def serve(server):
        calls.append("serve")
    return serve


# ── boot ORDER + happy path ───────────────────────────────────────────────────
def test_boot_order_then_serves():
    calls: list = []
    container = _RecordingContainer(calls)
    captured: dict = {}
    rc = M.main(argv=[], env=_OK_ENV, build_container_fn=_build_factory(container, captured),
                serve=_serve_recorder(calls))
    assert rc == M.EX_OK == 0
    assert calls == ["migrate", "build_index", "warm", "make_server", "serve"]
    assert captured["tenant_id"] == "acme"


# ── THE P0 invariant: marker-INERT (neither stamps nor clears) ────────────────
def test_serve_does_not_stamp_readiness_markers():
    # The whole point of P0: a per-session exec'd server must NOT write the PID-1 liveness
    # markers. Adding a _mark_ready-equivalent call to main() turns this RED (RULE-2).
    calls: list = []
    store = _MetaStore()
    container = _RecordingContainer(calls, store=store)
    rc = M.main(argv=[], env=_OK_ENV, build_container_fn=_build_factory(container),
                serve=_serve_recorder(calls))
    assert rc == M.EX_OK
    for key in _MARKER_KEYS:
        assert key not in store.meta, f"per-session server must not stamp {key!r}"


def test_does_not_clear_a_live_marker():
    # A per-session connection opens against the SAME /data volume whose markers PID 1 owns
    # and has set GREEN. The per-session boot must leave them untouched (no _invalidate_ready):
    # clearing embedder_loaded would flap the container HEALTHCHECK to red under a live server.
    calls: list = []
    store = _MetaStore()
    store.meta[E.MARK_EMBEDDER_LOADED] = "1"      # PID 1 is up and healthy
    store.meta[E.MARK_SERVE_PID] = "1"
    container = _RecordingContainer(calls, store=store)
    rc = M.main(argv=[], env=_OK_ENV, build_container_fn=_build_factory(container),
                serve=_serve_recorder(calls))
    assert rc == M.EX_OK
    assert store.meta[E.MARK_EMBEDDER_LOADED] == "1"     # untouched — still GREEN
    assert store.meta[E.MARK_SERVE_PID] == "1"


# ── identity resolution: CLI arg precedence, env fallback, tenant hard-required ─
def test_cli_args_take_precedence_over_env():
    captured: dict = {}
    container = _RecordingContainer([])
    M.main(argv=["--tenant", "fromcli", "--agent", "agentcli", "--db", "/tmp/x.db"],
           env={"HIVE_TENANT_ID": "fromenv", "HIVE_AGENT_ID": "agentenv"},
           build_container_fn=_build_factory(container, captured), serve=lambda s: None)
    assert captured["tenant_id"] == "fromcli"
    assert captured["agent_id"] == "agentcli"
    assert captured["cfg"].db_path == "/tmp/x.db"


def test_tenant_and_defaults_from_env():
    captured: dict = {}
    container = _RecordingContainer([])
    M.main(argv=[], env=_OK_ENV, build_container_fn=_build_factory(container, captured),
           serve=lambda s: None)
    assert captured["tenant_id"] == "acme"
    assert captured["agent_id"] == "default-agent"       # default when neither arg nor env set it
    assert captured["cfg"].db_path == "/data/shared.db"  # default per-session store path


@pytest.mark.parametrize("env", [{}, {"HIVE_TENANT_ID": ""}, {"HIVE_TENANT_ID": "   "}])
def test_missing_tenant_exits_config(env):
    calls: list = []
    rc = M.main(argv=[], env=env, build_container_fn=_build_factory(_RecordingContainer(calls)),
                serve=_serve_recorder(calls))
    assert rc == M.EX_CONFIG == 78
    assert calls == []                            # never assembled, never served


def test_missing_tenant_does_not_pollute_stdout(capsys):
    M.main(argv=[], env={}, build_container_fn=_build_factory(_RecordingContainer([])),
           serve=lambda s: None)
    assert capsys.readouterr().out == ""          # stdout is the JSON-RPC channel


# ── fail-fast: serve is UNREACHABLE unless every prior step succeeds ───────────
def test_assemble_failure_exits_software():
    calls: list = []

    def build_container_fn(cfg, *, tenant_id, agent_id):
        raise RuntimeError("assembler exploded")

    rc = M.main(argv=[], env=_OK_ENV, build_container_fn=build_container_fn,
                serve=_serve_recorder(calls))
    assert rc == M.EX_SOFTWARE == 70 and calls == []


def test_migrate_failure_exits_software_before_serve():
    calls: list = []
    container = _RecordingContainer(calls, fail_on={"migrate"})
    rc = M.main(argv=[], env=_OK_ENV, build_container_fn=_build_factory(container),
                serve=_serve_recorder(calls))
    assert rc == M.EX_SOFTWARE
    assert "serve" not in calls and calls == ["migrate"]


def test_index_failure_exits_software_before_serve():
    calls: list = []
    container = _RecordingContainer(calls, fail_on={"build_index"})
    rc = M.main(argv=[], env=_OK_ENV, build_container_fn=_build_factory(container),
                serve=_serve_recorder(calls))
    assert rc == M.EX_SOFTWARE
    assert "serve" not in calls and "warm" not in calls


def test_make_server_failure_exits_software_before_serve():
    calls: list = []
    container = _RecordingContainer(calls, fail_on={"make_server"})
    rc = M.main(argv=[], env=_OK_ENV, build_container_fn=_build_factory(container),
                serve=_serve_recorder(calls))
    assert rc == M.EX_SOFTWARE
    assert "serve" not in calls
    assert calls == ["migrate", "build_index", "warm", "make_server"]


# ── embedder-resident gate (EX_UNAVAILABLE = 69) ──────────────────────────────
def test_embedder_warm_failure_exits_69():
    calls: list = []
    container = _RecordingContainer(calls, fail_on={"warm"})
    rc = M.main(argv=[], env=_OK_ENV, build_container_fn=_build_factory(container),
                serve=_serve_recorder(calls))
    assert rc == M.EX_UNAVAILABLE == 69
    assert "serve" not in calls and "make_server" not in calls


def test_embedder_not_resident_exits_69():
    # warm "succeeds" but the model is not actually in RAM (loaded=False) ⇒ still 69:
    # healthy ≡ resident, so a cold server must never reach serve.
    calls: list = []
    container = _RecordingContainer(calls, embedder=_Embedder(loaded=False))
    rc = M.main(argv=[], env=_OK_ENV, build_container_fn=_build_factory(container),
                serve=_serve_recorder(calls))
    assert rc == M.EX_UNAVAILABLE
    assert "serve" not in calls
