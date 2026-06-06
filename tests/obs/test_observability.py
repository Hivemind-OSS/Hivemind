"""P1.9 — M11 observability: JSONFormatter (standard fields) + configure_json_logging,
and the security floor that a secret value never reaches an emitted log line.
"""
from __future__ import annotations

import io
import json
import logging

from hive.app.observability import JSONFormatter, configure_json_logging
from hive.app.config import Config


def _emit_and_capture(fn) -> list[str]:
    """Configure the 'hive' logger to a captured stream, run fn(log), return raw lines."""
    buf = io.StringIO()
    configure_json_logging(level=logging.DEBUG, stream=True)
    log = logging.getLogger("hive")
    # redirect the single stream handler to our buffer
    for h in log.handlers:
        if isinstance(h, logging.StreamHandler):
            h.setStream(buf)
    fn(log)
    for h in log.handlers:
        h.flush()
    return [ln for ln in buf.getvalue().splitlines() if ln.strip()]


def test_json_formatter_emits_standard_fields():
    rec = logging.LogRecord("hive", logging.INFO, __file__, 10, "hello %s", ("world",), None)
    out = json.loads(JSONFormatter().format(rec))
    assert out["level"] == "INFO"
    assert out["logger"] == "hive"
    assert out["message"] == "hello world"
    assert "ts" in out


def test_json_formatter_includes_extras():
    rec = logging.LogRecord("hive", logging.INFO, __file__, 10, "x", (), None)
    rec.trace_id = "abc123"
    out = json.loads(JSONFormatter().format(rec))
    assert out["trace_id"] == "abc123"


def test_configure_is_idempotent_no_handler_pileup():
    configure_json_logging(level=logging.INFO, stream=True)
    configure_json_logging(level=logging.INFO, stream=True)
    log = logging.getLogger("hive")
    assert sum(isinstance(h, logging.StreamHandler) for h in log.handlers) == 1


def test_secret_never_logged():
    # the env-coercion-failure path is the one §6 says omits the raw value (could be a secret).
    secret = "sk-LIVESECRET_ghp_AKIA_payload_99887766"
    env = {"HIVE_GEOMETRY__D": secret}   # non-coercible to int → WARN, value must be withheld

    lines = _emit_and_capture(lambda _log: Config.load(db_path=":memory:", env=env))

    blob = "\n".join(lines)
    assert "sk-" not in blob
    assert "AKIA" not in blob
    assert "ghp_" not in blob
    assert secret not in blob          # the WHOLE raw value, not just canary prefixes
    # but the field name (safe) MUST be present so the warning is actionable
    assert any("D" in ln or "geometry" in ln.lower() for ln in lines)
