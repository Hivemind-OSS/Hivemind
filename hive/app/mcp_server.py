"""HiveMCPServer — the single MCP/JSON-RPC trust boundary (M06 / C7).

A thin driving adapter (hexagonal): it maps five ``hive_*`` JSON-RPC verbs onto the
already-built domain ports and owns NO data. PORT+EXTEND of the AgentCortex
``serving/mcp_server.py`` stdio loop — KEEP the MCPRequest/MCPResponse/_err envelopes,
the dispatch table, ``tools/list = TOOL_DEFINITIONS``, and the
``{content:[{type:text,text:json}], isError}`` framing. The server-side approval queue
(hive_pending/approve/reject) was removed — capture is client-gated: a clean hive_write
is scanned, then stored APPROVED in one call, attributed to the caller's ``approved_by``.

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

NOT pure: this is the ``hive/app`` adapter layer (json/os/sys permitted). The module is
the surface + handlers + loop (wired from injected ports) PLUS the per-session process
entrypoint ``main()`` at the bottom — ``python -m hive.app.mcp_server``, the Tier-0
transport an IDE execs inside the warm container. It reuses ``build_container`` but is
marker-INERT: it NEVER touches the PID-1 boot readiness markers the container healthcheck
reads (that policy stays in ``hive.tools.entrypoint``, the long-lived ENTRYPOINT).
"""
from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from hive.app.gaps import cluster_misses, contested_misses
from hive.app.trends import compute_trends
from hive.app.onboard import (
    ONBOARDING_MANIFEST_VERSION, onboarding_hint, provenance_banner,
)
from hive.app.tool_defs import TOOL_DEFINITIONS
from hive.domain.errors import SecretRefused
from hive.domain.lifecycle import is_servable
from hive.domain.models import CONFIDENT, AgentContext

_log = logging.getLogger("hive.app.mcp_server")

_DAY_S = 86_400

MCP_VERSION = "2024-11-05"
SERVER_NAME = "hive"
SERVER_VERSION = "1.0.0"

_PREVIEW_LEN = 160
# inputSchema by tool name — the single source for the pre-dispatch validation belt.
_SCHEMA_BY_NAME: dict[str, dict] = {t["name"]: t["inputSchema"] for t in TOOL_DEFINITIONS}

# §7 V4 substrate: the capture verb(s) whose server-arrival is stamped as
# meta[hook:last_seen:<tool>], so hive_health.hooks_seen lets the onboarding agent confirm a
# Tier-2 OS hook actually reached the server. ONLY the capture verbs are stamped — recall/
# health/init/fetch stay pure reads (no write-amplification on the read path).
_HOOK_SEEN_PREFIX = "hook:last_seen:"
_HOOK_SEEN_TOOLS: frozenset = frozenset({"hive_write", "hive_capture"})


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


def _manifest_report(m) -> dict:
    """Serialize the abstract HookManifest (the harness-independent 'manifest JSON' the
    generic profile self-applies). Explicit — _json_default would str() the dataclass."""
    return {"manifest_version": m.manifest_version,
            "hooks": [{"event": h.event, "action": h.action, "directive": h.directive}
                      for h in m.hooks]}


