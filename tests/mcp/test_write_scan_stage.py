"""M06 hive_write: the three-branch secret floor wired into the client-gated tool —
PASS lands APPROVED + recallable, REDACT lands APPROVED with MASKED text (no raw secret
bytes, hash over post-redaction text), REFUSE writes NOTHING (0 rows). The secret scan
is the SOLE always-on gate now that the approval queue is gone — a planted credential is
refused on the direct path exactly as before (the chunk's mutation pin)."""
from __future__ import annotations

from hive.domain.models import content_hash
from hive.domain.secret_scan import REDACT
from tests.fakes._fakes import FakeScanner
from tests.mcp._helpers import build_real_server, content, tool_call

_SECRET = "deploy key sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345 do not leak"


def test_write_clean_lands_approved():
    server, _ = build_real_server()
    out = content(tool_call(server, "hive_write",
                            {"text": "a clean durable insight", "approved_by": "u"}))
    assert out["status"] == "approved"
    assert isinstance(out["id"], int)
    assert out["approved_by"] == "u"
    assert out["scan"]["action"] == "clean"
    assert server.store.counts() == (1, 0)                 # 1 approved, 0 pending


def test_write_redact_stores_masked_text_no_raw_secret():
    server, _ = build_real_server(scanner=FakeScanner(mode=REDACT))
    out = content(tool_call(server, "hive_write", {"text": _SECRET, "approved_by": "u"}))
    assert out["status"] == "redacted"
    # the stored row holds the MASKED text — none of the raw secret bytes
    stored = server.store.get_episode(out["id"]).text
    assert "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345" not in stored
    assert "[REDACTED]" in stored
    # the stored row's content_hash binds the POST-redaction text (over the mask, not the secret)
    assert server.store.get_episode(out["id"]).content_hash == content_hash(stored)
    # the raw secret appears nowhere in the envelope
    assert "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345" not in out["redacted_preview"]
    assert out["scan"]["action"] == "redact"
    # a redacted write is ALSO approved + recallable (no pending state survives)
    assert server.store.get_episode(out["id"]).status == "approved"


def test_write_planted_secret_refused_before_write():
    server, _ = build_real_server(scanner=FakeScanner())   # default: REFUSE
    out = content(tool_call(server, "hive_write", {"text": _SECRET, "approved_by": "u"}))
    assert out["status"] == "refused"
    assert out["scan"]["action"] == "refuse"
    assert out["scan"]["rules"]                             # named rule(s)
    assert out["scan"]["n_findings"] >= 1
    # NOTHING written: 0 rows, and the secret is in no blob
    assert server.store.counts() == (0, 0)
    assert server.store.conn.execute(
        "SELECT 1 FROM blobs WHERE content_hash=?", (content_hash(_SECRET),)).fetchone() is None


def test_refused_envelope_carries_no_secret_bytes():
    server, _ = build_real_server(scanner=FakeScanner())
    out = content(tool_call(server, "hive_write", {"text": _SECRET, "approved_by": "u"}))
    import json as _json
    blob = _json.dumps(out)
    assert "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345" not in blob
