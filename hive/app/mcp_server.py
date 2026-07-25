"""HiveMCPServer — the single MCP/JSON-RPC trust boundary (M06 / C7).

A thin driving adapter (hexagonal): it maps the eight ``hive_*`` JSON-RPC verbs onto the
already-built domain ports and owns NO data. PORT+EXTEND of the AgentCortex
``serving/mcp_server.py`` stdio loop — KEEP the MCPRequest/MCPResponse/_err envelopes,
the dispatch table, ``tools/list = TOOL_DEFINITIONS``, and the
``{content:[{type:text,text:json}], isError}`` framing. There is no approver anywhere
(v3 thin-agent contract): ``hive_write`` lands ``trust='provisional'`` and serves NOW;
retirement (``hive_prune`` / ``hive_supersede`` / ``hive_write(replaces=)``) is
agent-called but MACHINE-GATED — the boundary assembles the §3.2 gate feeds and calls
the ONE pure owner ``hive.domain.retirement.retirement_evidence`` BEFORE any store
mutation; an unqualified target is a benign noop envelope, never an error.

Three load-bearing belts live HERE (in addition to the store-query suspenders):
  1. **Schema enforcement** — ``_validate_args`` runs over TOOL_DEFINITIONS BEFORE any
     port is touched; a malformed call never reaches the store (handlers read args
     permissively via ``.get`` so this belt is the SOLE required-field guard). It reads
     the SAME schema objects tools/list advertises (array items included), so the
     advertised and the enforced contract cannot diverge.
  2. **Servable-only recall** — ``_handle_recall`` re-filters every candidate through
     ``is_servable`` independent of the pipeline/index, and an empty post-belt
     set is an ABSTAIN, never a confident-empty (never-hallucinate, structural).
  3. **Scope grammar** — ``anchors``/``repos`` pass ``normalize_anchors``/
     ``normalize_repos`` (the boundary grammar gate); a violation — including an
     unregistered repo name — is a clean ``refused`` envelope, nothing stored.

Recalled text is framed under ``reference_context`` (never ``instructions``); every
recall envelope carries ``trace_id`` on hit AND abstain (the move-#6 join key), and
every hit carries its ``repos`` / ``anchors`` / ``drift`` (§3.4 — drift attached via
the fail-open ``hive.app.drift.attach_drift`` enrichment).
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
from typing import Any, Callable, Mapping, Optional, TextIO

from hive.app.anchors import BadAnchors, normalize_anchors, normalize_repos
from hive.app.census_health import census_health_report
from hive.app.contract import REMEDIATION_NOTICE, SERVER_INSTRUCTIONS
from hive.app.drift import attach_drift, tip_for
from hive.app.gaps import cluster_misses
from hive.app.trends import compute_trends
from hive.app.tool_defs import TOOL_DEFINITIONS
from hive.domain.conflict import (
    CONTRADICTION,
    ConflictItem,
    ConflictNote,
    _cosine,
    detect_conflicts,
)
from hive.domain.consensus import (
    SuspectConsensusItem,
    detect_suspect_consensus,
)
from hive.domain.errors import SecretRefused
from hive.domain.evidence_kinds import (
    EK_OUTCOME_HELPED,
    EK_OUTCOME_HURT,
    EK_OUTCOME_VERIFIED_HURT,
    EK_PRUNE,
    EK_SUPERSEDE,
    EK_VERIFY_CURRENT,
    EK_VERIFY_STALE,
)
from hive.domain.kinds import DEFAULT_KIND
from hive.domain.lifecycle import DEPRECATED, is_servable
from hive.domain.meta import BadMeta, normalize_meta
from hive.domain.models import CONFIDENT, AnchorRef, Episode
from hive.domain.recall import RecallScope
from hive.domain.retirement import Eligibility, retirement_evidence
from hive.domain.secret_scan import ScanVerdict

_log = logging.getLogger("hive.app.mcp_server")

_DAY_S = 86_400

MCP_VERSION = "2024-11-05"
SERVER_NAME = "hive"
SERVER_VERSION = "1.0.0"

_PREVIEW_LEN = 160
# inputSchema by tool name — the single source for the pre-dispatch validation belt.
_SCHEMA_BY_NAME: dict[str, dict[str, Any]] = {
    t["name"]: t["inputSchema"] for t in TOOL_DEFINITIONS
}

# ── the §3.2 retirement-gate wire strings (normative — CT-7/CT-1 assert them) ──
# The unqualified-supersede rider marker on a write envelope, and the noop reason on
# the retirement verbs. One owner each; the em-dash is part of the contract.
GATE_NOOP_SIGNAL = "no qualifying machine signal"
GATE_NOOP_REASON = GATE_NOOP_SIGNAL + " — nothing retired"

# The ledger kinds the retirement gate reads (§3.2 clauses 1b/2). The boundary names
# what qualifies; an unfiltered read would hand the gate foreign kinds to ignore.
_GATE_LEDGER_KINDS = (
    EK_VERIFY_CURRENT,
    EK_VERIFY_STALE,
    EK_OUTCOME_HURT,
    EK_OUTCOME_VERIFIED_HURT,
)

_INELIGIBLE = Eligibility(False, ())


# ── JSON-RPC envelopes (PORT) ────────────────────────────────────────────────


@dataclass
class MCPRequest:
    id: Any
    method: str
    params: dict[str, Any]


@dataclass
class MCPResponse:
    id: Any
    result: Optional[dict[str, Any]] = None
    error: Optional[dict[str, Any]] = None

    def to_json(self) -> str:
        out: dict[str, Any] = {"jsonrpc": "2.0", "id": self.id}
        if self.error is not None:
            out["error"] = self.error
        else:
            out["result"] = self.result
        return json.dumps(out, default=_json_default)


def _err(code: int, message: str, data: Any = None) -> dict[str, Any]:
    e: dict[str, Any] = {"code": code, "message": message}
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


def _validate_items(key: str, val: list[Any], items: dict[str, Any]) -> Optional[str]:
    """Per-item validation of an array field against its advertised ``items`` schema —
    the SAME dict object ``tool_defs`` advertises (e.g. ``_ANCHORS_PROPERTY['items']``),
    never a parallel shape. Scalar items get a type check; object items additionally get
    required-key, per-property type, and (under ``additionalProperties: False``)
    extra-key checks. Registry membership (is the repo REGISTERED?) is deliberately NOT
    here — that is the ``normalize_anchors`` boundary gate's job (a clean ``refused``
    envelope, not a schema reject). // O(items · keys)."""
    t = items.get("type")
    for i, item in enumerate(val):
        if t is not None and not _type_ok(item, t):
            return f"field {key!r}[{i}] must be of type {t}"
        if t != "object" or not isinstance(item, dict):
            continue
        for req in items.get("required", []):
            if req not in item:
                return f"field {key!r}[{i}] missing required key: {req!r}"
        item_props = items.get("properties", {})
        for k, v in item.items():
            spec = item_props.get(k)
            if spec is None:
                if items.get("additionalProperties") is False:
                    return f"field {key!r}[{i}] has unknown key: {k!r}"
                continue
            pt = spec.get("type")
            if pt is not None and not _type_ok(v, pt):
                return f"field {key!r}[{i}].{k} must be of type {pt}"
    return None


def _validate_args(name: str, args: dict[str, Any]) -> Optional[str]:
    """Return an error message if ``args`` violate the tool's inputSchema, else None.
    Checks required[], scalar types, enum membership, and array ITEMS (object items —
    the ``anchors`` grammar — down to their required/typed/no-extra keys). Extra
    top-level fields are ignored (permissive — a stale client arg like ``approved_by``
    is ignored-extra, never a refusal). // O(fields) time. The deleting of the CALL to
    this is mutation #1 — handlers do no required-field guard of their own."""
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
        items = spec.get("items")
        if t == "array" and isinstance(val, list) and isinstance(items, dict):
            err = _validate_items(key, val, items)
            if err is not None:
                return err
    return None


def _preview(text: str) -> str:
    text = text or ""
    return text if len(text) <= _PREVIEW_LEN else text[:_PREVIEW_LEN] + "…"


def _scan_report(verdict: ScanVerdict) -> dict[str, Any]:
    """The ScanReport envelope — rule NAMES + count only, NEVER the secret bytes."""
    return {
        "action": verdict.action,
        "rules": [f.rule for f in verdict.findings],
        "n_findings": len(verdict.findings),
    }