def _recipe_report(r) -> dict:
    """Serialize the per-harness HarnessRecipe: resolved tier, the Tier-≥1 rules addendum,
    the NL playbook, and (Tier-2 only) the hook files the agent materializes."""
    return {"harness": r.harness, "resolved_tier": r.resolved_tier,
            "manifest_version": r.manifest_version, "rules_addendum": r.rules_addendum,
            "playbook": r.playbook,
            "hook_files": [{"path": f.path, "content": f.content, "mechanism": f.mechanism}
                           for f in r.hook_files]}


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
                 trailer_key: str = "Hive-Trace", autonomy=None) -> None:
        self.admission = admission          # AdmissionService: write + capture
        self.recall = recall                # RecallPipeline: recall(query,*,agent_id,agent_ctx)
        self.store = store                  # EpisodeStore: get_episode (belt) / fetch / counts
        self.embedder = embedder            # EmbeddingProvider: health probes (d, w_version, name)
        self.install_planner = install_planner  # InstallPlanner: hive_init plan/confirm
        self.identity = identity
        self.now = now
        self.started_ts = int(started_ts)
        self.db_path = db_path
        self.trailer_key = trailer_key
        # the lifecycle knob group (duck-typed AutonomyConfig); None ⇒ defaults.
        # The recall belt's per-hit is_servable re-check reads provisional_ttl off
        # it; the gap report reads demand_window/demand_tau.
        if autonomy is None:
            from hive.app.config import AutonomyConfig   # noqa: PLC0415 — lazy default
            autonomy = AutonomyConfig()
        self.autonomy = autonomy
        self._tool_handlers: dict[str, Callable[[dict, ServerIdentity], dict]] = {
            "hive_write": self._handle_write,
            "hive_capture": self._handle_capture,
            "hive_recall": self._handle_recall,
            "hive_fetch": self._handle_fetch,
            "hive_init": self._handle_init,
            "hive_health": self._handle_health,
        }

    # ── protocol dispatch (PORT) ──────────────────────────────────────────────
    def handle(self, req: MCPRequest, *, identity: Optional[ServerIdentity] = None) -> MCPResponse:
        # Per-request identity (the HTTP daemon's verified caller); ``None`` ⇒ the process
        # ``ServerIdentity`` (the stdio path + every existing test pass ``req`` only, so they
        # are byte-for-byte unchanged). The transport resolves WHO calls; attribution stays
        # in the handlers (§9-D1).
        ident = identity or self.identity
        if req.method == "initialize":
            return MCPResponse(id=req.id, result={
                "protocolVersion": MCP_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION}})
        if req.method == "tools/list":
            return MCPResponse(id=req.id, result={"tools": TOOL_DEFINITIONS})
        if req.method == "tools/call":
            return self._tools_call(req, ident)
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

    def _tools_call(self, req: MCPRequest, identity: ServerIdentity) -> MCPResponse:
        name = req.params.get("name")
        args = req.params.get("arguments", {}) or {}
        handler = self._tool_handlers.get(name)
        if handler is None:                                  # unknown tool → JSON-RPC error
            _log.warning("mcp.unknown_tool", extra={"event": "mcp.unknown_tool",
                         "tool": name, "agent_id": identity.agent_id})
            return MCPResponse(id=req.id, error=_err(-32602, f"unknown tool: {name}"))
        # ── BELT 1: schema validation BEFORE any port is touched (RULE-2 mut #1) ──
        verr = _validate_args(name, args)
        if verr is not None:
            _log.info("mcp.schema_reject", extra={"event": "mcp.schema_reject",
                      "tool": name, "reason": verr, "agent_id": identity.agent_id})
            return self._tool_error(req.id, f"invalid arguments: {verr}")
        try:
            content = handler(args, identity)        # per-request identity → the handler
            self._note_hook_seen(name)               # V4: best-effort, never fails the call
            return self._tool_result(req.id, content, is_error=False)
        except SecretRefused:
            # only hive_write raises this and it returns its own refused envelope;
            # reaching here means a defensive double-raise — treat as a tool error.
            raise
        except Exception as e:                               # loop survives; stack → log, NOT agent
            _log.error("mcp.tool_raised", extra={"event": "mcp.tool_raised",
                       "tool": name, "agent_id": identity.agent_id,
                       "error_type": type(e).__name__}, exc_info=True)
            return self._tool_error(req.id, f"error: {type(e).__name__}: {e}")

    # ── tool handlers: (args, identity); args read permissively, _validate_args is the only
    #    required-field guard. ``identity`` is the per-request caller resolved by the transport
    #    (HTTP daemon) or the process default (stdio) — attribution lives HERE (§9-D1). ──
    def _handle_write(self, args: dict, identity: ServerIdentity) -> dict:
        text = args.get("text")
        approved_by = args.get("approved_by") or ""   # required by the schema belt
        source = args.get("source") or ""
        tags = args.get("tags") or []
        tags_str = ",".join(str(t) for t in tags) if isinstance(tags, list) else str(tags)
        # proposed_by is ALWAYS the authenticated caller — INV-2: no client field to assert it
        # (``proposed_by`` is gone from the write schema). RULE-2 mut: read self.identity here.
        proposed_by = identity.agent_id
        replaces = args.get("replaces")           # human-vouched supersession target
        try:
            res = self.admission.write(text, approved_by=approved_by,
                                       proposed_by=proposed_by, source=source,
                                       tags=tags_str, replaces=replaces)
        except SecretRefused as e:
            # REFUSE: nothing written (0 rows). Envelope carries rule NAMES, never bytes.
            return {"status": "refused", "reason": str(e),
                    "scan": {"action": "refuse", "rules": e.rules,
                             "n_findings": e.n_findings}}
        # CLEAN/REDACT both land APPROVED + recallable in one call (client-gated).
        out: dict = {"status": res.status, "id": res.episode_id,
                     "content_hash": res.content_hash, "scan": _scan_report(res.scan),
                     "approved_by": approved_by}
        if res.superseded is not None:           # the vouched correction retired its target
            out["superseded"] = res.superseded
        if res.status == "redacted" and res.scan.redacted_text is not None:
            out["redacted_preview"] = _preview(res.scan.redacted_text)
        return out

    def _handle_capture(self, args: dict, identity: ServerIdentity) -> dict:
        text = args.get("text")
        source = args.get("source") or ""
        tags = args.get("tags") or []
        tags_str = ",".join(str(t) for t in tags) if isinstance(tags, list) else str(tags)
        try:
            res = self.admission.capture(text, proposed_by=identity.agent_id,
                                         source=source, tags=tags_str)
        except SecretRefused as e:
            # REFUSE: nothing written (0 rows). Envelope carries rule NAMES, never bytes.
            return {"status": "refused", "reason": str(e),
                    "scan": {"action": "refuse", "rules": e.rules,
                             "n_findings": e.n_findings}}
        if res.status == "disabled":             # autonomy off — nothing written
            return {"status": "disabled"}
        return {"status": res.status, "id": res.episode_id,
                "content_hash": res.content_hash, "scan": _scan_report(res.scan),
                "deduped": res.deduped}

    def _handle_recall(self, args: dict, identity: ServerIdentity) -> dict:
        query = args.get("query") or ""
        ctx = AgentContext(repo_remote=args.get("repo_remote") or "",
                           language=args.get("language") or "",
                           workflow=args.get("workflow") or "general")
        result = self.recall.recall(query, agent_id=identity.agent_id, agent_ctx=ctx)
        # ── BELT 2: servable-only re-filter, independent of the index (RULE-2 mut #2).
        # The per-hit is_servable re-check is the AUTHORITATIVE freshness layer: a
        # TTL-lapsed provisional row still sitting in the warm index is dropped HERE
        # even before the sweep materializes its death.
        now = int(self.now())
        belt_ttl_s = int(self.autonomy.provisional_ttl_days) * _DAY_S
        hits: list[dict] = []
        for h in result.hits:
            ep = self.store.get_episode(h.episode_id)
            if ep is None or not is_servable(                # ← delete this guard ⇒ mut #2
                    status=ep.status, trust=ep.trust,
                    last_active_ts=ep.last_active_ts, now=now,
                    provisional_ttl_s=belt_ttl_s):
                _log.warning("mcp.recall_belt_drop", extra={"event": "mcp.recall_belt_drop",
                             "trace_id": result.trace_id, "episode_id": h.episode_id})
                continue
            hits.append({"episode_id": h.episode_id, "text": h.text,
                         "sim": float(h.sim), "content_hash": ep.content_hash,
                         "trust": h.trust, "ts": h.ts})
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

    def _handle_fetch(self, args: dict, identity: ServerIdentity) -> dict:
        h = args.get("content_hash") or ""
        text = self.store.fetch(h)                           # clean miss → None, never raises
        out: dict = {"found": text is not None, "text": text}
        if text is not None:
            # supersession annotation: a fetched-but-superseded row names its
            # TERMINAL successor as {episode_id, content_hash} (hash because fetch
            # is hash-keyed — a bare id would be unfollowable). Best-effort: an
            # annotation fault never breaks the fetch.
            try:
                eid = self.store.episode_id_by_hash(h)
                succ = self.store.terminal_successor(eid) if eid is not None else None
                if succ is not None:
                    out["superseded_by"] = {"episode_id": succ[0],
                                            "content_hash": succ[1]}
            except Exception:                                # noqa: BLE001 — annotation only
                _log.warning("mcp.fetch_successor_probe_failed", extra={
                    "event": "mcp.fetch_successor_probe_failed"}, exc_info=True)
        return out

    def _handle_init(self, args: dict, identity: ServerIdentity) -> dict:
        repo_path = args.get("repo_path") or ""
        harness = args.get("harness") or "generic"
        rules_file = args.get("rules_file")
        confirm_hash = args.get("confirm_hash")
        if confirm_hash:                                     # phase 2: hash-verified link
            res = self.install_planner.confirm(repo_path, bytes.fromhex(confirm_hash), harness)
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
                "manifest": _manifest_report(plan.manifest),
                "recipe": _recipe_report(plan.recipe),
                # §5.3.2: the completion marker the agent stamps LAST (after the verify
                # gate), OUTSIDE the hashed block — surfaced here so phase-1 hands over the
                # whole bundle in one round-trip. NOT written by the server (no repo FS).
                "provenance_banner": provenance_banner(plan.harness, plan.recipe.resolved_tier)}

    def _handle_health(self, args: dict, identity: ServerIdentity) -> dict:
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
            snap["hooks_seen"] = self._hooks_seen()          # §7 V4: last-seen tick per capture verb
            # trust-lifecycle telemetry: quarantine pile-up must be visible, never
            # silent. Best-effort — an older store double (tests) may lack these.
            try:
                snap["trust_counts"] = self.store.trust_counts()
                snap["n_misses_7d"] = self.store.miss_count_since(
                    int(self.now()) - 7 * _DAY_S)
            except Exception:                                # noqa: BLE001 — telemetry only
                _log.warning("mcp.health_trust_probe_failed", extra={
                    "event": "mcp.health_trust_probe_failed"}, exc_info=True)
            hint = self._solo_hint()                         # §3.5: stalls self-describe
            if hint is not None:
                snap["solo_hint"] = hint
            if args.get("include_trends"):
                snap["trends"] = self._trends_report()       # CV4: the convergence KPI
            if args.get("include_gaps"):
                snap["gaps"] = self._gap_report()
                contested = self._contested_report()         # CV3: the review queue
                if contested:
                    snap["contested"] = contested
                    snap["contested_note"] = (
                        "repeated abstains near a servable row = near-dups or a "
                        "contradiction inside the store; repeated re-asks = the "
                        "served content isn't satisfying — review and resolve "
                        "with one hive_write(replaces=<episode_id>)")
            if repo_path is not None:
                linked, link = self._link_status(repo_path)
                snap["linked"] = linked
                snap["link"] = link
                if not linked:                       # §5.2 first-touch: hand the agent its next step
                    snap["onboarding"] = onboarding_hint()
                else:                                # §5.3.3 re-touch: the bootstrap short-circuit
                    snap["setup"] = {"complete": True, "next": None}
                    # an old-manifest link drives re-init (the v2 hooks change the
                    # capture contract from ask-first to capture-without-asking)
                    snap["manifest_outdated"] = bool(
                        int((link or {}).get("manifest_version", 1))
                        < ONBOARDING_MANIFEST_VERSION)
            return snap
        except Exception as e:                               # fail-closed subset ONLY
            _log.error("mcp.health_probe_fail", extra={"event": "mcp.health_probe_fail",
                       "error_type": type(e).__name__, "db_path": self.db_path},
                       exc_info=True)
            return {"ok": False, "error": f"{type(e).__name__}: {e}",
                    "db_path": self.db_path}

    # ── health helpers ────────────────────────────────────────────────────────
    def _solo_hint(self) -> Optional[str]:
        """§3.5: when single-seat traffic is WASTING demand (≥ demand_m window
        misses, all from ≤1 identity) under the anti-gaming rule, the autonomy
        loop is silently inert — return the self-describing hint. None when
        autonomy is off, solo_mode is already on, the store is empty/quiet, or
        the probe faults (telemetry only, never breaks health)."""
        try:
            if not bool(getattr(self.autonomy, "enabled", True)):
                return None
            if bool(getattr(self.autonomy, "solo_mode", False)):
                return None
            window_s = int(self.autonomy.demand_window_days) * _DAY_S
            misses = self.store.misses_window(int(self.now()) - window_s)
            if len(misses) < int(self.autonomy.demand_m):    # ← the wasted-demand floor
                return None
            if len({m.agent_id for m in misses}) > 1:
                return None
            return ("single-seat traffic: demand-promotion is inert under the "
                    "anti-gaming rule — set HIVE_AUTONOMY__SOLO_MODE=true or "
                    "provision per-seat identities (one token per seat: "
                    "hive token <seat>)")
        except Exception:                                    # noqa: BLE001 — telemetry only
            _log.warning("mcp.solo_hint_probe_failed", extra={
                "event": "mcp.solo_hint_probe_failed"}, exc_info=True)
            return None

    def _gap_report(self) -> list[dict]:
        """The clustered demand-gap report (deterministic, capped window, top-10).
        Window + cosine neighborhood mirror what the promotion rule sees, so a
        reported gap is exactly un-served demand. Degrades to [] on a probe fault."""
        try:
            window_s = int(self.autonomy.demand_window_days) * _DAY_S
            rows = self.store.misses_detail_window(int(self.now()) - window_s)
            return cluster_misses(rows, tau=float(self.autonomy.demand_tau))
        except Exception:                                    # noqa: BLE001 — telemetry only
            _log.warning("mcp.gap_report_failed", extra={
                "event": "mcp.gap_report_failed"}, exc_info=True)
            return []

    def _trends_report(self) -> dict:
        """CV4: current-vs-previous 14d windows over existing tables — the
        convergence KPI (confident_rate ↑, demand_entropy ↓, dead_capture_ratio
        bounded). Composes the gaps clustering; degrades to {} on a fault."""
        try:
            tau = float(self.autonomy.demand_tau)
            return compute_trends(
                self.store, lambda rows: cluster_misses(rows, tau=tau),
                now=int(self.now()))
        except Exception:                                    # noqa: BLE001 — telemetry only
            _log.warning("mcp.trends_report_failed", extra={
                "event": "mcp.trends_report_failed"}, exc_info=True)
            return {}

    def _contested_report(self) -> list[dict]:
        """CV3: servable rows the window's misses cluster against (cosine ≥
        contested_tau) — the mechanical supersession-review queue. Probes the
        live servable index once per CLUSTER. Degrades to [] on any fault."""
        try:
            window_s = int(self.autonomy.demand_window_days) * _DAY_S
            rows = self.store.misses_detail_window(int(self.now()) - window_s)
            return contested_misses(
                rows, tau=float(self.autonomy.demand_tau),
                contested_tau=float(getattr(self.autonomy, "contested_tau", 0.80)),
                search=self.recall.index.search,
                get_episode=self.store.get_episode)
        except Exception:                                    # noqa: BLE001 — telemetry only
            _log.warning("mcp.contested_report_failed", extra={
                "event": "mcp.contested_report_failed"}, exc_info=True)
            return []

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

    # ── V4 hook-seen substrate (§7) ────────────────────────────────────────────
    def _note_hook_seen(self, tool: str) -> None:
        """Stamp meta[hook:last_seen:<tool>] for a capture verb after it is served, so the
        agent can confirm a Tier-2 OS hook reached the server. Best-effort and fully self-
        contained: a stamp failure is logged and SWALLOWED — it must never convert a
        successful capture into a tool error. // O(1)."""
        if tool not in _HOOK_SEEN_TOOLS:
            return
        try:
            self.store.meta_set(f"{_HOOK_SEEN_PREFIX}{tool}", str(int(self.now())))
        except Exception:                                    # telemetry must never break a capture
            _log.warning("mcp.hook_seen_stamp_failed", extra={"event": "mcp.hook_seen_stamp_failed",
                         "tool": tool}, exc_info=True)

    def _hooks_seen(self) -> dict:
        """The read side: last-seen tick per capture verb (a verb absent ⇒ never seen). A
        pure read; a meta failure degrades to ``{}`` rather than failing health. // O(1)."""
        out: dict[str, int] = {}
        for tool in _HOOK_SEEN_TOOLS:
            try:
                raw = self.store.meta_get(f"{_HOOK_SEEN_PREFIX}{tool}")
            except Exception:
                continue
            if raw is None:
                continue
            try:
                out[tool] = int(raw)
            except (TypeError, ValueError):                  # a non-int blob ⇒ skip, never raise
                continue
        return out


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


