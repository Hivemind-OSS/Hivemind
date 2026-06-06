"""P1.13 / M12 — the stdio transport contract (channel hygiene).

The ported `run_stdio` loop is the container's MCP transport. M12 pins two things here:
a JSON-RPC request gets a reply on the OUT stream, and the loop writes NOTHING to the
process stdout/stderr (stdout is the protocol channel — a stray print pollutes it). Uses
a minimal stub server so this stays a transport test, not a full-server test.
"""
from __future__ import annotations

import io
import json

from hive.app.mcp_server import MCPResponse, run_stdio


class _StubServer:
    """Just enough surface for run_stdio: a `.handle(req)` that echoes the method."""
    def __init__(self):
        self.seen = []

    def handle(self, req):
        self.seen.append(req.method)
        return MCPResponse(id=req.id, result={"echo": req.method})


def _roundtrip(lines: str):
    server = _StubServer()
    out = io.StringIO()
    run_stdio(server, io.StringIO(lines), out)
    return server, out.getvalue()


def test_jsonrpc_request_gets_reply_on_out_stream():
    _, out = _roundtrip(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                    "params": {}}) + "\n")
    reply = json.loads(out.strip())
    assert reply["id"] == 1 and reply["jsonrpc"] == "2.0"
    assert reply["result"]["echo"] == "initialize"


def test_loop_writes_nothing_to_process_stdio(capsys):
    # the ONLY sink is the passed out_stream; the real stdout/stderr stay clean.
    _roundtrip(json.dumps({"jsonrpc": "2.0", "id": 7, "method": "ping", "params": {}}) + "\n")
    captured = capsys.readouterr()
    assert captured.out == ""


def test_notification_gets_no_reply():
    _, out = _roundtrip(json.dumps({"jsonrpc": "2.0", "method": "initialize",
                                    "params": {}}) + "\n")  # no id ⇒ notification
    assert out == ""


def test_loop_survives_malformed_line_then_serves_next():
    server, out = _roundtrip(
        "{not json\n" + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping",
                                    "params": {}}) + "\n")
    replies = [json.loads(l) for l in out.splitlines() if l.strip()]
    # a parse-error reply (-32700) on the out stream, then the good request answered —
    # the loop never crashed.
    assert any(r.get("error", {}).get("code") == -32700 for r in replies)
    assert any(r.get("id") == 2 for r in replies)
    assert server.seen == ["ping"]