def _anchor_to_wire(a: AnchorRef, declared_refs: Mapping[str, str]) -> dict[str, Any]:
    """One served anchor entry: ``{repo, anchor}`` plus the memory's DECLARED
    ``ref`` for that repo when it named one — omitted when canonical (the
    no-line envelope stays byte-inert), never a caller-supplied value.
    ``declared_refs`` is the episode's own ``episode_refs`` projection (repo ->
    ref), the input a branch-scoped drift verdict routes an anchor on."""
    d: dict[str, Any] = {"repo": a.repo, "anchor": a.anchor}
    ref = declared_refs.get(a.repo, "")
    if ref:
        d["ref"] = ref
    return d


def _abstain_note(state: str) -> str:
    """A neutral, non-instruction note for an empty/abstained recall."""
    if state == "EMPTY_NO_DATA":
        return "no approved memories are available to match this query"
    return "no confident match — abstained rather than surface a weak guess"


def _conflict_note_to_wire(note: ConflictNote) -> dict[str, Any]:
    """A mechanical ``ConflictNote`` → the ids-only wire dict. Carries NO memory text (Law 4):
    only ids, the relation, the cosine, the shared anchor, and a directional retire HINT with
    the resolution call spelled out — never an applied action here (the retirement verbs stay
    machine-gated at their own boundary, THEORY §10 O7)."""
    d: dict[str, Any] = {
        "a_id": note.a_id,
        "b_id": note.b_id,
        "relation": note.relation,
        "cosine": round(float(note.cosine), 4),
        "anchor": note.anchor,
        "loser_hint": note.loser_hint,
        "source": "mechanical",
    }
    if note.relation == CONTRADICTION:
        d["note"] = (
            "opposing polarity (do vs dont) on near-duplicate memories — they "
            "disagree; resolve which holds via hive_supersede (machine-gated)"
        )
    else:
        winner = note.b_id if note.loser_hint == note.a_id else note.a_id
        d["note"] = (
            f"near-duplicate memories; {note.loser_hint} is the lower-trust/older "
            f"retire candidate (hive_supersede loser={note.loser_hint} "
            f"winner={winner})"
        )
    return d


def _flag_to_wire(flag: dict[str, Any]) -> dict[str, Any]:
    """An open advisory ``conflict_flags`` row → the worklist wire dict. ``kind`` is the
    advisory vocabulary (conflict|supersedes); ``resolution`` is the flagger's secret-scanned
    rationale. ids only — no memory text rides it."""
    return {
        "a_id": flag["a_id"],
        "b_id": flag["b_id"],
        "kind": flag["kind"],
        "winner_id": flag["winner_id"],
        "resolution": flag["resolution"],
        "proposed_by": flag["proposed_by"],
        "source": "advisory",
        "note": "agent-flagged advisory; resolve via hive_supersede (machine-gated)",
    }


def _parse_repos_json(raw: object) -> list[str]:
    """The stored ``recall_misses.repos`` JSON array → a name list. '' (a global miss)
    and anything unparseable read as [] — the writer (the recall pipeline) owns the
    grammar; a corrupt scope never breaks the demand read (mirrors the store's own
    MissRow parse)."""
    if not isinstance(raw, str) or not raw:
        return []
    try:
        parsed = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(parsed, list):
        return []
    return [x for x in parsed if isinstance(x, str) and x]