# ── per-session process entrypoint (P0 — the Tier-0 connect transport) ─────────
# ``python -m hive.app.mcp_server`` is a PER-SESSION MCP server an IDE's ``.mcp.json`` execs
# INSIDE the already-warm container, e.g.:
#     docker compose -f compose.yaml exec -T hive-server \
#         python -m hive.app.mcp_server --db /data/shared.db --tenant "$HIVE_TENANT_ID"
# It reuses ``build_container``'s assembly against the shared ``/data/shared.db`` (migration is
# idempotent); then — because NO memory is shared with PID 1 — it builds its OWN in-RAM index
# from the store and warms its OWN embedder once (a per-session cost, then warm) before serving
# ``run_stdio`` on its own stdin/stdout. N IDE windows ⇒ N light processes sharing the one WAL.
#
# It deliberately does NOT replicate the container ENTRYPOINT's readiness-marker policy
# (``hive.tools.entrypoint._invalidate_ready`` / ``_mark_ready``): ``boot:serve_pid`` /
# ``boot:serve_starttime`` / ``boot:embedder_loaded`` identify PID 1 for the container
# HEALTHCHECK, and a per-session exec'd process (NOT PID 1, reusing the persistent volume)
# writing OR clearing them would corrupt the liveness identity the healthcheck reads. This
# entry is marker-INERT — it neither stamps nor invalidates them; liveness stays PID 1's alone.

