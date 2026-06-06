"""M06 hive_write: the three-branch secret floor wired into the tool — PASS stages
pending, REDACT stages MASKED (no raw secret bytes, hash over post-redaction text),
REFUSE stages NOTHING (0 rows). status is NEVER 'approved' from write."""
from __future__ import annotations

from hive.domain.models import content_hash
from hive.domain.secret_scan import REDACT
from tests.fakes._fakes import FakeScanner
from tests.mcp._helpers import build_real_server, content, tool_call

_SECRET = "deploy key sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345 do not leak"


def test_write_clean_stages_pending():
    server, _ = build_real_server()
    out = content(tool_call(server, "hive_write", {"text": "a clean durable insight"}))
    assert out["status"] == "pending"
    assert out["status"] != "approved"
    assert isinstance(out["id"], int)
    assert out["content_hash"] == content_hash("a clean durable insight")
    assert out["scan"]["action"] == "clean"
    assert server.store.counts() == (0, 1)                 # 0 approved, 1 pending


def test_write_redact_stages_masked_text_no_raw_secret():
    server, _ = build_real_server(scanner=FakeScanner(mode=REDACT))
    out = content(tool_call(server, "hive_write", {"text": _SECRET}))
    assert out["status"] == "redacted"
    # the staged row holds the MASKED text — none of the raw secret bytes
    pending = server.store.list_pending()
    assert len(pending) == 1
    staged = pending[0]["text"]
    assert "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345" not in staged
    assert "[REDACTED]" in staged
    # content_hash binds the POST-redaction text
    assert out["content_hash"] == content_hash(staged)
    # the raw secret appears nowhere in the envelope
    assert "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345" not in out["redacted_preview"]
    assert out["scan"]["action"] == "redact"


def test_write_planted_secret_refused_before_stage():
    server, _ = build_real_server(scanner=FakeScanner())   # default: REFUSE
    out = content(tool_call(server, "hive_write", {"text": _SECRET}))
    assert out["status"] == "refused"
    assert out["scan"]["action"] == "refuse"
    assert out["scan"]["rules"]                              # named rule(s)
    assert out["scan"]["n_findings"] >= 1
    # NOTHING staged: 0 rows, and the secret is in no blob
    assert server.store.counts() == (0, 0)
    assert server.store.fetch(content_hash(_SECRET)) is None


def test_refused_envelope_carries_no_secret_bytes():
    server, _ = build_real_server(scanner=FakeScanner())
    out = content(tool_call(server, "hive_write", {"text": _SECRET}))
    import json as _json
    blob = _json.dumps(out)
    assert "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345" not in blob