class HiveMCPServer:
    """JSON-RPC 2.0 over stdio. One process == one ``ServerIdentity``."""

    def __init__(
        self,
        *,
        admission: Any,
        recall: Any,
        store: Any,
        embedder: Any,
        identity: ServerIdentity,
        now: Callable[[], int],
        started_ts: int = 0,
        db_path: str = "",
        autonomy: Any = None,
        conflict: Any = None,
        flag_service: Any = None,
        suspect_consensus: Any = None,
        secret_scan_enabled: bool = True,
    ) -> None:
        self.admission = admission  # AdmissionService: write + capture
        self.recall = recall  # RecallPipeline: recall(query, *, agent_id)
        self.store = store  # the store adapter: get_episode (belt) / counts
        self.embedder = embedder  # EmbeddingProvider: health probes (d, name)
        self.identity = identity
        self.now = now
        self.started_ts = int(started_ts)
        self.db_path = db_path
        # the lifecycle knob group (duck-typed AutonomyConfig); None ⇒ defaults.
        # The recall belt's per-hit is_servable re-check reads provisional_ttl off
        # it; the gap report reads demand_window/demand_tau.
        if autonomy is None:
            from hive.app.config import AutonomyConfig  # noqa: PLC0415 — lazy default

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
            from hive.app.config import ConflictConfig  # noqa: PLC0415 — lazy default

            conflict = ConflictConfig(enabled=False)
        self.conflict = conflict
        self.flag_service = flag_service
        # suspect-consensus worklist (duck-typed SuspectConsensusConfig). The worklist is gated
        # SOLELY by the include_suspect_consensus request flag (no config enabled toggle), so a
        # bare-construction server's health envelope is byte-identical until the flag is set — the
        # same single-gate posture as the conflict worklist. Detection-only — it reads stamped
        # promote audits, never retires (Law 3).
        if suspect_consensus is None:
            from hive.app.config import SuspectConsensusConfig  # noqa: PLC0415 — lazy default

            suspect_consensus = SuspectConsensusConfig()
        self.suspect_consensus = suspect_consensus
        # The credential secret-floor posture (HIVE_SECRET_SCAN__ENABLED, default ON). The actual
        # scan/bypass is owned by the injected scanner adapter; the boundary holds this ONLY to
        # surface the loosened posture in hive_health (a disabled floor must never be silent — it
        # is also WARN-logged at boot). Default True ⇒ no health key ⇒ byte-identical envelope.
        self.secret_scan_enabled = bool(secret_scan_enabled)
        self._tool_handlers: dict[
            str, Callable[[dict[str, Any], ServerIdentity], dict[str, Any]]
        ] = {
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
    def handle(
        self, req: MCPRequest, *, identity: Optional[ServerIdentity] = None
    ) -> MCPResponse:
        # Per-request identity (the HTTP daemon's verified caller); ``None`` ⇒ the process
        # ``ServerIdentity`` (the stdio path + every existing test pass ``req`` only, so they
        # are byte-for-byte unchanged). The transport resolves WHO calls; attribution stays
        # in the handlers (D1).
        ident = identity or self.identity
        if req.method == "initialize":
            return MCPResponse(
                id=req.id,
                result={
                    "protocolVersion": MCP_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                    # the spec-canonical usage contract every client surfaces at connect (M2 channel)
                    "instructions": SERVER_INSTRUCTIONS,
                },
            )
        if req.method == "tools/list":
            return MCPResponse(id=req.id, result={"tools": TOOL_DEFINITIONS})
        if req.method == "tools/call":
            return self._tools_call(req, ident)
        if req.method == "ping":
            return MCPResponse(id=req.id, result={})
        return MCPResponse(
            id=req.id, error=_err(-32601, f"method not found: {req.method}")
        )

    def _tool_result(
        self, req_id: Any, content: dict[str, Any], *, is_error: bool
    ) -> MCPResponse:
        # v3: NO contract-version beacon — the usage contract reaches every agent fresh at
        # connect via the initialize instructions; there is no installed block to drift
        # from, so no per-result version stamp exists (CT-11 pins its absence).
        return MCPResponse(
            id=req_id,
            result={
                "content": [
                    {"type": "text", "text": json.dumps(content, default=_json_default)}
                ],
                "isError": is_error,
            },
        )

    def _tool_error(self, req_id: Any, message: str) -> MCPResponse:
        return MCPResponse(
            id=req_id,
            result={"content": [{"type": "text", "text": message}], "isError": True},
        )

    def _tools_call(self, req: MCPRequest, identity: ServerIdentity) -> MCPResponse:
        # a raw JSON field: any client-sent shape reaches the .get lookup unchanged
        # (a non-string name simply resolves no handler → the -32602 reply below)
        name: Any = req.params.get("name")
        args = req.params.get("arguments", {}) or {}
        handler = self._tool_handlers.get(name)
        if handler is None:  # unknown tool → JSON-RPC error
            _log.warning(
                "mcp.unknown_tool",
                extra={
                    "event": "mcp.unknown_tool",
                    "tool": name,
                    "agent_id": identity.agent_id,
                },
            )
            return MCPResponse(id=req.id, error=_err(-32602, f"unknown tool: {name}"))
        # ── BELT 1: schema validation BEFORE any port is touched (mut #1) ──
        verr = _validate_args(name, args)
        if verr is not None:
            _log.info(
                "mcp.schema_reject",
                extra={
                    "event": "mcp.schema_reject",
                    "tool": name,
                    "reason": verr,
                    "agent_id": identity.agent_id,
                },
            )
            return self._tool_error(req.id, f"invalid arguments: {verr}")
        try:
            content = handler(args, identity)  # per-request identity → the handler
            return self._tool_result(req.id, content, is_error=False)
        except SecretRefused:
            # only hive_write raises this and it returns its own refused envelope;
            # reaching here means a defensive double-raise — treat as a tool error.
            raise
        except Exception as e:  # loop survives; stack → log, NOT agent
            _log.error(
                "mcp.tool_raised",
                extra={
                    "event": "mcp.tool_raised",
                    "tool": name,
                    "agent_id": identity.agent_id,
                    "error_type": type(e).__name__,
                },
                exc_info=True,
            )
            return self._tool_error(req.id, f"error: {type(e).__name__}: {e}")

    # ── the §3.2 retirement-evidence gate feeds (assembled HERE, judged in-domain) ──
    def _known_repos(self) -> Callable[[str], bool]:
        """The injected registry-membership predicate ``normalize_anchors``/
        ``normalize_repos`` consume — lazy, so a call with no scope args never reads
        the registry."""
        return lambda name: any(row.name == name for row in self.store.repo_registry())

    def _gate_conflict_pairs(self, ep: Episode) -> tuple[ConflictNote, ...]:
        """§3.2 clause 3 feed: mechanical near-dup pairs detected AT GATE TIME between
        the target and the co-servable field (contradiction or redundancy — both are
        machine evidence). The target is appended when not itself servable (e.g. a
        quarantined row being pruned) so 'target vs a co-servable row' stays reachable.
        Near-dup floor = ``conflict.tau`` (the ONE owner). Raises freely — the caller
        owns the fail-closed."""
        now = int(self.now())
        ttl_s = int(self.autonomy.provisional_ttl_days) * _DAY_S
        labeled = self.store.scan_servable_labeled(now=now, provisional_ttl_s=ttl_s)
        items = [
            ConflictItem(
                episode_id=int(eid),
                vector=value,
                polarity=polarity,
                anchor=anchor,
                ts=int(ts),
                trust=trust,
            )
            for eid, value, polarity, anchor, ts, trust in labeled
        ]
        if int(ep.id) not in {it.episode_id for it in items} and ep.value is not None:
            items.append(
                ConflictItem(
                    episode_id=int(ep.id),
                    vector=ep.value,
                    polarity=ep.polarity,
                    anchor=(ep.anchors[0].anchor if ep.anchors else ""),
                    ts=int(ep.ts),
                    trust=ep.trust,
                )
            )
        if len(items) < 2:
            return ()
        cap = max(1, len(items) * (len(items) - 1) // 2)  # never top_n-starved
        notes = detect_conflicts(items, tau=float(self.conflict.tau), top_n=cap)
        return tuple(n for n in notes if n.a_id == int(ep.id) or n.b_id == int(ep.id))

    def _gate_drift_verdicts(self, ep: Episode) -> list[str]:
        """§3.2 clause 1a feed: the materialized drift verdicts for the target's
        anchors at each anchor repo's OWN declared tip — ``ref_tip(repo,
        declared_ref)`` when the episode declared a line for that repo (via
        ``episode_refs``), else the CANONICAL tip (the sync watermark meta key).
        ``branch_route_verdict`` is deliberately NOT applied here: consumer ==
        declared by construction, so ``branch_scoped`` can never arise and a
        dead anchor on the memory's own line still qualifies for retirement —
        this is what keeps a declared ref from becoming caller-asserted
        immunity (THEORY §9 #6). An unknown tip / un-materialized anchor
        contributes nothing (absence never qualifies). Raises freely — the
        caller owns the fail-closed."""
        verdicts: list[str] = []
        if not ep.anchors:
            return verdicts  # general/scope-only: drift is n/a
        by_repo: dict[str, list[str]] = {}
        for a in ep.anchors:
            by_repo.setdefault(a.repo, []).append(a.anchor)
        declared = self.store.episode_refs(int(ep.id))
        for repo, anchors in by_repo.items():
            tip = tip_for(self.store, repo, declared.get(repo, ""))
            if not tip:
                continue
            cached = self.store.drift_get(repo, tip, anchors)
            for entry in cached.values():
                if isinstance(entry, (tuple, list)) and entry:
                    verdicts.append(str(entry[0]))
        return verdicts

    def _gate_own_lines(self, ep: Episode) -> Optional[frozenset[str]]:
        """§3.2 clause 1b's line filter: the line whose ledger evidence is THIS
        memory's own. A verify payload stamps the LINE it was measured on but NOT
        the repo, so a row is attributable only when the memory has exactly ONE
        anchored repo:

          - no anchors           -> None  (clause 1b is unreachable for an
                                           anchor-less memory: change_evidence.
                                           ingest joins only anchored_episodes(),
                                           which filters anchor != '')
          - != 1 anchored repo   -> frozenset()  (nothing attributable — under-claim)
          - 1 repo, declared ref -> frozenset({ref})
          - 1 repo, no declared  -> None  (it named no line, so canonical rows ARE
                                           its own; byte-identical to
                                           pre-declared-line behaviour)

        Under-claim, never over-claim. Raises freely — the caller owns the
        fail-closed. // marker: returning a non-empty set for a memory with MORE
        THAN ONE anchored repo is the named mutation — the payload carries no
        repo, so another repo's staleness on a same-named line would retire it
        (reds tests/mcp/test_retirement_gate_boundary.py::
        test_multi_repo_memory_is_never_attributable and its unknown-line twin)."""
        repos = {a.repo for a in ep.anchors}
        if not repos:
            return None  # clause 1b is unreachable for an anchor-less memory
        if len(repos) != 1:
            return frozenset()  # whose line is it? undecidable ⇒ nothing qualifies
        ref = self.store.episode_refs(int(ep.id)).get(next(iter(repos)), "")
        return frozenset({ref}) if ref else None

    def _retirement_eligibility(
        self, ep: Episode, caller_identity: str, winner: Optional[Episode] = None
    ) -> Eligibility:
        """Assemble every §3.2 gate feed and run the ONE pure owner
        ``retirement_evidence``. ``winner`` (supersede only): the boundary owns the
        ``conflict.tau`` threshold (Law 4 — config never reaches the pure gate), so
        ``winner_cosine`` is passed through ONLY when it already cleared tau.
        FAIL-CLOSED: any feed-reader fault ⇒ ineligible ⇒ the caller noops — a broken
        feed can refuse a retirement, never allow one. // marker: deleting the CALL to
        this in a retirement handler is the named mutation — a healthy, evidence-less
        target gets retired, which reds CT-7's noop-on-healthy tests
        (tests/contract/test_retirement_gate.py) and their boundary twins
        (tests/mcp/test_retirement_gate_boundary.py)."""
        try:
            drift_verdicts = self._gate_drift_verdicts(ep)
            rows = self.store.evidence_rows_for(int(ep.id), _GATE_LEDGER_KINDS)
            pairs = self._gate_conflict_pairs(ep)
            winner_cosine = None
            if winner is not None:
                c = _cosine(ep.value, winner.value)
                if c is not None and c >= float(self.conflict.tau):
                    winner_cosine = c
            return retirement_evidence(
                episode=ep,
                caller_identity=caller_identity,
                drift_verdicts=drift_verdicts,
                evidence_rows=rows,
                conflict_pairs=pairs,
                winner_cosine=winner_cosine,
                own_lines=self._gate_own_lines(ep),
            )
        except Exception:  # noqa: BLE001 — undecidable ⇒ noop
            _log.warning(
                "mcp.retirement_gate_feed_failed",
                extra={
                    "event": "mcp.retirement_gate_feed_failed",
                    "episode_id": int(getattr(ep, "id", -1)),
                },
                exc_info=True,
            )
            return _INELIGIBLE

    def _stamp_retirement_audit(
        self, episode_id: int, kind: str, actor: str, signals: tuple[str, ...]
    ) -> None:
        """Stamp exactly WHICH machine signal(s) authorized a retirement into the
        ledger (one server-written row beside the store's own supersede/prune row).
        Runs only AFTER a successful retire; a stamp fault propagates loudly rather
        than leaving a silently-unattributed retirement."""
        self.store.insert_audit(
            int(episode_id),
            kind,
            actor,
            int(self.now()),
            json.dumps({"signals": list(signals)}),
        )

    # ── tool handlers: (args, identity); args read permissively, _validate_args is the only
    #    required-field guard. ``identity`` is the per-request caller resolved by the transport
    #    (HTTP daemon) or the process default (stdio) — attribution lives HERE (D1). ──
    def _handle_write(
        self, args: dict[str, Any], identity: ServerIdentity
    ) -> dict[str, Any]:
        text = args.get("text")
        # proposed_by is ALWAYS the authenticated caller — INV-2: no client field to assert it
        # (``proposed_by`` is gone from the write schema). Deliberate mutation: read self.identity here.
        proposed_by = identity.agent_id
        polarity = args.get(
            "polarity", "neutral"
        )  # do|dont|neutral (schema-enum-checked)
        kind = args.get(
            "kind", DEFAULT_KIND
        )  # registry vocabulary (schema-enum-checked)
        try:
            meta = normalize_meta(args.get("meta"))  # the ONE grammar gate (meta.py)
        except BadMeta as e:
            return {"status": "refused", "reason": f"bad meta: {e}"}
        try:
            known = self._known_repos()  # the scope grammar gate (anchors.py)
            anchors = normalize_anchors(args.get("anchors"), known_repos=known)
            scope = normalize_repos(args.get("repos"), known_repos=known)
        except BadAnchors as e:
            return {"status": "refused", "reason": str(e)}  # nothing stored

        # ── the §3.2 replaces= retirement rider, gated BEFORE any store mutation.
        # UNKNOWN target ⇒ the rider passes through and admission fails the WHOLE call
        # loudly (nothing stored — a caller bug, not an unqualified target). A KNOWN
        # target is retired IFF the gate qualifies; else the write still lands and the
        # envelope reports the rider noop.
        replaces = args.get("replaces")
        gate: Optional[Eligibility] = None
        rider: Optional[int] = None
        if replaces is not None:
            rider = int(replaces)
            target = self.store.get_episode(rider)
            if target is not None:
                gate = self._retirement_eligibility(target, identity.agent_id)
                if not gate.eligible:
                    rider = None  # target untouched; the write proceeds
        try:
            res = self.admission.write(
                text,
                proposed_by=proposed_by,
                replaces=rider,
                polarity=polarity,
                kind=kind,
                anchors=anchors,
                repos=scope,
                meta=meta,
            )
        except SecretRefused as e:
            # REFUSE: nothing written (0 rows). Envelope carries rule NAMES, never bytes.
            return {
                "status": "refused",
                "reason": str(e),
                "scan": {
                    "action": "refuse",
                    "rules": e.rules,
                    "n_findings": e.n_findings,
                },
            }
        # CLEAN/REDACT both land servable NOW as trust=provisional (v3 §3.1); the
        # envelope labels the row's ACTUAL trust (a dedup onto an established row
        # honestly reads established — a write never demotes).
        ep = (
            self.store.get_episode(res.episode_id)
            if res.episode_id is not None
            else None
        )
        out: dict[str, Any] = {
            "status": res.status,
            "id": res.episode_id,
            "trust": (ep.trust if ep is not None else None),
            "scan": _scan_report(res.scan),
            "deduped": res.deduped,
        }
        if replaces is not None:
            out["superseded"] = res.superseded  # int when retired, else null
            if gate is not None and not gate.eligible:
                out["supersede_noop"] = GATE_NOOP_SIGNAL
            elif gate is not None and res.superseded is not None:
                self._stamp_retirement_audit(
                    res.superseded, EK_SUPERSEDE, identity.agent_id, gate.signals
                )
        if res.status == "redacted" and res.scan.redacted_text is not None:
            out["redacted_preview"] = _preview(res.scan.redacted_text)
        return out

    def _handle_capture(
        self, args: dict[str, Any], identity: ServerIdentity
    ) -> dict[str, Any]:
        text = args.get("text")
        polarity = args.get(
            "polarity", "neutral"
        )  # do|dont|neutral (schema-enum-checked)
        kind = args.get(
            "kind", DEFAULT_KIND
        )  # registry vocabulary (schema-enum-checked)
        try:
            meta = normalize_meta(args.get("meta"))  # the ONE grammar gate (meta.py)
        except BadMeta as e:
            return {"status": "refused", "reason": f"bad meta: {e}"}
        try:
            known = self._known_repos()  # the scope grammar gate (anchors.py)
            anchors = normalize_anchors(args.get("anchors"), known_repos=known)
            scope = normalize_repos(args.get("repos"), known_repos=known)
        except BadAnchors as e:
            return {"status": "refused", "reason": str(e)}  # nothing stored
        try:
            res = self.admission.capture(
                text,
                proposed_by=identity.agent_id,
                polarity=polarity,
                kind=kind,
                anchors=anchors,
                repos=scope,
                meta=meta,
            )
        except SecretRefused as e:
            # REFUSE: nothing written (0 rows). Envelope carries rule NAMES, never bytes.
            return {
                "status": "refused",
                "reason": str(e),
                "scan": {
                    "action": "refuse",
                    "rules": e.rules,
                    "n_findings": e.n_findings,
                },
            }
        if res.status == "disabled":  # autonomy off — nothing written
            return {"status": "disabled"}
        return {
            "status": res.status,
            "id": res.episode_id,
            "scan": _scan_report(res.scan),
            "deduped": res.deduped,
        }

    def _handle_recall(
        self, args: dict[str, Any], identity: ServerIdentity
    ) -> dict[str, Any]:
        query = args.get("query") or ""
        # ── the v3 partition ask (§3.3): each repos entry is ``name`` or ``name@branch``
        # (the branch names the drift tip only). An unregistered name is a clean refusal;
        # omitted/empty scope keeps the legacy GLOBAL read (no scope argument passed, so
        # a scope-less recall double stays byte-compatible).
        try:
            scope_pairs = normalize_repos(
                args.get("repos"), known_repos=self._known_repos()
            )
        except BadAnchors as e:
            return {"status": "refused", "reason": str(e)}
        anchor_prefix = args.get("anchor_prefix") or ""
        if scope_pairs or anchor_prefix:
            result = self.recall.recall(
                query,
                agent_id=identity.agent_id,
                scope=RecallScope(repos=scope_pairs, anchor_prefix=anchor_prefix),
            )
        else:
            result = self.recall.recall(query, agent_id=identity.agent_id)
        # ── BELT 2: servable-only re-filter, independent of the index (mut #2).
        # The per-hit is_servable re-check is the AUTHORITATIVE freshness layer: a
        # TTL-lapsed provisional row still sitting in the warm index is dropped HERE
        # even before the sweep materializes its death.
        now = int(self.now())
        belt_ttl_s = int(self.autonomy.provisional_ttl_days) * _DAY_S
        hits: list[dict[str, Any]] = []
        for h in result.hits:
            ep = self.store.get_episode(h.episode_id)
            if ep is None or not is_servable(  # ← delete this guard ⇒ mut #2
                status=ep.status,
                trust=ep.trust,
                last_active_ts=ep.last_active_ts,
                now=now,
                provisional_ttl_s=belt_ttl_s,
            ):
                _log.warning(
                    "mcp.recall_belt_drop",
                    extra={
                        "event": "mcp.recall_belt_drop",
                        "trace_id": result.trace_id,
                        "episode_id": h.episode_id,
                    },
                )
                continue
            declared_refs = self.store.episode_refs(ep.id) if h.anchors else {}
            hit = {
                "episode_id": h.episode_id,
                "text": h.text,
                "sim": float(h.sim),
                "trust": h.trust,
                "ts": h.ts,
                "polarity": h.polarity,
                "kind": h.kind,
                # §3.4: repos/anchors ride EVERY hit ([] = general/scope-only); each
                # anchor also carries the memory's declared ref for that repo, when
                # it named one (omitted when canonical).
                "repos": list(h.repos),
                "anchors": [_anchor_to_wire(a, declared_refs) for a in h.anchors],
            }
            if h.meta:  # omit-when-empty: the no-meta envelope is byte-inert
                hit["meta"] = h.meta
            hits.append(hit)
        # §3.4 drift enrichment — the ONE owner is hive.app.drift.attach_drift:
        # per-anchor verdicts at the queried ref's tip (else canonical), most-severe
        # aggregation, ref_requests demand touch for a queried non-canonical branch.
        # FAIL-OPEN by contract: a reader fault degrades a hit to unverifiable and
        # never breaks the read.
        attach_drift(hits, store=self.store, queried_repos=scope_pairs, now=now)
        # verification-recency stamp: derived ledger evidence enriched at the boundary,
        # its own FAIL-OPEN side-channel — a reader fault serves the envelope without
        # stamps, never breaks recall. A never-verified hit emits NO key (byte-inert);
        # the kernel never judges churn — the stamp is carried, the edge decides.
        if hits:
            try:
                # scoped to each memory's OWN declared line(s), read straight off
                # the anchors already built above (no second store read): the
                # rider must never report a memory stale on a line it never
                # claimed — the advisory twin of the retirement gate's own-line
                # rule. A memory that declared nothing filters nothing.
                lv = self.store.last_verification(
                    [h["episode_id"] for h in hits],
                    own_refs={
                        h["episode_id"]: frozenset(
                            ref
                            for a in h["anchors"]
                            if isinstance(a, dict) and (ref := a.get("ref"))
                        )
                        for h in hits
                    },
                )
                for hit in hits:
                    stamp = lv.get(hit["episode_id"])
                    if stamp is not None:  # absent ⇒ no key, never null
                        verified_ts, sha, state = stamp
                        hit["last_verified"] = {
                            "ts": verified_ts,
                            "sha": sha,
                            "state": state,
                        }
                        # server-side rider: a hit the ledger already knows is stale carries its
                        # remediation options on EVERY harness (advisory text, O7-safe — detection +
                        # guidance, never an autonomous retirement). STALE-ONLY: a current hit stays
                        # byte-inert. The edge's own point-verify verdict, when present, wins over it.
                        if state == "stale":
                            hit["remediation"] = REMEDIATION_NOTICE
            except Exception:  # noqa: BLE001 — side-channel only
                _log.warning(
                    "mcp.recall_last_verified_failed",
                    extra={
                        "event": "mcp.recall_last_verified_failed",
                        "trace_id": result.trace_id,
                    },
                    exc_info=True,
                )
        # an empty post-belt set is an ABSTAIN, never a confident-empty (never-hallucinate)
        abstained = (result.state != CONFIDENT) or (not hits)
        env: dict[str, Any] = {
            "reference_context": hits,
            "abstained": abstained,
            "trace_id": result.trace_id,
            "state": result.state,
            "top_cos": float(result.top_cos),
        }
        # conflict carrier: a DISTINCT envelope key referencing IDS ONLY (never text, never
        # reference_context — Law 4). The pure pipeline detects over the PRE-select servable
        # field, so a near-dup the decorrelated serve dropped is still surfaced. Byte-inert when
        # off (no key) AND when no near-dup pair is co-present (lean by default).
        if getattr(self.conflict, "enabled", False) and result.conflicts:
            env["conflicts"] = [_conflict_note_to_wire(n) for n in result.conflicts]
        if abstained:
            _log.info(
                "mcp.recall_abstain",
                extra={
                    "event": "mcp.recall_abstain",
                    "trace_id": result.trace_id,
                    "state": result.state,
                    "top_cos": float(result.top_cos),
                },
            )
            env["note"] = _abstain_note(result.state)
        return env

    def _handle_supersede(
        self, args: dict[str, Any], identity: ServerIdentity
    ) -> dict[str, Any]:
        """Retirement-with-replacement, agent-called but MACHINE-GATED (§3.2, no
        approver exists): the gate feeds + ``retirement_evidence`` run BEFORE any store
        mutation — unqualified ⇒ the exact §3.2 noop envelope (isError=false, nothing
        retired); qualified ⇒ ``store.supersede`` retires the loser and the audit
        stamps WHICH signal(s) qualified. Unknown-id / self-supersede keep today's
        distinct noop shape; a gate-feed reader fault fails CLOSED to noop."""
        raw_loser: Any = args.get("loser")  # required+integer (schema belt)
        loser = int(raw_loser)
        raw_winner: Any = args.get("winner")
        winner = int(raw_winner)
        loser_ep = self.store.get_episode(loser)
        winner_ep = self.store.get_episode(winner)
        if loser == winner or loser_ep is None or winner_ep is None:
            return {
                "status": "noop",
                "loser": loser,
                "winner": winner,
                "reason": "unknown id or self-supersede — nothing retired",
            }
        if loser_ep.trust == DEPRECATED and loser_ep.superseded_by == winner:
            # idempotent re-run: already retired in favor of THIS winner — echo the
            # success without a duplicate audit (mirrors store.supersede's guard).
            return {
                "status": "superseded",
                "loser": loser,
                "winner": winner,
                "signals": [],
            }
        elig = self._retirement_eligibility(
            loser_ep, identity.agent_id, winner=winner_ep
        )
        if not elig.eligible:  # §3.2: benign noop, never an error
            return {
                "status": "noop",
                "loser": loser,
                "winner": winner,
                "reason": GATE_NOOP_REASON,
                "signals": [],
            }
        ok = self.store.supersede(
            loser, winner, actor=identity.agent_id, ts=int(self.now())
        )
        if not ok:
            return {
                "status": "noop",
                "loser": loser,
                "winner": winner,
                "reason": "unknown id or self-supersede — nothing retired",
            }
        self._stamp_retirement_audit(
            loser, EK_SUPERSEDE, identity.agent_id, elig.signals
        )
        _log.info(
            "mcp.superseded",
            extra={
                "event": "mcp.superseded",
                "loser": loser,
                "winner": winner,
                "agent_id": identity.agent_id,
                "signals": list(elig.signals),
            },
        )
        return {
            "status": "superseded",
            "loser": loser,
            "winner": winner,
            "signals": list(elig.signals),
        }

    def _handle_prune(
        self, args: dict[str, Any], identity: ServerIdentity
    ) -> dict[str, Any]:
        """The bare-retirement verb (the SECOND retirement owner beside hive_supersede),
        agent-called but MACHINE-GATED (§3.2, no approver exists): the gate feeds +
        ``retirement_evidence`` run BEFORE any store mutation — unqualified ⇒ the exact
        §3.2 noop envelope (isError=false, nothing retired); qualified ⇒
        ``store.deprecate`` retires the target (no replacement) and the audit stamps
        WHICH signal(s) qualified. Unknown-id / already-retired keep today's distinct
        noop shape (never a double audit); a gate-feed reader fault fails CLOSED to
        noop."""
        raw_episode_id: Any = args.get("episode_id")  # required+integer (schema belt)
        episode_id = int(raw_episode_id)
        ep = self.store.get_episode(episode_id)
        if ep is None or ep.status != "approved" or ep.trust == DEPRECATED:
            return {
                "status": "noop",
                "episode_id": episode_id,
                "reason": "unknown id or already retired — nothing pruned",
            }
        elig = self._retirement_eligibility(ep, identity.agent_id)
        if not elig.eligible:  # §3.2: benign noop, never an error
            return {
                "status": "noop",
                "episode_id": episode_id,
                "reason": GATE_NOOP_REASON,
                "signals": [],
            }
        ok = self.store.deprecate(
            episode_id, actor=identity.agent_id, ts=int(self.now())
        )
        if not ok:
            return {
                "status": "noop",
                "episode_id": episode_id,
                "reason": "unknown id or already retired — nothing pruned",
            }
        self._stamp_retirement_audit(
            episode_id, EK_PRUNE, identity.agent_id, elig.signals
        )
        _log.info(
            "mcp.pruned",
            extra={
                "event": "mcp.pruned",
                "episode_id": episode_id,
                "agent_id": identity.agent_id,
                "signals": list(elig.signals),
            },
        )
        return {
            "status": "pruned",
            "episode_id": episode_id,
            "signals": list(elig.signals),
        }

    def _handle_outcome(
        self, args: dict[str, Any], identity: ServerIdentity
    ) -> dict[str, Any]:
        """The PURE evidence verb: an agent reports which recalled episode_ids materially HELPED
        or HURT its task. Records one ``outcome_helped`` / ``outcome_hurt`` evidence_events audit
        per KNOWN id (unknown ids are skipped — never a forged audit, never a raise), carrying
        ids + kinds ONLY, no memory text (Law 4). It holds NO establish/retire handle, so it
        structurally CANNOT mutate trust (O7-safe by construction, like ConflictFlagService): the
        evidence feeds the dormant martingale settled-win clause + a human worklist, never an
        action directly — hurt rows are §3.2 clause-2 gate feed. The reporter is the audit actor.
        Defensively coerces ids (a non-int item is skipped, fail-open side-channel)."""
        now = int(self.now())
        actor = identity.agent_id
        empty = json.dumps({})  # no memory text rides the audit (Law 4)

        def _record(raw: Any, kind: str) -> list[int]:
            recorded: list[int] = []
            for x in raw if isinstance(raw, list) else []:
                try:
                    eid = int(x)
                except (TypeError, ValueError):
                    continue  # skip a non-coercible id (fail-open)
                if self.store.get_episode(eid) is None:
                    continue  # skip an unknown id (never forge an audit)
                self.store.insert_audit(eid, kind, actor, now, empty)
                recorded.append(eid)
            return recorded

        helped = _record(args.get("helped"), EK_OUTCOME_HELPED)
        hurt = _record(args.get("hurt"), EK_OUTCOME_HURT)
        _log.info(
            "mcp.outcome",
            extra={
                "event": "mcp.outcome",
                "agent_id": actor,
                "n_helped": len(helped),
                "n_hurt": len(hurt),
            },
        )
        return {"status": "recorded", "helped": helped, "hurt": hurt}

    def _handle_flag(
        self, args: dict[str, Any], identity: ServerIdentity
    ) -> dict[str, Any]:
        """The advisory Layer-2 verb (gated by conflict.enabled). Records an UNTRUSTED
        conflict/supersedes marker via the ConflictFlagService — it NEVER retires (resolution
        stays the human hive_supersede). ``proposed_by`` is the transport identity (INV-2,
        never a tool field). A validation failure is a clean ``rejected`` envelope; a secret in
        the resolution is ``refused`` (0 rows)."""
        if self.flag_service is None:
            return {"status": "disabled"}
        raw_a: Any = args.get("a")  # required+integer (schema belt)
        a = int(raw_a)
        raw_b: Any = args.get("b")
        b = int(raw_b)
        winner = args.get("winner")
        try:
            res = self.flag_service.flag(
                kind=args.get("kind"),
                a_id=a,
                b_id=b,
                winner_id=(int(winner) if winner is not None else None),
                resolution=args.get("resolution") or "",
                proposed_by=identity.agent_id,
            )
        except SecretRefused as e:
            return {
                "status": "refused",
                "reason": str(e),
                "scan": {
                    "action": "refuse",
                    "rules": e.rules,
                    "n_findings": e.n_findings,
                },
            }
        except ValueError as e:
            return {"status": "rejected", "reason": str(e)}
        if res.status == "disabled":
            return {"status": "disabled"}
        return {
            "status": res.status,
            "kind": res.kind,
            "recorded": res.recorded,
            "a": a,
            "b": b,
        }

    def _handle_health(
        self, args: dict[str, Any], identity: ServerIdentity
    ) -> dict[str, Any]:
        try:
            n_episodes, n_pending = (
                self.store.counts()
            )  # probe: store + (below) embedder
            snap: dict[str, Any] = {
                "ok": True,
                "tenant_id": self.identity.tenant_id,
                "db_path": self.db_path,
                "db_size_bytes": self._db_size(),
                "n_episodes": n_episodes,
                "n_pending": n_pending,
                "embedder": str(getattr(self.embedder, "name", "unknown")),
                "embedder_loaded": bool(getattr(self.embedder, "loaded", True)),
                "d": int(getattr(self.embedder, "d", 0)),
                "index_authoritative": bool(self.recall.index.is_authoritative()),
                "uptime_s": max(0, int(self.now()) - self.started_ts),
            }
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
                    int(self.now()) - 7 * _DAY_S
                )
            except Exception:  # noqa: BLE001 — telemetry only
                _log.warning(
                    "mcp.health_trust_probe_failed",
                    extra={"event": "mcp.health_trust_probe_failed"},
                    exc_info=True,
                )
            hint = self._solo_hint()  # stalls self-describe
            if hint is not None:
                snap["solo_hint"] = hint
            if args.get("include_trends"):
                snap["trends"] = self._trends_report()  # CV4: the convergence KPI
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
            # the graph-propagated staleness worklist: same sole-request-flag gate, no
            # config knob; dropping the request flag ⇒ always-emits mutation.
            if args.get("include_stale_suspects"):
                snap["stale_suspects"] = self._stale_suspects_report()
            # the passive census-feed staleness signal: {"repos": per-repo blocks keyed
            # by registry name, "fleet": the tick shell's own state}. The fleet slot is
            # what makes a DEAD daemon visible — its fault belongs to no repo, and no
            # sentinel key inside the repo-keyed map could be reserved (the registry
            # slug gate would let a real repo collide with it). Same sole-request-flag
            # gate, no config knob; census_health_report self-wraps fail-open, so it is
            # called directly (no wrapper method); dropping the request flag ⇒
            # always-emits mutation.
            if args.get("include_census_health"):
                snap["census_health"] = census_health_report(self.store)
            # the corpus token-version histogram (the meta envelope law's no-migration
            # observability): same sole-request-flag gate, no config knob; the ONE sanctioned
            # server-side read of meta content is the version PREFIX, in domain meta.py —
            # dropping the request flag ⇒ always-emits mutation.
            if args.get("include_meta_versions"):
                snap["meta_versions"] = self.store.meta_version_counts()
            # v3: NO include_onboarding — the install-payload channel is deleted; the
            # usage contract is served fresh at every connect (initialize instructions).
            return snap
        except Exception as e:  # fail-closed subset ONLY
            _log.error(
                "mcp.health_probe_fail",
                extra={
                    "event": "mcp.health_probe_fail",
                    "error_type": type(e).__name__,
                    "db_path": self.db_path,
                },
                exc_info=True,
            )
            return {
                "ok": False,
                "error": f"{type(e).__name__}: {e}",
                "db_path": self.db_path,
            }

    # ── health helpers ────────────────────────────────────────────────────────
    def _solo_hint(self) -> Optional[str]:
        """When window demand is WASTING itself on ONE identity under the
        anti-gaming rule, promotion is silently inert — return the self-describing
        hint. None when autonomy is off, the store is quiet, ≥2 identities are
        present, or the probe faults (telemetry only, never breaks health). With
        the solo-mode span bypass removed, this is the ONLY feedback signal that
        turns a silent identity-collapse into a visible, actionable one.

        The identity-collapse clause below is what actually detects the condition;
        there is deliberately NO ``demand_m`` floor here. The real bar
        (``lifecycle``) counts misses from identities OTHER than the writer, which
        is not what a window-wide ``len(misses)`` measures — reading the same knob
        over a different quantity made this hint's own description false."""
        try:
            if not bool(getattr(self.autonomy, "enabled", True)):
                return None
            window_s = int(self.autonomy.demand_window_days) * _DAY_S
            misses = self.store.misses_window(int(self.now()) - window_s)
            if not misses:
                return None  # a quiet store has no demand to waste
            if len({m.agent_id for m in misses}) > 1:
                return None
            return (
                "single-identity traffic: demand-promotion is inert under the "
                "anti-gaming rule because your agents share one identity — "
                "register each agent via `hive connect` so every session carries "
                "a distinct X-Hive-Agent-Id (remote seats: one token each)."
            )
        except Exception:  # noqa: BLE001 — telemetry only
            _log.warning(
                "mcp.solo_hint_probe_failed",
                extra={"event": "mcp.solo_hint_probe_failed"},
                exc_info=True,
            )
            return None

    def _gap_report(self) -> list[dict[str, Any]]:
        """The clustered demand-gap report (deterministic, capped window, top-10).
        Window + cosine neighborhood mirror what the promotion rule sees, so a
        reported gap is exactly un-served demand. v3: each miss's REPO scope rides
        its row so every gap entry names the partitions that asked (``repos``).
        Degrades to [] on a probe fault."""
        try:
            window_s = int(self.autonomy.demand_window_days) * _DAY_S
            since = int(self.now()) - window_s
            rows = self.store.misses_detail_window(since)
            self._attach_miss_scope(rows, since)
            return cluster_misses(rows, tau=float(self.autonomy.demand_tau))
        except Exception:  # noqa: BLE001 — telemetry only
            _log.warning(
                "mcp.gap_report_failed",
                extra={"event": "mcp.gap_report_failed"},
                exc_info=True,
            )
            return []

    def _attach_miss_scope(self, rows: list[dict[str, Any]], since: int) -> None:
        """Zip each miss row's stored repo scope onto the ``misses_detail_window``
        feed. The store method predates the v3 ``repos`` column, so the scope is read
        in the SAME frame (``ts > since``, id order) and positionally zipped — a
        concurrent append only ever extends the tail, so the aligned prefix is stable.
        Its own fail-open leg: a scope-read fault leaves the rows scope-less and the
        gap report still serves."""
        try:
            raw = self.store.conn.execute(
                "SELECT repos FROM recall_misses WHERE ts>? ORDER BY id", (int(since),)
            ).fetchall()
            for row, extra in zip(rows, raw):
                row["repos"] = _parse_repos_json(extra["repos"])
        except Exception:  # noqa: BLE001 — telemetry only
            _log.warning(
                "mcp.miss_scope_attach_failed",
                extra={"event": "mcp.miss_scope_attach_failed"},
                exc_info=True,
            )

    def _trends_report(self) -> dict[str, Any]:
        """CV4: current-vs-previous 7d windows over existing tables — the
        demand-health KPI (confident_rate ↑, demand_entropy ↓). The ONLY window
        into silent fail-open rot (THEORY §8 tension #3). Composes the gaps clustering;
        degrades to {} on a fault."""
        try:
            tau = float(self.autonomy.demand_tau)
            return compute_trends(
                self.store,
                lambda rows: cluster_misses(rows, tau=tau),
                now=int(self.now()),
            )
        except Exception:  # noqa: BLE001 — telemetry only
            _log.warning(
                "mcp.trends_report_failed",
                extra={"event": "mcp.trends_report_failed"},
                exc_info=True,
            )
            return {}

    def _conflict_report(self) -> list[dict[str, Any]]:
        """The conflict/redundancy worklist (THEORY I1/I2 — detection, never resolution):
        mechanical near-dup detections over the SERVABLE set, v3-bucketed by (repo,
        anchor) BINDING — an episode joins one bucket per anchor binding, unanchored
        episodes share the ('', '') bucket, and the same near-dup pair split ACROSS
        repos never co-buckets (precision + bound — a cross-bucket near-dup still
        surfaces at recall, just not here). Every mechanical entry carries its bucket's
        ``repo`` + ``anchor``. MERGED with open advisory flags whose BOTH episodes are
        still servable (the servable-both filter is how a flag auto-clears the instant
        either side is retired — no status flip needed). Mechanical entries are
        globally capped at conflict.top_n by cosine. Degrades to [] on a probe fault
        (a side-channel, fail-open — never breaks health)."""
        try:
            now = int(self.now())
            ttl_s = int(self.autonomy.provisional_ttl_days) * _DAY_S
            labeled = self.store.scan_servable_labeled(now=now, provisional_ttl_s=ttl_s)
            servable_ids = {row[0] for row in labeled}
            # the (repo, anchor) binding map — servable_scopes shares the servability
            # decision with the scan, so the two reads can never disagree on the field.
            scopes = self.store.servable_scopes(now=now, provisional_ttl_s=ttl_s)
            buckets: dict[tuple[str, str], list[ConflictItem]] = {}
            for eid, value, polarity, _anchor, ts, trust in labeled:
                _repos, anchor_pairs = scopes.get(int(eid), (frozenset(), ()))
                for repo, anchor in anchor_pairs or (("", ""),):
                    buckets.setdefault((repo, anchor), []).append(
                        ConflictItem(
                            episode_id=eid,
                            vector=value,
                            polarity=polarity,
                            anchor=anchor,
                            ts=ts,
                            trust=trust,
                        )
                    )
            top_n = int(self.conflict.top_n)
            scored: list[tuple[float, dict[str, Any]]] = []
            for (repo, anchor), items in buckets.items():
                for note in detect_conflicts(
                    items, tau=float(self.conflict.tau), top_n=top_n
                ):
                    wire = _conflict_note_to_wire(note)
                    wire["repo"] = repo  # v3: the bucket rides the entry
                    wire["anchor"] = anchor
                    scored.append((note.cosine, wire))
            scored.sort(key=lambda t: -t[0])  # global cap across all buckets
            out = [wire for _c, wire in scored[:top_n]]
            # merge open advisory flags, filtered to both-episodes-still-servable
            for flag in self.store.open_conflict_flags():
                if flag["a_id"] in servable_ids and flag["b_id"] in servable_ids:
                    out.append(_flag_to_wire(flag))
            return out
        except Exception:  # noqa: BLE001 — telemetry only
            _log.warning(
                "mcp.conflict_report_failed",
                extra={"event": "mcp.conflict_report_failed"},
                exc_info=True,
            )
            return []

    def _suspect_consensus_report(self) -> list[dict[str, Any]]:
        """The suspect-consensus worklist (THEORY §10 O7 — detection, never resolution):
        PROVISIONAL promotions whose stamped demand had thin EFFECTIVE INDEPENDENCE
        (``n_eff / k`` below the floor) — the confidently-wrong-but-popular failure mode. Scans
        the SERVABLE set, keeps the provisional (demand-promoted, not human-vouched) ids, reads
        each one's newest ``promote`` audit ``demand_independence`` stamp, and runs the pure
        detector. Ids-only wire dicts (no memory text, Law 4); a provisional with no stamp
        (pre-3b promotion, or never demand-promoted) under-claims (omitted).

        ``has_settled_win`` is SOURCED from ``store.settled_wins`` — the UNION of
        ``outcome_helped`` (self-reported via hive_outcome) and ``outcome_verified_helped``
        (SHA-bound census corroboration). The martingale clause sharpens: a thin provisional
        WITH a settled win drops to ``martingale_warning=False`` (popular-but-corroborated),
        one WITHOUT stays True (popular-but-uncorroborated). NOTE the self-report half is
        TRUSTED-SOLO: in a hostile fleet a poisoner could self-report ``helped`` on its
        poison to suppress its own flag — moot in the operator's trusted-solo stance, and
        the worklist is detection-only (no auto-action); the verified half is non-forgeable
        by the memory's writer (evidence_events is server-written only).

        Degrades to [] on any probe fault (a side-channel must never break health, fail-open)."""
        try:
            now = int(self.now())
            ttl_s = int(self.autonomy.provisional_ttl_days) * _DAY_S
            labeled = self.store.scan_servable_labeled(now=now, provisional_ttl_s=ttl_s)
            # (id, value, polarity, anchor, ts, trust) — keep provisional (demand-promoted) only
            anchor_by_id = {
                row[0]: row[3] for row in labeled if row[5] == "provisional"
            }
            prov = self.store.promotion_provenance(list(anchor_by_id))
            settled = self.store.settled_wins(
                list(prov)
            )  # ids with >= 1 self-reported helped
            items = [
                SuspectConsensusItem(
                    episode_id=eid,
                    rho_bar=rho_bar,
                    n_eff=n_eff,
                    k=k,
                    has_settled_win=(eid in settled),
                    anchor=anchor_by_id.get(eid, ""),
                )
                for eid, (rho_bar, n_eff, k) in prov.items()
            ]
            notes = detect_suspect_consensus(
                items,
                n_eff_frac_max=float(self.suspect_consensus.n_eff_frac_max),
                top_n=int(self.suspect_consensus.top_n),
            )
            return [
                {
                    "episode_id": n.episode_id,
                    "rho_bar": round(float(n.rho_bar), 4),
                    "n_eff": round(float(n.n_eff), 4),
                    "k": n.k,
                    "martingale_warning": n.martingale_warning,
                    "note": (
                        "provisional promoted on thin effective-independence demand "
                        "(correlated asks) — re-examine; resolve via hive_supersede "
                        "(replace) or hive_prune (retire if wrong)"
                    ),
                }
                for n in notes
            ]
        except Exception:  # noqa: BLE001 — telemetry only
            _log.warning(
                "mcp.suspect_consensus_report_failed",
                extra={"event": "mcp.suspect_consensus_report_failed"},
                exc_info=True,
            )
            return []

    def _stale_suspects_report(self) -> list[dict[str, Any]]:
        """The graph-propagated staleness worklist (detection, never resolution): per
        SERVABLE episode, the newest ``stale_suspect`` ledger row — written by the census
        ingest when the episode's anchor sat in a breaking/removed change's blast radius
        (the receipt's ``--propagate`` block). Ids/enums/hashes only (Law 4): episode_id +
        its anchor + the seed subject, drift, head SHA and row ts; anchor-bucketed order
        (sorted by anchor, then id). A non-servable episode is DROPPED (retired/decayed
        suspicion is dead worklist noise); a malformed payload is OMITTED (under-claim).
        Resolution stays human — re-verify the anchor, then hive_supersede / hive_prune.
        Degrades to [] on any probe fault (a side-channel must never break health)."""
        try:
            now = int(self.now())
            ttl_s = int(self.autonomy.provisional_ttl_days) * _DAY_S
            anchor_by_id = {
                row[0]: row[3]
                for row in self.store.scan_servable_labeled(
                    now=now, provisional_ttl_s=ttl_s
                )
            }
            out: list[dict[str, Any]] = []
            for eid, ts, payload in self.store.stale_suspect_rows():
                if eid not in anchor_by_id:
                    continue  # non-servable: dropped
                try:
                    doc = json.loads(payload)
                except ValueError:
                    continue  # malformed: under-claim
                if not isinstance(doc, dict):
                    continue
                seed, drift = doc.get("seed"), doc.get("drift")
                stamp = doc.get("stamp")
                head_sha = stamp.get("head_sha") if isinstance(stamp, dict) else None
                if not all(isinstance(v, str) and v for v in (seed, drift, head_sha)):
                    continue  # half-shaped: under-claim
                out.append(
                    {
                        "episode_id": eid,
                        "anchor": anchor_by_id[eid],
                        "seed": seed,
                        "drift": drift,
                        "head_sha": head_sha,
                        "ts": int(ts),
                    }
                )
            out.sort(key=lambda r: (r["anchor"], r["episode_id"]))
            return out
        except Exception:  # noqa: BLE001 — telemetry only
            _log.warning(
                "mcp.stale_suspects_report_failed",
                extra={"event": "mcp.stale_suspects_report_failed"},
                exc_info=True,
            )
            return []

    def _db_size(self) -> int:
        try:
            return (
                os.path.getsize(self.db_path)
                if self.db_path and os.path.exists(self.db_path)
                else 0
            )
        except OSError:
            return 0


# ── stdio loop (PORT verbatim; stderr-clean so stdout JSON-RPC is unpolluted) ──


def run_stdio(server: HiveMCPServer, in_stream: TextIO, out_stream: TextIO) -> None:
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
            _log.warning(
                "mcp.parse_error",
                extra={"event": "mcp.parse_error", "line_len": len(line)},
            )
            out_stream.write(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32700, "message": f"parse error: {e}"},
                    }
                )
                + "\n"
            )
            out_stream.flush()
            continue
        if not isinstance(payload, dict):  # batch array / bare scalar
            _log.warning(
                "mcp.invalid_request",
                extra={
                    "event": "mcp.invalid_request",
                    "payload_type": type(payload).__name__,
                },
            )
            out_stream.write(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {
                            "code": -32600,
                            "message": "invalid request: expected a JSON object",
                        },
                    }
                )
                + "\n"
            )
            out_stream.flush()
            continue
        if "id" not in payload:  # notification → no response
            continue
        raw_params = payload.get("params")
        params = (
            raw_params if isinstance(raw_params, dict) else {}
        )  # never let .get crash
        req = MCPRequest(
            id=payload.get("id"), method=payload.get("method", ""), params=params
        )
        try:
            resp = server.handle(req)
        except Exception as e:  # last-resort guard: loop survives
            _log.error(
                "mcp.handle_crash",
                extra={"event": "mcp.handle_crash", "error_type": type(e).__name__},
                exc_info=True,
            )
            resp = MCPResponse(
                id=req.id, error=_err(-32603, f"internal error: {type(e).__name__}")
            )
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
    except Exception as exc:  # noqa: BLE001 — logging setup never aborts boot
        _log.warning("mcp_server.log_config_failed kind=%s", type(exc).__name__)