# sysexits.h boot contract — MIRRORS ``hive.tools.entrypoint`` (an exit code cannot lie like a
# log line can; an orchestrator gates on it). Redefined locally rather than imported so the
# ``hive.app`` surface does not depend up the stack on the ``hive.tools`` boot adapter.
EX_OK = 0
EX_UNAVAILABLE = 69
EX_SOFTWARE = 70
EX_CONFIG = 78

_DEFAULT_DB_PATH = "/data/shared.db"
_DEFAULT_AGENT_ID = "default-agent"


def _configure_logging(level: int = logging.INFO) -> None:
    """Bind the structured-JSON-to-stderr handler onto the ``hive`` logger (stdout is the
    JSON-RPC channel). Idempotent; a logging-setup failure NEVER aborts the boot. // O(1)."""
    try:
        from hive.app.observability import configure_json_logging  # noqa: PLC0415 — lazy
        configure_json_logging(level=int(level), stream=True)
    except Exception as exc:                       # noqa: BLE001 — logging setup never aborts boot
        _log.warning("mcp_server.log_config_failed kind=%s", type(exc).__name__)


def _resolve_identity(tenant: Optional[str], db: Optional[str], agent: Optional[str],
                      env: Mapping[str, str]) -> Optional[tuple[str, str, str]]:
    """Resolve ``(tenant_id, db_path, agent_id)`` — CLI arg takes precedence, then env, then a
    safe default for db/agent. ``tenant_id`` is the one HARD-required value (the single-tenant
    boundary, mirroring ``entrypoint._resolve_env``); returns None iff it is absent in BOTH the
    arg and env (the caller maps that to EX_CONFIG). NEVER echoes an env *value* (secret-safe).
    // O(1)."""
    tenant_id = (tenant or env.get("HIVE_TENANT_ID") or "").strip()
    if not tenant_id:
        _log.error("mcp_server.missing_required arg/var=tenant (pass --tenant or HIVE_TENANT_ID) "
                   "code=%d", EX_CONFIG)
        return None
    db_path = (db or env.get("HIVE_STORE__DB_PATH") or "").strip() or _DEFAULT_DB_PATH
    agent_id = (agent or env.get("HIVE_AGENT_ID") or "").strip() or _DEFAULT_AGENT_ID
    return tenant_id, db_path, agent_id


