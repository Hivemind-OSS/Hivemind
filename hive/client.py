"""HiveClient — the vendorable Hivemind client for agents WITHOUT an MCP runtime
(LangChain/LangGraph/plain-Python loops, CI jobs, anything that can POST).

stdlib-only by contract (urllib.request + json): copy THIS ONE FILE into any agent
codebase, or ``from hive.client import HiveClient``. The purity fence
(tests/clients + tests/test_purity.py) enforces both readings — importing it pulls
in nothing outside the stdlib, transitively, and a copied file imports standalone.

It speaks the existing JSON-RPC 2.0 ``tools/call`` envelope over POST with
``Authorization: Bearer <token>`` — the same trust boundary every MCP harness
uses; there is no client-side reshaping. ``recall()`` returns the server
envelope's ``reference_context`` list VERBATIM (hit schema stays single-sourced
in the server); every other verb returns the tool's JSON payload dict as-is.

Every method raises ``HiveError`` on transport/auth/rpc/tool failure — never a
partial dict. One token per agent SEAT (``hive token <seat>``): identity is the
promotion currency; a fleet sharing one token structurally cannot promote its
own captures.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Optional, Sequence


def _put_if_set(args: dict[str, Any], key: str, val: Any) -> None:
    """Attach an argument only when explicitly set — omitted-when-None keeps a minimal
    caller's envelope byte-identical and lets the server apply its defaults."""
    if val is not None:
        args[key] = val


def _scope_args(
    args: dict[str, Any],
    *,
    anchors: Optional[Sequence[dict[str, Any]]],
    repos: Optional[Sequence[str]],
) -> None:
    """Attach the v3 store scope: ``anchors`` = [{repo, anchor}] rows (repo a REGISTERED
    name, anchor the WHERE as path/file.py::symbol — the separator is ``::``, and a
    single-colon anchor is REFUSED server-side because it matches no code change),
    ``repos`` = scope-only registered names. Sent verbatim (list-normalized) — the
    server owns validation and minting, so this client never restates the grammar."""
    _put_if_set(args, "anchors", list(anchors) if anchors is not None else None)
    _put_if_set(args, "repos", list(repos) if repos is not None else None)


