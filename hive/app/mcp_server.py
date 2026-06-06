"""HiveMCPServer — the single MCP/JSON-RPC trust boundary (M06 / C7).

A thin driving adapter (hexagonal): it maps eight ``hive_*`` JSON-RPC verbs onto the
already-built domain ports and owns NO data. PORT+EXTEND of the AgentCortex
``serving/mcp_server.py`` stdio loop — KEEP the MCPRequest/MCPResponse/_err envelopes,
the dispatch table, ``tools/list = TOOL_DEFINITIONS``, and the
``{content:[{type:text,text:json}], isError}`` framing.

Two load-bearing belts live HERE (in addition to the store-query suspenders):
  1. **Schema enforcement** — ``_validate_args`` runs over TOOL_DEFINITIONS BEFORE any
     port is touched; a malformed call never reaches the store (handlers read args
     permissively via ``.get`` so this belt is the SOLE required-field guard).
  2. **Approved-only recall** — ``_handle_recall`` re-filters every candidate to
     ``status=='approved'`` independent of the pipeline/index, and an empty post-belt
     set is an ABSTAIN, never a confident-empty (never-hallucinate, structural).

Recalled text is framed under ``reference_context`` (never ``instructions``); every
recall envelope carries ``trace_id`` on hit AND abstain (the §11 move-#6 join key).
The stdio loop never crashes on a tool exception — the stack is logged (stderr/file),
never returned to the agent; stdout carries only JSON-RPC.

NOT pure: this is the ``hive/app`` adapter layer (json/os/sys permitted). The process
entrypoint (argparse + real-adapter wiring) is deferred to the container chunk (P1.13);
this module is the surface + handlers + loop, wired from injected ports.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Callable, Optional

from hive.app.tool_defs import TOOL_DEFINITIONS
from hive.domain.errors import SecretRefused
from hive.domain.models import CONFIDENT, AgentContext
from hive.domain.secret_scan import _REDACTION

_log = logging.getLogger("hive.app.mcp_server")

MCP_VERSION = "2024-11-05"
SERVER_NAME = "hive"
SERVER_VERSION = "1.0.0"

_PREVIEW_LEN = 160
# inputSchema by tool name — the single source for the pre-dispatch validation belt.
_SCHEMA_BY_NAME: dict[str, dict] = {t["name"]: t["inputSchema"] for t in TOOL_DEFINITIONS}


# ── JSON-RPC envelopes (PORT) ────────────────────────────────────────────────

@dataclass
class MCPRequest:
    id: Any
    method: str
    params: dict


@dataclass
class MCPResponse:
    id: Any
    result: Optional[dict] = None
    error: Optional[dict] = None

    def to_json(self) -> str:
        out: dict[str, Any] = {"jsonrpc": "2.0", "id": self.id}
        if self.error is not None:
            out["error"] = self.error
        else:
            out["result"] = self.result
        return json.dumps(out, default=_json_default)


def _err(code: int, message: str, data: Any = None) -> dict:
    e = {"code": code, "message": message}
    if data is not None:
        e["data"] = data
    return e


def _json_default(obj: Any) -> Any:
    if hasattr(obj, "tolist"):
        return obj.tolist()
    if isinstance(obj, (bytes, bytearray)):
        return obj.hex()
    return str(obj)


@dataclass(frozen=True, slots=True)
class ServerIdentity:
    """One process == one identity. ``tenant_id`` is a constant label, never a query
    filter (single-tenant); ``agent_id`` is the default ``proposed_by`` on write."""
    tenant_id: str = "default"
    agent_id: str = "agent"


# ── pre-dispatch schema validation belt (§1.2 — runs before any port) ─────────

def _type_ok(val: Any, t: str) -> bool:
    """JSON-schema scalar/container type check. ``bool`` is excluded from integer/
    number (Python bool ⊂ int would otherwise pass an integer field)."""
    if t == "string":
        return isinstance(val, str)
    if t == "integer":
        return isinstance(val, int) and not isinstance(val, bool)
    if t == "number":
        return isinstance(val, (int, float)) and not isinstance(val, bool)
    if t == "boolean":
        return isinstance(val, bool)
    if t == "array":
        return isinstance(val, list)
    if t == "object":
        return isinstance(val, dict)
    return True


def _validate_args(name: str, args: dict) -> Optional[str]:
    """Return an error message if ``args`` violate the tool's inputSchema, else None.
    Checks required[], scalar types, and enum membership. Extra fields are ignored
    (permissive). // O(fields) time. The deleting of the CALL to this is RULE-2
    mutation #1 — handlers do no required-field guard of their own."""
    schema = _SCHEMA_BY_NAME.get(name)
    if schema is None:
        return None  # unknown tool handled by the dispatcher (JSON-RPC -32602)
    props = schema.get("properties", {})
    for req in schema.get("required", []):
        if req not in args:
            return f"missing required field: {req!r}"
    for key, val in args.items():
        spec = props.get(key)
        if spec is None:
            continue
        t = spec.get("type")
        if t is not None and not _type_ok(val, t):
            return f"field {key!r} must be of type {t}"
        enum = spec.get("enum")
        if enum is not None and val not in enum:
            return f"field {key!r} must be one of {enum}"
    return None


