"""P1.13 / M12 §6 — the entrypoint's boot checkpoints must be STRUCTURED JSON on stderr
(stdout is the JSON-RPC channel), at the operator's HIVE_OBS__LOG_LEVEL. Before the fix the
'hive' logger was NOTSET with no handler, so every INFO checkpoint was silently dropped and
HIVE_OBS__LOG_LEVEL was a dead knob. The conftest fixture restores the global logger.
"""
from __future__ import annotations

import json

from hive.tools import entrypoint as E


class _Embedder:
    loaded = True


class _Store:
    def __init__(self):
        self.meta = {}

    def meta_set(self, k, v):
        self.meta[k] = v

    def meta_get(self, k):
        return self.meta.get(k)


class _Boot:
    def __init__(self):
        self.store = _Store()

    def migrate(self): ...
    def build_index(self): ...
    def warm_embedder(self):
        return _Embedder()

    def make_server(self):
        return object()


def _build_boot(cfg, *, tenant_id, agent_id):
    return _Boot()


def test_serve_ready_checkpoint_is_json_on_stderr_not_stdout(capsys):
    rc = E.main(env={"HIVE_TENANT_ID": "acme", "HIVE_OBS__LOG_LEVEL": "20"},
                build_boot=_build_boot, serve=lambda s: None)
    assert rc == E.EX_OK
    cap = capsys.readouterr()
    assert cap.out == ""                                   # stdout stays the protocol channel
    lines = [l for l in cap.err.splitlines() if l.strip().startswith("{")]
    events = []
    for l in lines:
        try:
            events.append(json.loads(l))
        except json.JSONDecodeError:
            pass
    msgs = " ".join(e.get("message", "") for e in events)
    # the §6 boot checkpoints surface as JSON records
    assert "serve_ready" in msgs
    assert "config_loaded" in msgs
    assert any(e.get("level") == "INFO" for e in events)


def test_log_level_gates_info_checkpoints(capsys):
    # at WARNING (40) the INFO serve_ready checkpoint must be suppressed (the knob is LIVE)
    rc = E.main(env={"HIVE_TENANT_ID": "acme", "HIVE_OBS__LOG_LEVEL": "40"},
                build_boot=_build_boot, serve=lambda s: None)
    assert rc == E.EX_OK
    assert "serve_ready" not in capsys.readouterr().err


def test_missing_env_error_is_structured_json(capsys):
    rc = E.main(env={}, build_boot=_build_boot, serve=lambda s: None)
    assert rc == E.EX_CONFIG
    cap = capsys.readouterr()
    assert cap.out == ""
    # the missing-env error surfaces as a JSON ERROR record (configured at default level first)
    err_records = []
    for l in cap.err.splitlines():
        l = l.strip()
        if l.startswith("{"):
            try:
                err_records.append(json.loads(l))
            except json.JSONDecodeError:
                pass
    assert any(r.get("level") == "ERROR" and "missing_required_env" in r.get("message", "")
               for r in err_records)
