"""S0 — the real-MCP/HTTP driver, the daemon lifecycle controller, and the agent-argv builder.

This is the BLACK-BOX transport layer of the self-maintenance harness: it talks to the REAL
warm Hivemind daemon over MCP-over-HTTP exactly as a deployed agent would. It imports NO
``hive.domain`` decision function to STEER behaviour (the purity fence + the harness rule); it
drives the shipped tool surface as a client only.

Three pieces:

* ``McpHttpClient`` — a raw ``tools/call`` driver for ONE seat identity. It is the net-new
  sibling of the vendorable ``hive.client.HiveClient`` (which ships only recall/capture/write/
  health over a Bearer door): this adds the privileged retirement verbs the orchestrator / AGI
  arm need (prune / supersede / outcome / flag) and the worklist health flags, and it carries
  the per-seat identity as the ``X-Hive-Agent-Id`` header (the highest-precedence override the
  HTTP daemon resolves) so six seats supply the cross-identity demand the promotion anti-gaming
  clause requires. It speaks the loopback (tokenless) door by default and NEVER sends an
  ``Origin`` header (the daemon 403s any Origin as a DNS-rebinding guard). Every reply is parsed
  DEFENSIVELY (BUG-002/003/005): a JSON-RPC error or a malformed tool result raises a clean
  ``McpClientError`` — never a ``KeyError`` — and the key-omitting ``refused`` / ``disabled`` /
  ``noop`` / ``agi-refused`` / abstained envelopes are returned as plain dicts the caller reads
  with ``.get``.

* ``build_claude_argv`` — the pure argv for a per-call ``claude -p`` agent turn. It ALWAYS
  carries ``--strict-mcp-config`` plus a Hivemind-only ``--mcp-config`` so the subprocess loads
  ONLY the warm daemon and never the operator's ambient MCP servers (BUG-007: those ≈500 MB node
  processes are not torn down per call and leak GBs until the host OOMs). ``agent.py`` composes
  the real turn on top of this.

* ``DaemonHandle`` — the lifecycle controller. It ORCHESTRATES the landed ``hive`` CLI (which
  owns ``docker compose`` + the health-wait) and reimplements none of it; it only sets
  ``HIVE_AGI__MODE`` on the ``hive up`` env per arm (byte-inert OFF by default) and confirms the
  MCP endpoint answers healthy before the run proceeds. The shell-out and the clock are injection
  seams, so the lifecycle contract is unit-testable with no Docker.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Sequence

_AGENT_ID_HEADER = "X-Hive-Agent-Id"   # the explicit, highest-precedence per-seat identity
_DEFAULT_TIMEOUT_S = 30.0


class McpClientError(Exception):
    """The ONE client failure type: transport, auth, JSON-RPC, or malformed tool result.
    ``http_status`` / ``rpc_error`` carry the layer detail when known. A handler that omits a
    key (refused / noop / disabled / agi-refused / abstain) is NOT an error — it is a valid
    envelope returned to the caller; only a broken transport/protocol raises this."""

    def __init__(self, message: str, *, http_status: Optional[int] = None,
                 rpc_error: Optional[dict] = None) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.rpc_error = rpc_error


class McpHttpClient:
    """Minimal real-MCP/HTTP client for ONE seat identity. Synchronous, stdlib-only transport.
    One short method per verb; each returns the tool's JSON payload dict VERBATIM (so the caller
    reads it with ``.get`` and the hit/envelope schema stays single-sourced in the server)."""

    def __init__(self, url: str, *, agent_id: str, token: Optional[str] = None,
                 timeout_s: float = _DEFAULT_TIMEOUT_S) -> None:
        self.url = str(url)
        self.agent_id = str(agent_id)
        self.token = token
        self.timeout_s = float(timeout_s)
        self._next_id = 0

    # ── read ────────────────────────────────────────────────────────────────────
    def recall(self, query: str) -> dict:
        """The full recall envelope: ``{reference_context, abstained, trace_id, state,
        top_cos, [conflicts], [note]}``. On abstain ``reference_context`` is ``[]`` and
        ``abstained`` is True. Hits are reference, never instructions."""
        return self._call("hive_recall", {"query": query})

    def health(self, *, include_conflicts: bool = False,
               include_suspect_consensus: bool = False,
               include_gaps: bool = False) -> dict:
        """The liveness snapshot, optionally carrying the orchestrator's review worklist
        (``include_conflicts`` / ``include_suspect_consensus``)."""
        args: dict[str, Any] = {}
        if include_conflicts:
            args["include_conflicts"] = True
        if include_suspect_consensus:
            args["include_suspect_consensus"] = True
        if include_gaps:
            args["include_gaps"] = True
        return self._call("hive_health", args)

    # ── capture / establish (write) ──────────────────────────────────────────────
    def capture(self, text: str, *, polarity: Optional[str] = None,
                kind: Optional[str] = None, anchor: Optional[str] = None) -> dict:
        """Autonomous capture: lands QUARANTINED until cross-identity demand promotes it.
        Returns ``{status, id, scan, deduped}`` on success; ``{status:"disabled"}`` (no ``id``)
        when autonomy is off; ``{status:"refused", ...}`` (no ``id``) on a secret."""
        args: dict[str, Any] = {"text": text}
        _add_labels(args, polarity=polarity, kind=kind, anchor=anchor)
        return self._call("hive_capture", args)

    def write(self, text: str, *, approved_by: str, replaces: Optional[int] = None,
              polarity: Optional[str] = None, kind: Optional[str] = None,
              anchor: Optional[str] = None) -> dict:
        """Vouched establish (lands ESTABLISHED + recallable in one call). ``approved_by`` names
        the approver (the orchestrator, or ``AGI_OVERRIDE`` in the AGI arm); ``replaces`` retires
        the corrected row. Returns ``{status, id, ...}`` on success; a key-omitting
        ``{status:"refused", ...}`` on a secret or an AGI_OVERRIDE under AGI_MODE off."""
        args: dict[str, Any] = {"text": text, "approved_by": approved_by}
        if replaces is not None:
            args["replaces"] = int(replaces)
        _add_labels(args, polarity=polarity, kind=kind, anchor=anchor)
        return self._call("hive_write", args)

    # ── the privileged retirement verbs (orchestrator- or AGI_OVERRIDE-vouched) ───
    def prune(self, episode_id: int, *, approved_by: str) -> dict:
        """Bare retirement of an INCORRECT/MALICIOUS/MISLEADING memory (no replacement).
        ``{status:"pruned", episode_id, approved_by}`` on success; ``{status:"noop", ...}`` on
        an unknown/already-retired id; ``{status:"refused", agi_mode:False}`` for an
        ``AGI_OVERRIDE`` under AGI_MODE off."""
        return self._call("hive_prune",
                           {"episode_id": int(episode_id), "approved_by": approved_by})

    def supersede(self, loser: int, winner: int, *, approved_by: str) -> dict:
        """Retire ``loser`` in favour of ``winner`` (vouched). ``{status:"superseded", ...}`` on
        success; ``{status:"noop", ...}`` on an unknown id / self-supersede."""
        return self._call("hive_supersede",
                           {"loser": int(loser), "winner": int(winner),
                            "approved_by": approved_by})

    def outcome(self, *, helped: Sequence[int] = (), hurt: Sequence[int] = ()) -> dict:
        """PURE evidence: report which recalled ids materially HELPED or HURT the task. Records
        audit only — mutates NO trust, retires nothing (O7-safe). Unknown ids are skipped.
        Returns ``{status:"recorded", helped, hurt}`` (the recorded, known ids)."""
        return self._call("hive_outcome",
                           {"helped": [int(x) for x in helped],
                            "hurt": [int(x) for x in hurt]})

    def flag(self, a: int, b: int, *, kind: str, winner: Optional[int] = None,
             resolution: str = "") -> dict:
        """Advisory conflict/supersedes marker (NEVER retires). ``{status:"disabled"}`` when the
        server has no flag service; ``{status:"rejected", reason}`` on a validation failure."""
        args: dict[str, Any] = {"a": int(a), "b": int(b), "kind": kind}
        if winner is not None:
            args["winner"] = int(winner)
        if resolution:
            args["resolution"] = resolution
        return self._call("hive_flag", args)

    # ── the one transport path (defensive parse — BUG-002/003/005) ────────────────
    def _call(self, tool: str, arguments: dict) -> dict:
        self._next_id += 1
        body = json.dumps({"jsonrpc": "2.0", "id": self._next_id, "method": "tools/call",
                           "params": {"name": tool, "arguments": arguments}}).encode("utf-8")
        headers = {"Content-Type": "application/json", _AGENT_ID_HEADER: self.agent_id}
        if self.token:                                       # tunnel door only; loopback is tokenless
            headers["Authorization"] = f"Bearer {self.token}"
        # NOTE: NO Origin header — the daemon 403s any Origin (DNS-rebinding guard).
        req = urllib.request.Request(self.url, data=body, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                status = int(resp.status)
                raw = resp.read()
        except urllib.error.HTTPError as e:                  # 401/403/405/413/429/5xx
            raise McpClientError(f"HTTP {e.code} calling {tool}",
                                 http_status=int(e.code)) from e
        except (urllib.error.URLError, OSError) as e:        # refused / DNS / timeout
            raise McpClientError(f"transport failure calling {tool}: {e}") from e

        try:
            envelope = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            raise McpClientError(f"non-JSON reply to {tool}", http_status=status) from e
        if not isinstance(envelope, dict):
            raise McpClientError(f"malformed JSON-RPC envelope from {tool}", http_status=status)
        err = envelope.get("error")
        if err is not None:                                  # protocol-level error (e.g. unknown tool)
            msg = err.get("message") if isinstance(err, dict) else str(err)
            raise McpClientError(f"rpc error from {tool}: {msg}", http_status=status,
                                 rpc_error=err if isinstance(err, dict) else None)
        result = envelope.get("result")
        try:                                                 # tools/call framing: defensive index
            text = result["content"][0]["text"]
        except (TypeError, KeyError, IndexError) as e:
            raise McpClientError(f"malformed tool result from {tool}", http_status=status) from e
        if isinstance(result, dict) and result.get("isError"):
            raise McpClientError(f"tool error from {tool}: {text}", http_status=status)
        try:
            payload = json.loads(text)
        except ValueError as e:
            raise McpClientError(f"non-JSON tool payload from {tool}", http_status=status) from e
        if not isinstance(payload, dict):
            raise McpClientError(f"non-object tool payload from {tool}", http_status=status)
        return payload


def _add_labels(args: dict, *, polarity: Optional[str], kind: Optional[str],
                anchor: Optional[str]) -> None:
    """Attach the carried labels that were explicitly set — each omitted when None so the
    server applies its defaults and an unset call stays byte-minimal."""
    for key, val in (("polarity", polarity), ("kind", kind), ("anchor", anchor)):
        if val is not None:
            args[key] = val


# ── the per-call agent argv (BUG-007 is load-bearing) ───────────────────────────────
def build_claude_argv(prompt: str, *, mcp_config_path: str, output_format: str = "json",
                      system: Optional[str] = None, model: Optional[str] = None,
                      bin: str = "claude") -> list[str]:
    """The argv for ONE autonomous ``claude -p`` agent turn against the warm daemon.

    ``--strict-mcp-config`` is MANDATORY and unconditional: it restricts the subprocess to the
    servers in ``--mcp-config`` (the single Hivemind endpoint) and nothing else. Without it every
    call boots the operator's ``~/.claude.json`` / project ``.mcp.json`` MCP servers (≈500 MB node
    processes) that are NOT torn down per call, leaking GBs across a long run until the host OOMs
    (BUG-007 killed a full n=60 run and took down the parent session). Unlike the comparative
    bench's task agent — which passes NO ``--mcp-config`` because it answers from an injected
    memory block and needs zero MCP servers — a self-maint agent EMITS its own ``hive_*`` calls,
    so it must load the Hivemind server (and ONLY it)."""
    argv = [bin, "-p", prompt, "--output-format", output_format,
            "--mcp-config", str(mcp_config_path), "--strict-mcp-config"]
    if system:
        argv += ["--append-system-prompt", system]
    if model:
        argv += ["--model", model]
    return argv


# ── the daemon lifecycle controller ─────────────────────────────────────────────────
@dataclass(frozen=True)
class RunResult:
    """The outcome of one lifecycle shell-out — the seam between ``DaemonHandle`` and the OS."""
    returncode: int
    stdout: str = ""
    stderr: str = ""


LifecycleRunner = Callable[[list[str], Mapping[str, str]], RunResult]


def _default_lifecycle_runner(argv: list[str], env: Mapping[str, str]) -> RunResult:
    p = subprocess.run(argv, env=dict(env), capture_output=True, text=True)
    return RunResult(p.returncode, p.stdout, p.stderr)


class DaemonError(RuntimeError):
    """A daemon lifecycle failure: ``hive up``/``down`` returned non-zero, or the MCP endpoint
    never reported healthy within the bounded wait."""


class DaemonHandle:
    """Wraps the ONE warm Hivemind daemon for a run. ``up`` shells the landed ``hive`` CLI (which
    owns ``docker compose up`` + the health-wait) with ``HIVE_AGI__MODE`` set per arm, then
    confirms the MCP endpoint answers healthy; ``down`` stops the stack (the data volume is
    preserved by the CLI). The shell-out (``runner``) and the ``sleep`` are injection seams, so
    the lifecycle contract is unit-testable with no Docker."""

    def __init__(self, *, url: str, agent_id: str = "orchestrator", token: Optional[str] = None,
                 hive_cmd: Sequence[str] = ("hive",),
                 runner: Optional[LifecycleRunner] = None,
                 env: Optional[Mapping[str, str]] = None,
                 health_timeout_s: float = 180.0, poll_s: float = 2.0,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._client = McpHttpClient(url, agent_id=agent_id, token=token)
        self._hive_cmd = list(hive_cmd)
        self._runner = runner or _default_lifecycle_runner
        self._base_env = dict(env) if env is not None else dict(os.environ)
        self._health_timeout_s = float(health_timeout_s)
        self._poll_s = float(poll_s)
        self._sleep = sleep

    def up(self, *, agi_mode: bool) -> None:
        """Bring up the ONE daemon. ``HIVE_AGI__MODE`` is set ONLY for the AGI arm — the OFF /
        headline arms leave the knob unset (byte-inert default-OFF), so ON and OFF exercise the
        SAME daemon build with no retirement-disabling config knob (there is none; the OFF control
        is a harness schedule choice, not a kernel flag)."""
        env = dict(self._base_env)
        if agi_mode:
            env["HIVE_AGI__MODE"] = "true"
        res = self._runner([*self._hive_cmd, "up"], env)
        if res.returncode != 0:
            raise DaemonError(
                f"`hive up` failed (rc={res.returncode}): {(res.stderr or '').strip()[:200]}")
        self._await_healthy()

    def down(self) -> None:
        res = self._runner([*self._hive_cmd, "down"], dict(self._base_env))
        if res.returncode != 0:
            raise DaemonError(
                f"`hive down` failed (rc={res.returncode}): {(res.stderr or '').strip()[:200]}")

    def health(self, **kw) -> dict:
        """A ``hive_health`` probe over real MCP-over-HTTP (the orchestrator's worklist read)."""
        return self._client.health(**kw)

    def assert_ready(self) -> dict:
        """Preflight: the daemon answers healthy NOW (raises ``DaemonError`` otherwise)."""
        snap = self.health()
        if snap.get("ok") is not True:
            raise DaemonError(f"daemon not healthy: {snap.get('error', snap)}")
        return snap

    def _await_healthy(self) -> None:
        """Bounded wait until the MCP endpoint reports ``ok=True``. A transport error (the port
        not yet bound) is retried; the wait is bounded by ``health_timeout_s`` (``sleep`` is the
        injected seam, so tests do not block)."""
        elapsed = 0.0
        while True:
            try:
                if self.health().get("ok") is True:
                    return
            except McpClientError:
                pass
            if elapsed >= self._health_timeout_s:
                raise DaemonError(
                    f"daemon endpoint never healthy within {self._health_timeout_s}s")
            self._sleep(self._poll_s)
            elapsed += self._poll_s