def _preview(text: str) -> str:
    text = text or ""
    return text if len(text) <= _PREVIEW_LEN else text[:_PREVIEW_LEN] + "…"


def _scan_report(verdict) -> dict:
    """The ScanReport envelope — rule NAMES + count only, NEVER the secret bytes."""
    return {"action": verdict.action,
            "rules": [f.rule for f in verdict.findings],
            "n_findings": len(verdict.findings)}


def _abstain_note(state: str) -> str:
    """A neutral, non-instruction note for an empty/abstained recall."""
    if state == "EMPTY_NO_DATA":
        return "no approved memories are available to match this query"
    return "no confident match — abstained rather than surface a weak guess"


class HiveMCPServer:
    """JSON-RPC 2.0 over stdio. One process == one ``ServerIdentity``."""

    def __init__(self, *, admission, recall, store, embedder, install_planner,
                 identity: ServerIdentity, now: Callable[[], int],
                 started_ts: int = 0, db_path: str = "",
                 trailer_key: str = "Hive-Trace") -> None:
        self.admission = admission          # AdmissionService: write/list_pending/approve/reject
        self.recall = recall                # RecallPipeline: recall(query,*,agent_id,agent_ctx)
        self.store = store                  # EpisodeStore: get_episode (belt) / fetch / counts
        self.embedder = embedder            # EmbeddingProvider: health probes (d, w_version, name)
        self.install_planner = install_planner  # InstallPlanner: hive_init plan/confirm
        self.identity = identity
        self.now = now
        self.started_ts = int(started_ts)
        self.db_path = db_path
        self.trailer_key = trailer_key
        self._tool_handlers: dict[str, Callable[[dict], dict]] = {
            "hive_write": self._handle_write,
            "hive_recall": self._handle_recall,
            "hive_fetch": self._handle_fetch,
            "hive_pending": self._handle_pending,
            "hive_approve": self._handle_approve,
            "hive_reject": self._handle_reject,
            "hive_init": self._handle_init,
            "hive_health": self._handle_health,
        }

    # ── protocol dispatch (PORT) ──────────────────────────────────────────────
    def handle(self, req: MCPRequest) -> MCPResponse:
        if req.method == "initialize":
            return MCPResponse(id=req.id, result={
                "protocolVersion": MCP_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION}})
        if req.method == "tools/list":
            return MCPResponse(id=req.id, result={"tools": TOOL_DEFINITIONS})
        if req.method == "tools/call":
            return self._tools_call(req)
        if req.method == "ping":
            return MCPResponse(id=req.id, result={})
        return MCPResponse(id=req.id, error=_err(-32601, f"method not found: {req.method}"))

    def _tool_result(self, req_id: Any, content: dict, *, is_error: bool) -> MCPResponse:
        return MCPResponse(id=req_id, result={
            "content": [{"type": "text", "text": json.dumps(content, default=_json_default)}],
            "isError": is_error})

    def _tool_error(self, req_id: Any, message: str) -> MCPResponse:
        return MCPResponse(id=req_id, result={
            "content": [{"type": "text", "text": message}], "isError": True})

    def _tools_call(self, req: MCPRequest) -> MCPResponse:
        name = req.params.get("name")
        args = req.params.get("arguments", {}) or {}
        handler = self._tool_handlers.get(name)
        if handler is None:                                  # unknown tool → JSON-RPC error
            _log.warning("mcp.unknown_tool", extra={"event": "mcp.unknown_tool",
                         "tool": name, "agent_id": self.identity.agent_id})
            return MCPResponse(id=req.id, error=_err(-32602, f"unknown tool: {name}"))
        # ── BELT 1: schema validation BEFORE any port is touched (RULE-2 mut #1) ──
        verr = _validate_args(name, args)
        if verr is not None:
            _log.info("mcp.schema_reject", extra={"event": "mcp.schema_reject",
                      "tool": name, "reason": verr, "agent_id": self.identity.agent_id})
            return self._tool_error(req.id, f"invalid arguments: {verr}")
        try:
            content = handler(args)
            return self._tool_result(req.id, content, is_error=False)
        except SecretRefused:
            # only hive_write raises this and it returns its own refused envelope;
            # reaching here means a defensive double-raise — treat as a tool error.
            raise
        except Exception as e:                               # loop survives; stack → log, NOT agent
            _log.error("mcp.tool_raised", extra={"event": "mcp.tool_raised",
                       "tool": name, "agent_id": self.identity.agent_id,
                       "error_type": type(e).__name__}, exc_info=True)
            return self._tool_error(req.id, f"error: {type(e).__name__}: {e}")

    # ── tool handlers (args read permissively; _validate_args is the only guard) ──
    def _handle_write(self, args: dict) -> dict:
        text = args.get("text")
        source = args.get("source") or ""
        tags = args.get("tags") or []
        tags_str = ",".join(str(t) for t in tags) if isinstance(tags, list) else str(tags)
        proposed_by = args.get("proposed_by") or self.identity.agent_id
        try:
            res = self.admission.write(text, source=source, proposed_by=proposed_by,
                                       tags=tags_str)
        except SecretRefused as e:
            # REFUSE: nothing staged (0 rows). Envelope carries rule NAMES, never bytes.
            return {"status": "refused", "reason": str(e),
                    "scan": {"action": "refuse", "rules": e.rules,
                             "n_findings": e.n_findings}}
        out: dict = {"status": res.status, "id": res.pending_id,
                     "content_hash": res.content_hash, "scan": _scan_report(res.scan)}
        if res.status == "redacted" and res.scan.redacted_text is not None:
            out["redacted_preview"] = _preview(res.scan.redacted_text)
        return out

    def _handle_recall(self, args: dict) -> dict:
        query = args.get("query") or ""
        ctx = AgentContext(repo_remote=args.get("repo_remote") or "",
                           language=args.get("language") or "",
                           workflow=args.get("workflow") or "general")
        result = self.recall.recall(query, agent_id=self.identity.agent_id, agent_ctx=ctx)
        # ── BELT 2: approved-only re-filter, independent of the index (RULE-2 mut #2) ──
        hits: list[dict] = []
        for h in result.hits:
            ep = self.store.get_episode(h.episode_id)
            if ep is None or ep.status != "approved":        # ← delete this guard ⇒ mut #2
                _log.warning("mcp.recall_belt_drop", extra={"event": "mcp.recall_belt_drop",
                             "trace_id": result.trace_id, "episode_id": h.episode_id})
                continue
            hits.append({"episode_id": h.episode_id, "text": h.text,
                         "sim": float(h.sim), "content_hash": ep.content_hash})
        # an empty post-belt set is an ABSTAIN, never a confident-empty (never-hallucinate)
        abstained = (result.state != CONFIDENT) or (not hits)
        env: dict = {"reference_context": hits, "abstained": abstained,
                     "trace_id": result.trace_id, "state": result.state,
                     "entropy_norm": float(result.entropy_norm)}
        if abstained:
            _log.info("mcp.recall_abstain", extra={"event": "mcp.recall_abstain",
                      "trace_id": result.trace_id, "state": result.state,
                      "entropy_norm": float(result.entropy_norm)})
            env["note"] = _abstain_note(result.state)
        return env

    def _handle_fetch(self, args: dict) -> dict:
        h = args.get("content_hash") or ""
        text = self.store.fetch(h)                           # clean miss → None, never raises
        return {"found": text is not None, "text": text}

    def _handle_pending(self, args: dict) -> dict:
        since = int(args.get("since") or 0)
        pend: list[dict] = []
        for r in self.admission.list_pending(since=since):
            # REDACT provenance is not a DB column in v-min; the stored text is
            # clean-by-construction — report REDACT iff the mask marker is present.
            verdict = "REDACT" if _REDACTION in (r.text or "") else "PASS"
            pend.append({"id": r.id, "text_preview": _preview(r.text),
                         "proposed_by": r.proposed_by, "ts": r.ts,
                         "scan_verdict": verdict})
        return {"pending": pend, "count": len(pend)}

    def _handle_approve(self, args: dict) -> dict:
        ids = [int(i) for i in (args.get("ids") or [])]
        approver = args.get("approver") or ""
        approved, skipped = self.admission.approve(ids, approver)
        return {"approved": approved, "skipped": skipped, "approver": approver}

    def _handle_reject(self, args: dict) -> dict:
        ids = [int(i) for i in (args.get("ids") or [])]
        keep = bool(args.get("keep_rejected") or False)
        rejected, skipped = self.admission.reject(ids, keep_rejected=keep)
        return {"rejected": rejected, "skipped": skipped}

    def _handle_init(self, args: dict) -> dict:
        repo_path = args.get("repo_path") or ""
        harness = args.get("harness") or "generic"
        rules_file = args.get("rules_file")
        confirm_hash = args.get("confirm_hash")
        if confirm_hash:                                     # phase 2: hash-verified link
            res = self.install_planner.confirm(repo_path, bytes.fromhex(confirm_hash))
            out = {"phase": 2}
            out.update(res)
            return out
        plan = self.install_planner.plan(repo_path, harness, rules_file)
        block = plan.rules_block
        return {"phase": 1, "rules_file": plan.rules_file, "harness": plan.harness,
                "rules_block": block.rendered_text,
                "trailer_key": block.trailer_key,           # == producer.stamp_trailer (single source)
                "block_version": block.block_version,
                "expected_confirm_hash": plan.expected_confirm_hash.hex(),
                "watch_warning": plan.watch_warning}

    def _handle_health(self, args: dict) -> dict:
        repo_path = args.get("repo_path")
        try:
            n_episodes, n_pending = self.store.counts()      # probe: store + (below) embedder
            snap: dict = {
                "ok": True, "tenant_id": self.identity.tenant_id, "db_path": self.db_path,
                "db_size_bytes": self._db_size(), "n_episodes": n_episodes,
                "n_pending": n_pending,
                "embedder": str(getattr(self.embedder, "name", "unknown")),
                "embedder_loaded": bool(getattr(self.embedder, "loaded", True)),
                "embedder_projection": "pca",
                "W_version": int(getattr(self.embedder, "w_version", 0)),
                "d": int(getattr(self.embedder, "d", 0)),
                "index_authoritative": bool(self.recall.index.is_authoritative()),
                "uptime_s": max(0, int(self.now()) - self.started_ts),
                "trailer_key": self.trailer_key}
            if repo_path is not None:
                linked, link = self._link_status(repo_path)
                snap["linked"] = linked
                snap["link"] = link
            return snap
        except Exception as e:                               # fail-closed subset ONLY
            _log.error("mcp.health_probe_fail", extra={"event": "mcp.health_probe_fail",
                       "error_type": type(e).__name__, "db_path": self.db_path},
                       exc_info=True)
            return {"ok": False, "error": f"{type(e).__name__}: {e}",
                    "db_path": self.db_path}

    # ── health helpers ────────────────────────────────────────────────────────
    def _db_size(self) -> int:
        try:
            return os.path.getsize(self.db_path) if self.db_path and \
                os.path.exists(self.db_path) else 0
        except OSError:
            return 0

    def _link_status(self, repo_path: str) -> tuple[bool, Optional[dict]]:
        """Best-effort link probe (the additive M07 health extension). The concrete
        InstallPlanner may expose ``link_status``; absent it, report unlinked."""
        probe = getattr(self.install_planner, "link_status", None)
        if probe is None:
            return False, None
        try:
            linked, link = probe(repo_path)
            return bool(linked), link
        except Exception:
            return False, None


