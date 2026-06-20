"""#5a acceptance — the secret floor end-to-end: a credential in a write is REFUSED
before any persistence (0 rows, 0 blobs), the refused envelope carries rule NAMES only,
and nothing about the secret reaches a row, blob, or recall."""
from __future__ import annotations

import json
import logging

import pytest

from hive.app.mcp_server import MCPRequest
from tests.acceptance.conftest import build_acc

pytestmark = pytest.mark.embed   # loads the real Qwen3 model (gated heavy tier)

# A realistic AWS access-key shape (a secret-floor rule family). Not a real credential.
_PLANTED = "deploy creds: AKIAIOSFODNN7EXAMPLE / wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"


def test_acceptance_secret_refused(embedder_v1):
    c = build_acc(embedder_v1)
    server = c.make_server()
    resp = server.handle(MCPRequest(1, "tools/call",
            {"name": "hive_write", "arguments": {"text": _PLANTED, "approved_by": "acc"}}))
    env = json.loads(resp.result["content"][0]["text"])

    assert env["status"] == "refused"
    assert env["scan"]["action"] == "refuse" and env["scan"]["n_findings"] >= 1
    # the envelope names the rule(s) but never echoes the secret bytes
    blob = json.dumps(env)
    assert "AKIAIOSFODNN7EXAMPLE" not in blob
    assert "wJalrXUtnFEMI" not in blob

    # nothing was staged: 0 episodes, 0 blobs, and the credential is unrecallable
    approved, pending = c.store.counts()
    assert (approved, pending) == (0, 0)
    n_blobs = c.conn.execute("SELECT COUNT(*) FROM blobs").fetchone()[0]
    assert n_blobs == 0


def test_acceptance_secret_never_in_any_row_after_mixed_writes(embedder_v1):
    """A clean write lands; a secret write is refused — and a scan of every persisted row
    + blob contains no secret substring (the #5a floor is structural, not hoped-for)."""
    c = build_acc(embedder_v1)
    c.admission.write("a perfectly clean durable insight about caching",
                      proposed_by="acc", approved_by="acc")
    try:
        c.admission.write(_PLANTED, proposed_by="acc", approved_by="acc")
    except Exception:
        pass                                            # refuse raises SecretRefused — expected
    rows = c.conn.execute("SELECT text FROM episodes").fetchall()
    blobs = c.conn.execute("SELECT text FROM blobs").fetchall()
    hay = " ".join(r["text"] for r in rows) + " ".join(b["text"] for b in blobs)
    assert "AKIAIOSFODNN7EXAMPLE" not in hay
    assert "wJalrXUtnFEMI" not in hay


def test_acceptance_secret_never_in_logs(embedder_v1, caplog):
    """#5a, the log half: the structured logs emitted on a refused write carry rule NAMES
    + counts only — the secret bytes appear in no message and no extra field, end-to-end."""
    c = build_acc(embedder_v1)
    hive_log = logging.getLogger("hive")
    prev = hive_log.propagate
    hive_log.propagate = True                       # ensure records reach caplog's root handler
    try:
        with caplog.at_level(logging.DEBUG, logger="hive"):
            try:
                c.admission.write(_PLANTED, proposed_by="acc", approved_by="acc")
            except Exception:
                pass                                # SecretRefused — expected
    finally:
        hive_log.propagate = prev
    parts: list[str] = []
    for r in caplog.records:
        parts.append(r.getMessage())
        parts.extend(str(v) for v in r.__dict__.values())   # every extra field too
    blob = " ".join(parts)
    assert any("refused" in r.getMessage() for r in caplog.records)   # the refusal DID log
    assert "AKIAIOSFODNN7EXAMPLE" not in blob
    assert "wJalrXUtnFEMI" not in blob
