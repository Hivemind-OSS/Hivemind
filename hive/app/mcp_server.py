"""HiveMCPServer — the single MCP/JSON-RPC trust boundary (M06 / C7).

A thin driving adapter (hexagonal): it maps four ``hive_*`` JSON-RPC verbs onto the
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
recall envelope carries ``trace_id`` on hit AND abstain (the move-#6 join key).
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

from hive.app.gaps import cluster_misses
from hive.app.onboard_ref import CONTRACT_VERSION, SERVER_INSTRUCTIONS
from hive.app.trends import compute_trends
from hive.app.tool_defs import TOOL_DEFINITIONS
from hive.domain.agi import is_agi_override
from hive.domain.conflict import (
    CONTRADICTION, ConflictItem, detect_conflicts,
)
from hive.domain.consensus import (
    SuspectConsensusItem, detect_suspect_consensus,
)
from hive.domain.errors import SecretRefused
from hive.domain.evidence_kinds import EK_OUTCOME_HELPED, EK_OUTCOME_HURT
from hive.domain.kinds import DEFAULT_KIND
from hive.domain.lifecycle import is_servable
from hive.domain.models import CONFIDENT

_log = logging.getLogger("hive.app.mcp_server")

_DAY_S = 86_400

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


# ── pre-dispatch schema validation belt (runs before any port) ─────────

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
    (permissive). // O(fields) time. The deleting of the CALL to this is
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


def _conflict_note_to_wire(note) -> dict:
    """A mechanical ``ConflictNote`` → the ids-only wire dict. Carries NO memory text (Law 4):
    only ids, the relation, the cosine, the shared anchor, and a directional retire HINT with
    the human resolution call spelled out — never an applied action (THEORY §10 O7)."""
    d: dict = {"a_id": note.a_id, "b_id": note.b_id, "relation": note.relation,
               "cosine": round(float(note.cosine), 4), "anchor": note.anchor,
               "loser_hint": note.loser_hint, "source": "mechanical"}
    if note.relation == CONTRADICTION:
        d["note"] = ("opposing polarity (do vs dont) on near-duplicate memories — they "
                     "disagree; a human resolves which holds via hive_supersede")
    else:
        winner = note.b_id if note.loser_hint == note.a_id else note.a_id
        d["note"] = (f"near-duplicate memories; {note.loser_hint} is the lower-trust/older "
                     f"retire candidate (hive_supersede loser={note.loser_hint} "
                     f"winner={winner})")
    return d


def _flag_to_wire(flag: dict) -> dict:
    """An open advisory ``conflict_flags`` row → the worklist wire dict. ``kind`` is the
    advisory vocabulary (conflict|supersedes); ``resolution`` is the flagger's secret-scanned
    rationale. ids only — no memory text rides it."""
    return {"a_id": flag["a_id"], "b_id": flag["b_id"], "kind": flag["kind"],
            "winner_id": flag["winner_id"], "resolution": flag["resolution"],
            "proposed_by": flag["proposed_by"], "source": "advisory",
            "note": "agent-flagged advisory; a human resolves it via hive_supersede"}


class HiveMCPServer:
    """JSON-RPC 2.0 over stdio. One process == one ``ServerIdentity``."""

    def __init__(self, *, admission, recall, store, embedder,
                 identity: ServerIdentity, now: Callable[[], int],
                 started_ts: int = 0, db_path: str = "", autonomy=None,
                 conflict=None, flag_service=None, suspect_consensus=None,
                 agi_mode: bool = False, secret_scan_enabled: bool = True) -> None:
        self.admission = admission          # AdmissionService: write + capture
        self.recall = recall                # RecallPipeline: recall(query, *, agent_id)
        self.store = store                  # the store adapter: get_episode (belt) / counts
        self.embedder = embedder            # EmbeddingProvider: health probes (d, name)
        self.identity = identity
        self.now = now
        self.started_ts = int(started_ts)
        self.db_path = db_path
        # the lifecycle knob group (duck-typed AutonomyConfig); None ⇒ defaults.
        # The recall belt's per-hit is_servable re-check reads provisional_ttl off
        # it; the gap report reads demand_window/demand_tau.
        if autonomy is None:
            from hive.app.config import AutonomyConfig   # noqa: PLC0415 — lazy default
            autonomy = AutonomyConfig()
        self.autonomy = autonomy
        # conflict surfacing (duck-typed ConflictConfig). The PRODUCT default is ON: the real
        # ConflictConfig the container wires from config enables detection (the ``conflicts``
        # carrier + the health worklist + the advisory hive_flag). But a server built with NO
        # conflict config has no ``flag_service`` to back the advisory verb, so the bare-
        # construction fallback stays byte-inert (enabled=False ⇒ no key, no worklist) rather than
        # surfacing a carrier the flag verb cannot service. ``flag_service`` (ConflictFlagService
        # or None) backs hive_flag — gated by conflict.enabled.
        if conflict is None:
            from hive.app.config import ConflictConfig   # noqa: PLC0415 — lazy default
            conflict = ConflictConfig(enabled=False)
        self.conflict = conflict
        self.flag_service = flag_service
        # suspect-consensus worklist (duck-typed SuspectConsensusConfig). The worklist is gated
        # SOLELY by the include_suspect_consensus request flag (no config enabled toggle), so a
        # bare-construction server's health envelope is byte-identical until the flag is set — the
        # same single-gate posture as the conflict worklist. Detection-only — it reads stamped
        # promote audits, never retires (Law 3).
        if suspect_consensus is None:
            from hive.app.config import SuspectConsensusConfig   # noqa: PLC0415 — lazy default
            suspect_consensus = SuspectConsensusConfig()
        self.suspect_consensus = suspect_consensus
        # AGI_MODE (default OFF): the transport flag that HONORS the reserved AGI_OVERRIDE
        # sentinel. It lives ONLY here at the boundary — the pure domain reacts to the sentinel
        # VALUE, never to this flag (Law 4). OFF ⇒ the sentinel is rejected fail-closed (Law 6)
        # and every existing envelope is byte-identical (THEORY §9.9). It LOOSENS a gate, so it
        # is the one default-OFF that stays strict regardless of product posture.
        self.agi_mode = bool(agi_mode)
        # The credential secret-floor posture (HIVE_SECRET_SCAN__ENABLED, default ON). The actual
        # scan/bypass is owned by the injected scanner adapter; the boundary holds this ONLY to
        # surface the loosened posture in hive_health (a disabled floor must never be silent — it
        # is also WARN-logged at boot). Default True ⇒ no health key ⇒ byte-identical envelope.
        self.secret_scan_enabled = bool(secret_scan_enabled)
        self._tool_handlers: dict[str, Callable[[dict, ServerIdentity], dict]] = {
            "hive_write": self._handle_write,
            "hive_capture": self._handle_capture,
            "hive_recall": self._handle_recall,
            "hive_supersede": self._handle_supersede,
            "hive_prune": self._handle_prune,
            "hive_outcome": self._handle_outcome,
            "hive_flag": self._handle_flag,
            "hive_health": self._handle_health,
        }

    # ── protocol dispatch (PORT) ──────────────────────────────────────────────
    def handle(self, req: MCPRequest, *, identity: Optional[ServerIdentity] = None) -> MCPResponse:
        # Per-request identity (the HTTP daemon's verified caller); ``None`` ⇒ the process
        # ``ServerIdentity`` (the stdio path + every existing test pass ``req`` only, so they
        # are byte-for-byte unchanged). The transport resolves WHO calls; attribution stays
        # in the handlers (D1).
        ident = identity or self.identity
        if req.method == "initialize":
            return MCPResponse(id=req.id, result={
                "protocolVersion": MCP_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                # the spec-canonical usage contract every client surfaces at connect (M2 channel)
                "instructions": SERVER_INSTRUCTIONS})
        if req.method == "tools/list":
            return MCPResponse(id=req.id, result={"tools": TOOL_DEFINITIONS})
        if req.method == "tools/call":
            return self._tools_call(req, ident)
        if req.method == "ping":
            return MCPResponse(id=req.id, result={})
        return MCPResponse(id=req.id, error=_err(-32601, f"method not found: {req.method}"))

    def _tool_result(self, req_id: Any, content: dict, *, is_error: bool) -> MCPResponse:
        # BEACON (mutation #3): stamp the live contract version on EVERY dict envelope through this
        # single choke point, so a connected agent detects a server-side contract upgrade on the only
        # channel live after connect (tool results) and re-onboards by comparing it to the version in
        # its installed HIVEMIND-RULES marker. It is an UNCONDITIONAL version stamp, not optional
        # behavior with an off-knob (THEORY §9 #14 "a measured-good behavior with one right answer is
        # NOT a knob"). The compare-and-reinstall procedure lives in the installed block + the
        # initialize instructions (single-sourced), not repeated on every result. The bare-string
        # _tool_error path is deliberately UNbeaconed (the single-owner boundary —
        # test_tool_error_path_is_unbeaconed pins it). Deleting this line is mutation #3.
        content = {**content, "contract_version": CONTRACT_VERSION}
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
        # ── BELT 1: schema validation BEFORE any port is touched (mut #1) ──
        verr = _validate_args(name, args)
        if verr is not None:
            _log.info("mcp.schema_reject", extra={"event": "mcp.schema_reject",
                      "tool": name, "reason": verr, "agent_id": identity.agent_id})
            return self._tool_error(req.id, f"invalid arguments: {verr}")
        try:
            content = handler(args, identity)        # per-request identity → the handler
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

    # ── AGI_MODE boundary gate (the ONE place the flag is read; the domain never sees it) ──
    def _override_refused(self, approved_by: str) -> bool:
        """True iff the caller named the reserved AGI_OVERRIDE sentinel but AGI_MODE is OFF —
        the fail-closed refusal (Law 6). With the flag off the override is UNCONSTRUCTABLE, so
        honesty stays structural (Law 1). A human-named vouch is never refused here; with
        AGI_MODE ON the sentinel passes through to the handler. Deleting this guard is the
        AGI-gate mutation — the OFF-refused tests red."""
        return is_agi_override(approved_by) and not self.agi_mode

    def _agi_refused(self, verb: str) -> dict:
        """The fail-closed refusal envelope for an AGI_OVERRIDE call under AGI_MODE OFF: nothing
        written/retired (0 rows), no ``id``, a self-describing reason. Mirrors the secret-refuse
        envelope shape (``status='refused'``) so the bench/client parse path is unchanged."""
        _log.info("mcp.agi_override_refused", extra={
            "event": "mcp.agi_override_refused", "verb": verb})
        return {"status": "refused", "reason": (
            "AGI_OVERRIDE rejected: AGI_MODE is off (the override is honored only when the "
            "operator sets HIVE_AGI__MODE=true). Nothing was written or retired."),
            "agi_mode": False}

    # ── tool handlers: (args, identity); args read permissively, _validate_args is the only
    #    required-field guard. ``identity`` is the per-request caller resolved by the transport
    #    (HTTP daemon) or the process default (stdio) — attribution lives HERE (D1). ──
    def _handle_write(self, args: dict, identity: ServerIdentity) -> dict:
        text = args.get("text")
        approved_by = args.get("approved_by") or ""   # required by the schema belt
        if self._override_refused(approved_by):       # AGI_OVERRIDE sentinel + AGI_MODE off ⇒ refuse
            return self._agi_refused("hive_write")
        # proposed_by is ALWAYS the authenticated caller — INV-2: no client field to assert it
        # (``proposed_by`` is gone from the write schema). Deliberate mutation: read self.identity here.
        proposed_by = identity.agent_id
        replaces = args.get("replaces")           # human-vouched supersession target
        polarity = args.get("polarity", "neutral")  # do|dont|neutral (schema-enum-checked)
        kind = args.get("kind", DEFAULT_KIND)     # registry vocabulary (schema-enum-checked)
        anchor = args.get("anchor", "")           # the WHERE — file/module/symbol (free text)
        try:
            res = self.admission.write(text, approved_by=approved_by,
                                       proposed_by=proposed_by, replaces=replaces,
                                       polarity=polarity, kind=kind, anchor=anchor)
        except SecretRefused as e:
            # REFUSE: nothing written (0 rows). Envelope carries rule NAMES, never bytes.
            return {"status": "refused", "reason": str(e),
                    "scan": {"action": "refuse", "rules": e.rules,
                             "n_findings": e.n_findings}}
        # CLEAN/REDACT both land APPROVED + recallable in one call (client-gated).
        out: dict = {"status": res.status, "id": res.episode_id,
                     "scan": _scan_report(res.scan), "approved_by": approved_by}
        if res.superseded is not None:           # the vouched correction retired its target
            out["superseded"] = res.superseded
        if res.status == "redacted" and res.scan.redacted_text is not None:
            out["redacted_preview"] = _preview(res.scan.redacted_text)
        return out

    def _handle_capture(self, args: dict, identity: ServerIdentity) -> dict:
        text = args.get("text")
        polarity = args.get("polarity", "neutral")  # do|dont|neutral (schema-enum-checked)
        kind = args.get("kind", DEFAULT_KIND)     # registry vocabulary (schema-enum-checked)
        anchor = args.get("anchor", "")           # the WHERE — file/module/symbol (free text)
        try:
            res = self.admission.capture(text, proposed_by=identity.agent_id,
                                         polarity=polarity, kind=kind, anchor=anchor)
        except SecretRefused as e:
            # REFUSE: nothing written (0 rows). Envelope carries rule NAMES, never bytes.
            return {"status": "refused", "reason": str(e),
                    "scan": {"action": "refuse", "rules": e.rules,
                             "n_findings": e.n_findings}}
        if res.status == "disabled":             # autonomy off — nothing written
            return {"status": "disabled"}
        return {"status": res.status, "id": res.episode_id,
                "scan": _scan_report(res.scan), "deduped": res.deduped}

    def _handle_recall(self, args: dict, identity: ServerIdentity) -> dict:
        query = args.get("query") or ""
        result = self.recall.recall(query, agent_id=identity.agent_id)
        # ── BELT 2: servable-only re-filter, independent of the index (mut #2).
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
                         "sim": float(h.sim), "trust": h.trust, "ts": h.ts,
                         "polarity": h.polarity, "kind": h.kind, "anchor": h.anchor})
        # an empty post-belt set is an ABSTAIN, never a confident-empty (never-hallucinate)
        abstained = (result.state != CONFIDENT) or (not hits)
        env: dict = {"reference_context": hits, "abstained": abstained,
                     "trace_id": result.trace_id, "state": result.state,
                     "top_cos": float(result.top_cos)}
        # conflict carrier: a DISTINCT envelope key referencing IDS ONLY (never text, never
        # reference_context — Law 4). The pure pipeline detects over the PRE-select servable
        # field, so a near-dup the decorrelated serve dropped is still surfaced. Byte-inert when
        # off (no key) AND when no near-dup pair is co-present (lean by default).
        if getattr(self.conflict, "enabled", False) and result.conflicts:
            env["conflicts"] = [_conflict_note_to_wire(n) for n in result.conflicts]
        if abstained:
            _log.info("mcp.recall_abstain", extra={"event": "mcp.recall_abstain",
                      "trace_id": result.trace_id, "state": result.state,
                      "top_cos": float(result.top_cos)})
            env["note"] = _abstain_note(result.state)
        return env

    def _handle_supersede(self, args: dict, identity: ServerIdentity) -> dict:
        """The human-vouched resolution verb (ALWAYS on — Law 3). Thin wrapper over the
        ONE retirement owner ``store.supersede``: retire the loser in favor of the winner,
        recording ``actor=approved_by`` as the human vouch. ``store.supersede`` owns the
        existence + self-supersede guards (False ⇒ a no-op envelope, nothing retired); the
        loser drops out of serving on success."""
        loser = int(args.get("loser"))               # required+integer (schema belt)
        winner = int(args.get("winner"))
        approved_by = args.get("approved_by") or ""
        if self._override_refused(approved_by):       # AGI_OVERRIDE sentinel + AGI_MODE off ⇒ refuse
            return self._agi_refused("hive_supersede")
        ok = self.store.supersede(loser, winner, actor=approved_by, ts=int(self.now()))
        if ok:
            _log.info("mcp.superseded", extra={"event": "mcp.superseded",
                      "loser": loser, "winner": winner, "agent_id": identity.agent_id})
            return {"status": "superseded", "loser": loser, "winner": winner,
                    "approved_by": approved_by}
        return {"status": "noop", "loser": loser, "winner": winner,
                "reason": "unknown id or self-supersede — nothing retired"}

    def _handle_prune(self, args: dict, identity: ServerIdentity) -> dict:
        """The bare-retirement verb (the SECOND retirement owner beside hive_supersede). Thin
        wrapper over ``store.deprecate``: retire an INCORRECT/MALICIOUS/MISLEADING memory to
        ``deprecated`` with NO replacement, recording ``actor=approved_by``. Human-vouched by
        default; under AGI_MODE an agent self-authorizes (approved_by=AGI_OVERRIDE) — the SAME
        fail-closed gate as write/supersede refuses the sentinel when AGI_MODE is off (§10 O7:
        operator-gated, audit-stamped, reversible-tier — never mechanical auto-resolution).
        ``store.deprecate`` owns the existence + idempotency guards (False ⇒ a noop envelope,
        nothing retired)."""
        episode_id = int(args.get("episode_id"))     # required+integer (schema belt)
        approved_by = args.get("approved_by") or ""   # required by the schema belt
        if self._override_refused(approved_by):       # AGI_OVERRIDE sentinel + AGI_MODE off ⇒ refuse
            return self._agi_refused("hive_prune")
        ok = self.store.deprecate(episode_id, actor=approved_by, ts=int(self.now()))
        if ok:
            _log.info("mcp.pruned", extra={"event": "mcp.pruned",
                      "episode_id": episode_id, "agent_id": identity.agent_id})
            return {"status": "pruned", "episode_id": episode_id, "approved_by": approved_by}
        return {"status": "noop", "episode_id": episode_id,
                "reason": "unknown id or already retired — nothing pruned"}

    def _handle_outcome(self, args: dict, identity: ServerIdentity) -> dict:
        """The PURE evidence verb: an agent reports which recalled episode_ids materially HELPED
        or HURT its task. Records one ``outcome_helped`` / ``outcome_hurt`` evidence_events audit
        per KNOWN id (unknown ids are skipped — never a forged audit, never a raise), carrying
        ids + kinds ONLY, no memory text (Law 4). It holds NO establish/retire handle, so it
        structurally CANNOT mutate trust (O7-safe by construction, like ConflictFlagService): the
        evidence feeds the dormant martingale settled-win clause + a human worklist, never an
        action. NOT gated by AGI_MODE — identical in both modes. The reporter is the audit actor.
        Defensively coerces ids (a non-int item is skipped, fail-open side-channel)."""
        now = int(self.now())
        actor = identity.agent_id
        empty = json.dumps({})                        # no memory text rides the audit (Law 4)

        def _record(raw: Any, kind: str) -> list[int]:
            recorded: list[int] = []
            for x in raw if isinstance(raw, list) else []:
                try:
                    eid = int(x)
                except (TypeError, ValueError):
                    continue                          # skip a non-coercible id (fail-open)
                if self.store.get_episode(eid) is None:
                    continue                          # skip an unknown id (never forge an audit)
                self.store.insert_audit(eid, kind, actor, now, empty)
                recorded.append(eid)
            return recorded

        helped = _record(args.get("helped"), EK_OUTCOME_HELPED)
        hurt = _record(args.get("hurt"), EK_OUTCOME_HURT)
        _log.info("mcp.outcome", extra={"event": "mcp.outcome", "agent_id": actor,
                  "n_helped": len(helped), "n_hurt": len(hurt)})
        return {"status": "recorded", "helped": helped, "hurt": hurt}

    def _handle_flag(self, args: dict, identity: ServerIdentity) -> dict:
        """The advisory Layer-2 verb (gated by conflict.enabled). Records an UNTRUSTED
        conflict/supersedes marker via the ConflictFlagService — it NEVER retires (resolution
        stays the human hive_supersede). ``proposed_by`` is the transport identity (INV-2,
        never a tool field). A validation failure is a clean ``rejected`` envelope; a secret in
        the resolution is ``refused`` (0 rows)."""
        if self.flag_service is None:
            return {"status": "disabled"}
        a = int(args.get("a"))                       # required+integer (schema belt)
        b = int(args.get("b"))
        winner = args.get("winner")
        try:
            res = self.flag_service.flag(
                kind=args.get("kind"), a_id=a, b_id=b,
                winner_id=(int(winner) if winner is not None else None),
                resolution=args.get("resolution") or "",
                proposed_by=identity.agent_id)
        except SecretRefused as e:
            return {"status": "refused", "reason": str(e),
                    "scan": {"action": "refuse", "rules": e.rules,
                             "n_findings": e.n_findings}}
        except ValueError as e:
            return {"status": "rejected", "reason": str(e)}
        if res.status == "disabled":
            return {"status": "disabled"}
        return {"status": res.status, "kind": res.kind, "recorded": res.recorded,
                "a": a, "b": b}

    def _handle_health(self, args: dict, identity: ServerIdentity) -> dict:
        try:
            n_episodes, n_pending = self.store.counts()      # probe: store + (below) embedder
            snap: dict = {
                "ok": True, "tenant_id": self.identity.tenant_id, "db_path": self.db_path,
                "db_size_bytes": self._db_size(), "n_episodes": n_episodes,
                "n_pending": n_pending,
                "embedder": str(getattr(self.embedder, "name", "unknown")),
                "embedder_loaded": bool(getattr(self.embedder, "loaded", True)),
                "d": int(getattr(self.embedder, "d", 0)),
                "index_authoritative": bool(self.recall.index.is_authoritative()),
                "uptime_s": max(0, int(self.now()) - self.started_ts)}
            if not self.secret_scan_enabled:
                # the credential floor is OFF (operator opt-out) — surface the loosened posture so
                # it is never silent. Present ONLY when disabled ⇒ the default envelope is
                # byte-identical (dropping this guard ⇒ the always-emits mutation).
                snap["secret_scan_disabled"] = True
            if not self.db_path:
                # an empty db_path means the store is in-memory (:memory:) — all memory is LOST on
                # restart. Surface the loss-prone posture so it is never silent (mirrors
                # secret_scan_disabled). Present ONLY when ephemeral ⇒ a persistent store's
                # envelope is byte-identical (dropping this guard ⇒ the always-emits mutation).
                snap["store_ephemeral"] = True
            # trust-lifecycle telemetry: quarantine pile-up must be visible, never
            # silent. Best-effort — an older store double (tests) may lack these.
            try:
                snap["trust_counts"] = self.store.trust_counts()
                snap["n_misses_7d"] = self.store.miss_count_since(
                    int(self.now()) - 7 * _DAY_S)
            except Exception:                                # noqa: BLE001 — telemetry only
                _log.warning("mcp.health_trust_probe_failed", extra={
                    "event": "mcp.health_trust_probe_failed"}, exc_info=True)
            hint = self._solo_hint()                         # stalls self-describe
            if hint is not None:
                snap["solo_hint"] = hint
            if args.get("include_trends"):
                snap["trends"] = self._trends_report()       # CV4: the convergence KPI
            if args.get("include_gaps"):
                snap["gaps"] = self._gap_report()
            # the conflict worklist is gated by BOTH the request flag AND conflict.enabled
            # (OFF ⇒ byte-inert, no key); dropping the request flag ⇒ always-emits mutation.
            if self.conflict.enabled and args.get("include_conflicts"):
                snap["conflicts"] = self._conflict_report()
            # the suspect-consensus worklist is gated SOLELY by the request flag (no config
            # toggle — byte-inert until requested); dropping the request flag ⇒ always-emits mutation.
            if args.get("include_suspect_consensus"):
                snap["suspect_consensus"] = self._suspect_consensus_report()
            return snap
        except Exception as e:                               # fail-closed subset ONLY
            _log.error("mcp.health_probe_fail", extra={"event": "mcp.health_probe_fail",
                       "error_type": type(e).__name__, "db_path": self.db_path},
                       exc_info=True)
            return {"ok": False, "error": f"{type(e).__name__}: {e}",
                    "db_path": self.db_path}

    # ── health helpers ────────────────────────────────────────────────────────
    def _solo_hint(self) -> Optional[str]:
        """When single-IDENTITY traffic is WASTING demand (≥ demand_m window
        misses, all from ≤1 identity) under the anti-gaming rule, promotion is
        silently inert — return the self-describing hint. None when autonomy is
        off, the store is empty/quiet, ≥2 identities are present, or the probe
        faults (telemetry only, never breaks health). With the solo-mode span
        bypass removed, this is the ONLY feedback signal that turns a silent
        identity-collapse into a visible, actionable one."""
        try:
            if not bool(getattr(self.autonomy, "enabled", True)):
                return None
            window_s = int(self.autonomy.demand_window_days) * _DAY_S
            misses = self.store.misses_window(int(self.now()) - window_s)
            if len(misses) < int(self.autonomy.demand_m):    # ← the wasted-demand floor
                return None
            if len({m.agent_id for m in misses}) > 1:
                return None
            return ("single-identity traffic: demand-promotion is inert under the "
                    "anti-gaming rule because your agents share one identity — "
                    "register each agent via `hive connect` so every session carries "
                    "a distinct X-Hive-Agent-Id (remote seats: one token each).")
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
        """CV4: current-vs-previous 7d windows over existing tables — the
        demand-health KPI (confident_rate ↑, demand_entropy ↓). The ONLY window
        into silent fail-open rot (THEORY §8 tension #3). Composes the gaps clustering;
        degrades to {} on a fault."""
        try:
            tau = float(self.autonomy.demand_tau)
            return compute_trends(
                self.store, lambda rows: cluster_misses(rows, tau=tau),
                now=int(self.now()))
        except Exception:                                    # noqa: BLE001 — telemetry only
            _log.warning("mcp.trends_report_failed", extra={
                "event": "mcp.trends_report_failed"}, exc_info=True)
            return {}

    def _conflict_report(self) -> list[dict]:
        """The conflict/redundancy worklist (THEORY I1/I2 — detection, never resolution):
        mechanical near-dup detections over the SERVABLE set (anchor-bucketed for precision +
        bound — a cross-anchor near-dup still surfaces at recall, just not here) MERGED with
        open advisory flags whose BOTH episodes are still servable. The servable-both filter is
        how a flag auto-clears the instant either side is retired (no status flip needed).
        Mechanical entries are globally capped at conflict.top_n by cosine. Degrades to [] on a
        probe fault (a side-channel, fail-open — never breaks health)."""
        try:
            now = int(self.now())
            ttl_s = int(self.autonomy.provisional_ttl_days) * _DAY_S
            labeled = self.store.scan_servable_labeled(now=now, provisional_ttl_s=ttl_s)
            servable_ids = {row[0] for row in labeled}
            buckets: dict[str, list[ConflictItem]] = {}
            for eid, value, polarity, anchor, ts, trust in labeled:
                buckets.setdefault(anchor, []).append(ConflictItem(
                    episode_id=eid, vector=value, polarity=polarity, anchor=anchor,
                    ts=ts, trust=trust))
            top_n = int(self.conflict.top_n)
            notes = []
            for items in buckets.values():
                notes.extend(detect_conflicts(items, tau=float(self.conflict.tau),
                                              top_n=top_n))
            notes.sort(key=lambda n: -n.cosine)          # global cap across all buckets
            out = [_conflict_note_to_wire(n) for n in notes[:top_n]]
            # merge open advisory flags, filtered to both-episodes-still-servable
            for flag in self.store.open_conflict_flags():
                if flag["a_id"] in servable_ids and flag["b_id"] in servable_ids:
                    out.append(_flag_to_wire(flag))
            return out
        except Exception:                                # noqa: BLE001 — telemetry only
            _log.warning("mcp.conflict_report_failed", extra={
                "event": "mcp.conflict_report_failed"}, exc_info=True)
            return []

    def _suspect_consensus_report(self) -> list[dict]:
        """The suspect-consensus worklist (THEORY §10 O7 — detection, never resolution):
        PROVISIONAL promotions whose stamped demand had thin EFFECTIVE INDEPENDENCE
        (``n_eff / k`` below the floor) — the confidently-wrong-but-popular failure mode. Scans
        the SERVABLE set, keeps the provisional (demand-promoted, not human-vouched) ids, reads
        each one's newest ``promote`` audit ``demand_independence`` stamp, and runs the pure
        detector. Ids-only wire dicts (no memory text, Law 4); a provisional with no stamp
        (pre-3b promotion, or never demand-promoted) under-claims (omitted).

        ``has_settled_win`` is now SOURCED from ``store.settled_wins`` (ids with >= 1
        ``outcome_helped`` audit logged via hive_outcome — a self-reported settled win). The
        martingale clause sharpens: a thin provisional WITH a helped report drops to
        ``martingale_warning=False`` (popular-but-corroborated), one WITHOUT stays True
        (popular-but-uncorroborated). NOTE this is a TRUSTED-SOLO self-report: in a hostile
        fleet a poisoner could self-report ``helped`` on its poison to suppress its own flag —
        moot in the operator's trusted-solo stance, and the worklist is detection-only (no
        auto-action). The fleet-safe form (verified-artifact binding + helped from >= N
        decorrelated identities) stays deferred.

        Degrades to [] on any probe fault (a side-channel must never break health, fail-open)."""
        try:
            now = int(self.now())
            ttl_s = int(self.autonomy.provisional_ttl_days) * _DAY_S
            labeled = self.store.scan_servable_labeled(now=now, provisional_ttl_s=ttl_s)
            # (id, value, polarity, anchor, ts, trust) — keep provisional (demand-promoted) only
            anchor_by_id = {row[0]: row[3] for row in labeled if row[5] == "provisional"}
            prov = self.store.promotion_provenance(list(anchor_by_id))
            settled = self.store.settled_wins(list(prov))    # ids with >= 1 self-reported helped
            items = [SuspectConsensusItem(
                        episode_id=eid, rho_bar=rho_bar, n_eff=n_eff, k=k,
                        has_settled_win=(eid in settled), anchor=anchor_by_id.get(eid, ""))
                     for eid, (rho_bar, n_eff, k) in prov.items()]
            notes = detect_suspect_consensus(
                items, n_eff_frac_max=float(self.suspect_consensus.n_eff_frac_max),
                top_n=int(self.suspect_consensus.top_n))
            return [{"episode_id": n.episode_id, "rho_bar": round(float(n.rho_bar), 4),
                     "n_eff": round(float(n.n_eff), 4), "k": n.k,
                     "martingale_warning": n.martingale_warning,
                     "note": ("provisional promoted on thin effective-independence demand "
                              "(correlated asks) — re-examine; resolve via hive_supersede "
                              "(replace) or hive_prune (retire if wrong)")}
                    for n in notes]
        except Exception:                                # noqa: BLE001 — telemetry only
            _log.warning("mcp.suspect_consensus_report_failed", extra={
                "event": "mcp.suspect_consensus_report_failed"}, exc_info=True)
            return []

    def _db_size(self) -> int:
        try:
            return os.path.getsize(self.db_path) if self.db_path and \
                os.path.exists(self.db_path) else 0
        except OSError:
            return 0


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
_DEFAULT_TENANT_ID = "default"


def _configure_logging(level: int = logging.INFO) -> None:
    """Bind the structured-JSON-to-stderr handler onto the ``hive`` logger (stdout is the
    JSON-RPC channel). Idempotent; a logging-setup failure NEVER aborts the boot. // O(1)."""
    try:
        from hive.app.observability import configure_json_logging  # noqa: PLC0415 — lazy
        configure_json_logging(level=int(level), stream=True)
    except Exception as exc:                       # noqa: BLE001 — logging setup never aborts boot
        _log.warning("mcp_server.log_config_failed kind=%s", type(exc).__name__)


def _resolve_identity(tenant: Optional[str], db: Optional[str], agent: Optional[str],
                      env: Mapping[str, str]) -> tuple[str, str, str]:
    """Resolve ``(tenant_id, db_path, agent_id)`` — CLI arg takes precedence, then env, then a
    safe default for all three. ``tenant_id`` is a constant label (never a query filter — the
    single-tenant boundary), defaulting to ``"default"`` when unset so the server boots
    zero-config. NEVER echoes an env *value* (secret-safe). // O(1)."""
    tenant_id = (tenant or env.get("HIVE_TENANT_ID") or "").strip() or _DEFAULT_TENANT_ID
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
                        help="tenant id label (default: $HIVE_TENANT_ID or 'default')")
    parser.add_argument("--agent", default=None,
                        help="agent id, stamped as proposed_by (default: $HIVE_AGENT_ID or default-agent)")
    args = parser.parse_args(argv)

    _configure_logging()                          # JSON→stderr before anything can fail

    # ── config.loaded (EX_CONFIG on Config validation failure) ──
    tenant_id, db_path, agent_id = _resolve_identity(args.tenant, args.db, args.agent, env)

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