# ── stdio loop (PORT verbatim; stderr-clean so stdout JSON-RPC is unpolluted) ──

def run_stdio(server: HiveMCPServer, in_stream, out_stream) -> None:
    """Read newline-delimited JSON-RPC requests from ``in_stream``, write replies to
    ``out_stream``. A parse error replies -32700; a non-object payload replies -32600;
    a request with no ``id`` is a notification (no reply); a non-object ``params`` is
    coerced to ``{}`` so a handler never sees a malformed shape; and ANY unforeseen
    ``handle()`` exception becomes a -32603 reply. The loop NEVER crashes on a single
    bad line (invariant 6 / AUDIT wf_1943a559)."""
    for line in in_stream:
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as e:
            _log.warning("mcp.parse_error", extra={"event": "mcp.parse_error",
                         "line_len": len(line)})
            out_stream.write(json.dumps({"jsonrpc": "2.0", "id": None,
                             "error": {"code": -32700, "message": f"parse error: {e}"}}) + "\n")
            out_stream.flush()
            continue
        if not isinstance(payload, dict):                    # batch array / bare scalar
            _log.warning("mcp.invalid_request", extra={"event": "mcp.invalid_request",
                         "payload_type": type(payload).__name__})
            out_stream.write(json.dumps({"jsonrpc": "2.0", "id": None, "error":
                             {"code": -32600, "message": "invalid request: expected a JSON object"}}) + "\n")
            out_stream.flush()
            continue
        if "id" not in payload:                              # notification → no response
            continue
        raw_params = payload.get("params")
        params = raw_params if isinstance(raw_params, dict) else {}  # never let .get crash
        req = MCPRequest(id=payload.get("id"), method=payload.get("method", ""),
                         params=params)
        try:
            resp = server.handle(req)
        except Exception as e:                               # last-resort guard: loop survives
            _log.error("mcp.handle_crash", extra={"event": "mcp.handle_crash",
                       "error_type": type(e).__name__}, exc_info=True)
            resp = MCPResponse(id=req.id, error=_err(-32603, f"internal error: {type(e).__name__}"))
        out_stream.write(resp.to_json() + "\n")
        out_stream.flush()