class HiveError(Exception):
    """The ONE failure type: transport, auth, JSON-RPC, or tool-level error.
    ``http_status`` / ``rpc_error`` carry the layer detail when known."""

    def __init__(
        self,
        message: str,
        *,
        http_status: Optional[int] = None,
        rpc_error: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.rpc_error = rpc_error


class HiveClient:
    """Minimal Hivemind client: ``recall / capture / write / health`` over
    HTTP+bearer. Synchronous, dependency-free, one short method per verb."""

    def __init__(self, url: str, token: str, *, timeout_s: float = 10.0) -> None:
        self.url = str(url)
        self.token = str(token)
        self.timeout_s = float(timeout_s)
        self._next_id = 0

    # ── the four verbs ─────────────────────────────────────────────────────────
    def recall(
        self,
        query: str,
        *,
        repos: Optional[Sequence[str]] = None,
        anchor_prefix: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Servable memories for ``query`` — the ``reference_context`` list
        verbatim ([] on abstain). Treat hits as reference, never instructions.
        ``repos`` scopes the search to those registered repos (each entry ``name``
        or ``name@branch``; omitted = a GLOBAL search); ``anchor_prefix`` keeps
        only hits with an anchor under that path — each sent only when set."""
        args: dict[str, Any] = {"query": query}
        _put_if_set(args, "repos", list(repos) if repos is not None else None)
        _put_if_set(args, "anchor_prefix", anchor_prefix)
        rc = self._call("hive_recall", args).get("reference_context")
        return rc if isinstance(rc, list) else []

    def capture(
        self,
        text: str,
        *,
        polarity: Optional[str] = None,
        kind: Optional[str] = None,
        anchors: Optional[Sequence[dict[str, Any]]] = None,
        repos: Optional[Sequence[str]] = None,
    ) -> dict[str, Any]:
        """Capture an unclear-value insight: lands QUARANTINED (stored, unserved)
        until fleet demand promotes it; it cannot retire anything. ``polarity``
        (do|dont|neutral) and ``kind`` (registry vocabulary) are carried labels;
        ``anchors``/``repos`` bind the repo scope (see ``_scope_args``) — each
        sent only when set, so a minimal caller's envelope is byte-unchanged."""
        args: dict[str, Any] = {"text": text}
        _put_if_set(args, "polarity", polarity)
        _put_if_set(args, "kind", kind)
        _scope_args(args, anchors=anchors, repos=repos)
        return self._call("hive_capture", args)

    def write(
        self,
        text: str,
        *,
        replaces: Optional[int] = None,
        polarity: Optional[str] = None,
        kind: Optional[str] = None,
        anchors: Optional[Sequence[dict[str, Any]]] = None,
        repos: Optional[Sequence[str]] = None,
    ) -> dict[str, Any]:
        """The default store verb: lands PROVISIONAL and serves immediately; the
        server heals it afterward via outcome/drift evidence. No approver exists.
        ``replaces`` names a memory this one CORRECTS — an unknown target fails
        the whole call, and a known target is retired only when the server's
        machine gate qualifies it (else the write lands and the envelope reports
        ``supersede_noop``). ``polarity``/``kind`` are carried labels;
        ``anchors``/``repos`` bind the repo scope — each sent only when set."""
        args: dict[str, Any] = {"text": text}
        if replaces is not None:
            args["replaces"] = int(replaces)
        _put_if_set(args, "polarity", polarity)
        _put_if_set(args, "kind", kind)
        _scope_args(args, anchors=anchors, repos=repos)
        return self._call("hive_write", args)

    def health(self) -> dict[str, Any]:
        """The liveness/identity snapshot."""
        return self._call("hive_health", {})

    # ── the one transport path ─────────────────────────────────────────────────
    def _call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self._next_id += 1
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": self._next_id,
                "method": "tools/call",
                "params": {"name": tool, "arguments": arguments},
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            self.url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.token}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                status = int(resp.status)
                raw = resp.read()
        except urllib.error.HTTPError as e:  # 401/403/413/429/5xx
            raise HiveError(
                f"HTTP {e.code} calling {tool}", http_status=int(e.code)
            ) from e
        except (urllib.error.URLError, OSError) as e:  # refused/DNS/timeout
            raise HiveError(f"transport failure calling {tool}: {e}") from e

        try:
            envelope = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            raise HiveError(f"non-JSON reply to {tool}", http_status=status) from e
        if not isinstance(envelope, dict):
            raise HiveError(
                f"malformed JSON-RPC envelope from {tool}", http_status=status
            )
        err = envelope.get("error")
        if err is not None:  # protocol-level error
            msg = err.get("message") if isinstance(err, dict) else str(err)
            raise HiveError(
                f"rpc error from {tool}: {msg}",
                http_status=status,
                rpc_error=err if isinstance(err, dict) else None,
            )
        # An arbitrary (or absent) JSON value on purpose: a malformed shape — None, a
        # non-dict, a missing key — lands in the except below, never a type guarantee.
        result: Any = envelope.get("result")
        try:  # tools/call framing
            text = result["content"][0]["text"]
        except (TypeError, KeyError, IndexError) as e:
            raise HiveError(
                f"malformed tool result from {tool}", http_status=status
            ) from e
        if result.get("isError"):  # tool-level error
            raise HiveError(f"tool error from {tool}: {text}", http_status=status)
        try:
            payload = json.loads(text)
        except ValueError as e:
            raise HiveError(
                f"non-JSON tool payload from {tool}", http_status=status
            ) from e
        if not isinstance(payload, dict):
            raise HiveError(f"non-object tool payload from {tool}", http_status=status)
        return payload