def main(argv: Optional[list[str]] = None, *, env: Optional[Mapping[str, str]] = None,
         build_container_fn: Optional[Callable[..., Any]] = None,
         serve: Optional[Callable[[Any], None]] = None) -> int:
    """Boot a PER-SESSION ``HiveMCPServer`` and serve it on stdio; return a sysexits exit code.

    Boot order MIRRORS the container entrypoint MINUS the readiness markers:
        config → build_container → migrate → build_index → warm_embedder → make_server → serve

    ``build_container_fn`` (default ``hive.app.container.build_container``, imported LAZILY to
    dodge the ``container → mcp_server`` import cycle) and ``serve`` (default ``run_stdio`` on
    the real std streams) are injection seams so the order, the fail-fast exit codes, and the
    marker-INERT policy are unit-testable with no torch and no real stdio. Never raises out of
    the boot path — every failure is logged and converted to an exit code so stdout (the
    JSON-RPC channel) stays clean. // O(1) control flow."""
    import argparse                              # stdlib; lazy keeps the module-import surface light

    env = os.environ if env is None else env
    parser = argparse.ArgumentParser(
        prog="hive.app.mcp_server",
        description="Per-session Hive MCP server (stdio), exec'd inside the warm container.")
    parser.add_argument("--db", default=None,
                        help="shared SQLite store path (default: $HIVE_STORE__DB_PATH or /data/shared.db)")
    parser.add_argument("--tenant", default=None,
                        help="tenant id — REQUIRED (default: $HIVE_TENANT_ID)")
    parser.add_argument("--agent", default=None,
                        help="agent id, stamped as proposed_by (default: $HIVE_AGENT_ID or default-agent)")
    args = parser.parse_args(argv)

    _configure_logging()                          # JSON→stderr before anything can fail

    # ── config.loaded (EX_CONFIG on missing-tenant OR validation failure) ──
    resolved = _resolve_identity(args.tenant, args.db, args.agent, env)
    if resolved is None:                          # ← removing this guard is the missing-tenant mutation
        return EX_CONFIG
    tenant_id, db_path, agent_id = resolved

    # Lazy imports: ``container`` imports THIS module (HiveMCPServer/ServerIdentity), so a
    # module-level ``from hive.app.container import build_container`` would be a circular import.
    try:
        from hive.app.config import Config        # noqa: PLC0415 — lazy
    except Exception as exc:                       # noqa: BLE001
        _log.error("mcp_server.config_import_failed kind=%s code=%d", type(exc).__name__, EX_SOFTWARE)
        return EX_SOFTWARE
    try:
        cfg = Config.load(db_path=db_path, env=env, runtime={"tenant_id": tenant_id})
    except Exception as exc:                       # noqa: BLE001 — bad config is EX_CONFIG
        _log.error("mcp_server.config_invalid kind=%s code=%d", type(exc).__name__, EX_CONFIG)
        return EX_CONFIG
    _configure_logging(int(getattr(cfg.obs, "log_level", logging.INFO)))  # operator level now live
    _log.info("mcp_server.config_loaded tenant_id=%s db_path=%s", tenant_id, db_path)

    if build_container_fn is None:
        try:
            from hive.app.container import build_container  # noqa: PLC0415 — lazy (breaks the cycle)
        except Exception as exc:                   # noqa: BLE001
            _log.error("mcp_server.container_import_failed kind=%s code=%d",
                       type(exc).__name__, EX_SOFTWARE)
            return EX_SOFTWARE
        build_container_fn = build_container
    try:
        container = build_container_fn(cfg, tenant_id=tenant_id, agent_id=agent_id)
    except Exception as exc:                       # noqa: BLE001 — assembler failure
        _log.error("mcp_server.assemble_failed kind=%s code=%d", type(exc).__name__, EX_SOFTWARE)
        return EX_SOFTWARE

    # ── boot the per-session server. NO _invalidate_ready / _mark_ready around this sequence —
    #    see the banner above. The boot readiness markers belong to PID 1 (the ENTRYPOINT). ──
    try:
        container.migrate()                        # idempotent verify vs the already-migrated shared DB
    except Exception as exc:                       # noqa: BLE001
        _log.error("mcp_server.migrate_failed kind=%s code=%d", type(exc).__name__, EX_SOFTWARE)
        return EX_SOFTWARE
    try:
        container.build_index()                    # THIS process's own in-RAM index, from approved rows
    except Exception as exc:                       # noqa: BLE001
        _log.error("mcp_server.index_failed kind=%s code=%d", type(exc).__name__, EX_SOFTWARE)
        return EX_SOFTWARE
    try:
        embedder = container.warm_embedder()       # THIS process's own embedder, warmed once
    except Exception as exc:                       # noqa: BLE001 — dead embedder
        _log.error("mcp_server.embedder_warm_failed kind=%s code=%d",
                   type(exc).__name__, EX_UNAVAILABLE)
        return EX_UNAVAILABLE
    if not bool(getattr(embedder, "loaded", False)):   # healthy ≡ resident — a cold embedder cannot serve
        _log.error("mcp_server.embedder_not_resident code=%d", EX_UNAVAILABLE)
        return EX_UNAVAILABLE
    try:
        server = container.make_server()
    except Exception as exc:                       # noqa: BLE001
        _log.error("mcp_server.make_server_failed kind=%s code=%d", type(exc).__name__, EX_SOFTWARE)
        return EX_SOFTWARE

    _log.info("mcp_server.serving tenant_id=%s db_path=%s agent_id=%s (per-session; markers untouched)",
              tenant_id, db_path, agent_id)
    if serve is None:
        def serve(s: Any) -> None:                 # default seam: blocking real-stdio serve
            run_stdio(s, sys.stdin, sys.stdout)
    serve(server)
    return EX_OK


if __name__ == "__main__":  # pragma: no cover — module entry (``python -m hive.app.mcp_server``)
    raise SystemExit(main(sys.argv[1:]))