def _resolve_identity(
    tenant: Optional[str],
    db: Optional[str],
    agent: Optional[str],
    env: Mapping[str, str],
) -> tuple[str, str, str]:
    """Resolve ``(tenant_id, db_path, agent_id)`` — CLI arg takes precedence, then env, then a
    safe default for all three. ``tenant_id`` is a constant label (never a query filter — the
    single-tenant boundary), defaulting to ``"default"`` when unset so the server boots
    zero-config. NEVER echoes an env *value* (secret-safe). // O(1)."""
    tenant_id = (
        tenant or env.get("HIVE_TENANT_ID") or ""
    ).strip() or _DEFAULT_TENANT_ID
    db_path = (db or env.get("HIVE_STORE__DB_PATH") or "").strip() or _DEFAULT_DB_PATH
    agent_id = (agent or env.get("HIVE_AGENT_ID") or "").strip() or _DEFAULT_AGENT_ID
    return tenant_id, db_path, agent_id


def main(
    argv: Optional[list[str]] = None,
    *,
    env: Optional[Mapping[str, str]] = None,
    build_container_fn: Optional[Callable[..., Any]] = None,
    serve: Optional[Callable[[Any], None]] = None,
) -> int:
    """Boot a PER-SESSION ``HiveMCPServer`` and serve it on stdio; return a sysexits exit code.

    Boot order MIRRORS the container entrypoint MINUS the readiness markers:
        config → build_container → migrate → build_index → warm_embedder → make_server → serve

    ``build_container_fn`` (default ``hive.app.container.build_container``, imported LAZILY to
    dodge the ``container → mcp_server`` import cycle) and ``serve`` (default ``run_stdio`` on
    the real std streams) are injection seams so the order, the fail-fast exit codes, and the
    marker-INERT policy are unit-testable with no torch and no real stdio. Never raises out of
    the boot path — every failure is logged and converted to an exit code so stdout (the
    JSON-RPC channel) stays clean. // O(1) control flow."""
    import argparse  # stdlib; lazy keeps the module-import surface light

    env = os.environ if env is None else env
    parser = argparse.ArgumentParser(
        prog="hive.app.mcp_server",
        description="Per-session Hive MCP server (stdio), exec'd inside the warm container.",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="shared SQLite store path (default: $HIVE_STORE__DB_PATH or /data/shared.db)",
    )
    parser.add_argument(
        "--tenant",
        default=None,
        help="tenant id label (default: $HIVE_TENANT_ID or 'default')",
    )
    parser.add_argument(
        "--agent",
        default=None,
        help="agent id, stamped as proposed_by (default: $HIVE_AGENT_ID or default-agent)",
    )
    args = parser.parse_args(argv)

    _configure_logging()  # JSON→stderr before anything can fail

    # ── config.loaded (EX_CONFIG on Config validation failure) ──
    tenant_id, db_path, agent_id = _resolve_identity(
        args.tenant, args.db, args.agent, env
    )

    # Lazy imports: ``container`` imports THIS module (HiveMCPServer/ServerIdentity), so a
    # module-level ``from hive.app.container import build_container`` would be a circular import.
    try:
        from hive.app.config import Config  # noqa: PLC0415 — lazy
    except Exception as exc:  # noqa: BLE001
        _log.error(
            "mcp_server.config_import_failed kind=%s code=%d",
            type(exc).__name__,
            EX_SOFTWARE,
        )
        return EX_SOFTWARE
    try:
        cfg = Config.load(db_path=db_path, env=env, runtime={"tenant_id": tenant_id})
    except Exception as exc:  # noqa: BLE001 — bad config is EX_CONFIG
        _log.error(
            "mcp_server.config_invalid kind=%s code=%d", type(exc).__name__, EX_CONFIG
        )
        return EX_CONFIG
    _configure_logging(
        int(getattr(cfg.obs, "log_level", logging.INFO))
    )  # operator level now live
    _log.info("mcp_server.config_loaded tenant_id=%s db_path=%s", tenant_id, db_path)

    if build_container_fn is None:
        try:
            from hive.app.container import build_container  # noqa: PLC0415 — lazy (breaks the cycle)
        except Exception as exc:  # noqa: BLE001
            _log.error(
                "mcp_server.container_import_failed kind=%s code=%d",
                type(exc).__name__,
                EX_SOFTWARE,
            )
            return EX_SOFTWARE
        build_container_fn = build_container
    try:
        container = build_container_fn(cfg, tenant_id=tenant_id, agent_id=agent_id)
    except Exception as exc:  # noqa: BLE001 — assembler failure
        _log.error(
            "mcp_server.assemble_failed kind=%s code=%d",
            type(exc).__name__,
            EX_SOFTWARE,
        )
        return EX_SOFTWARE

    # ── boot the per-session server. NO _invalidate_ready / _mark_ready around this sequence —
    #    see the banner above. The boot readiness markers belong to PID 1 (the ENTRYPOINT). ──
    try:
        container.migrate()  # idempotent verify vs the already-migrated shared DB
    except Exception as exc:  # noqa: BLE001
        _log.error(
            "mcp_server.migrate_failed kind=%s code=%d", type(exc).__name__, EX_SOFTWARE
        )
        return EX_SOFTWARE
    try:
        container.build_index()  # THIS process's own in-RAM index, from approved rows
    except Exception as exc:  # noqa: BLE001
        _log.error(
            "mcp_server.index_failed kind=%s code=%d", type(exc).__name__, EX_SOFTWARE
        )
        return EX_SOFTWARE
    try:
        embedder = container.warm_embedder()  # THIS process's own embedder, warmed once
    except Exception as exc:  # noqa: BLE001 — dead embedder
        _log.error(
            "mcp_server.embedder_warm_failed kind=%s code=%d",
            type(exc).__name__,
            EX_UNAVAILABLE,
        )
        return EX_UNAVAILABLE
    if not bool(
        getattr(embedder, "loaded", False)
    ):  # healthy ≡ resident — a cold embedder cannot serve
        _log.error("mcp_server.embedder_not_resident code=%d", EX_UNAVAILABLE)
        return EX_UNAVAILABLE
    try:
        server = container.make_server()
    except Exception as exc:  # noqa: BLE001
        _log.error(
            "mcp_server.make_server_failed kind=%s code=%d",
            type(exc).__name__,
            EX_SOFTWARE,
        )
        return EX_SOFTWARE

    _log.info(
        "mcp_server.serving tenant_id=%s db_path=%s agent_id=%s (per-session; markers untouched)",
        tenant_id,
        db_path,
        agent_id,
    )
    if serve is None:

        def serve(s: Any) -> None:  # default seam: blocking real-stdio serve
            run_stdio(s, sys.stdin, sys.stdout)

    serve(server)
    return EX_OK


if (
    __name__ == "__main__"
):  # pragma: no cover — module entry (``python -m hive.app.mcp_server``)
    raise SystemExit(main(sys.argv[1:]))
