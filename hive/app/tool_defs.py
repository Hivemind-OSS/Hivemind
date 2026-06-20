"""TOOL_DEFINITIONS — the static ``tools/list`` schema table for the MCP surface.
EXACTLY 4 tools: hive_write / hive_capture / hive_recall / hive_health. The onboarding
handshake (``hive_init``) is gone — onboarding is a STATIC reference carried in the
``hive_health`` description (a connected agent self-installs the rules block), not a
server-driven InstallPlanner step. ``hive_evidence`` deliberately does NOT exist (no
client-fed evidence in this build); the server-side approval QUEUE (hive_pending /
hive_approve / hive_reject) was removed with the move to client-gated capture; and the
AgentCortex-era 7 (consolidate/schemas/recall_cold/restore_cold/reconsolidate/audit/
outcome) are absent by construction.

A pure module constant — no runtime state. The same table is the source of truth
for (a) the ``tools/list`` reply and (b) the pre-dispatch schema-validation belt in
mcp_server (``required[]`` / ``type`` / ``enum`` are read straight off it), so the
advertised contract and the enforced contract cannot diverge.
"""
from __future__ import annotations

from hive.app.onboard_ref import ONBOARDING_REFERENCE
from hive.domain.kinds import KINDS, KIND_NAMES, QUERY_GUIDANCE

# Compact call-adjacent guidance, PROJECTED from the registry so the advertised glosses
# and enum cannot drift from the served/stored vocabulary. The FULL per-kind body
# templates are deliberately NOT here — they live once in SERVER_INSTRUCTIONS
# (render_taxonomy); this stays lean because it is resident the whole session.
_KIND_ENUM: list[str] = sorted(KIND_NAMES)            # the SOFT-validated enum the belt reads
_KIND_GLOSSES = "; ".join(f"{name}={spec['gloss']}" for name, spec in KINDS.items())
_WRITE_GUIDANCE = (
    " Pick the kind that fits (it rides every recall hit, is never embedded, and is NOT a "
    "recall filter): " + _KIND_GLOSSES + ". Set anchor to the WHERE (file/module/symbol). "
    "Write one dense, self-contained fact — don't pad or restate the obvious; verbosity "
    "flattens the embedding and makes recall abstain.")

# The four hive_* verbs. Frozen via the module boundary; handlers read args
# permissively (.get) so the ONLY required-field guard is _validate over this table.
TOOL_DEFINITIONS: list[dict] = [
    {"name": "hive_write",
     "description": "Save an APPROVED, immediately-recallable memory — the fast-path for "
                    "crucial, high-confidence facts that shouldn't wait for demand to promote "
                    "them (prefer hive_capture for everything else). Requires an approver's "
                    "explicit yes, passed as approved_by: your human (an in-chat yes) or, in an "
                    "orchestrated fleet, the orchestrator's sign-off. The server scans for "
                    "secrets, then stores it in one call (no separate approval step). Pass "
                    "replaces=<episode_id> when this CORRECTS an existing memory: the target is "
                    "retired immediately in favor of this one. Set polarity='dont' when the memory "
                    "is a PROHIBITION (don't do X) and 'do' for a prescription; it rides every "
                    "recall hit so the rule is never read as its opposite (default 'neutral')."
                    + _WRITE_GUIDANCE,
     # NOTE: no ``proposed_by`` property — the caller cannot assert an identity (INV-2);
     # ``proposed_by`` is always the authenticated label, threaded via handle(identity=…).
     "inputSchema": {"type": "object", "required": ["text", "approved_by"],
                     "properties": {"text": {"type": "string"},
                                    "approved_by": {"type": "string"},
                                    "replaces": {"type": "integer"},
                                    "polarity": {"type": "string",
                                                 "enum": ["do", "dont", "neutral"]},
                                    "kind": {"type": "string", "enum": _KIND_ENUM},
                                    "anchor": {"type": "string"}}}},
    {"name": "hive_capture",
     "description": "Capture an insight WITHOUT asking. It lands quarantined — stored and "
                    "embedded but NOT served — until measured demand from other agents "
                    "promotes it. Use for durable insights: bugs+fixes, dead-ends, "
                    "decisions, gotchas. No approver, no replaces — it cannot retire "
                    "anything. Set polarity='dont' for a PROHIBITION (don't do X) or 'do' for a "
                    "prescription so the recalled rule is never read as its opposite (default "
                    "'neutral')."
                    + _WRITE_GUIDANCE,
     "inputSchema": {"type": "object", "required": ["text"],
                     "properties": {"text": {"type": "string"},
                                    "polarity": {"type": "string",
                                                 "enum": ["do", "dont", "neutral"]},
                                    "kind": {"type": "string", "enum": _KIND_ENUM},
                                    "anchor": {"type": "string"}}}},
    {"name": "hive_recall",
     "description": "Retrieve servable memories. Returns {reference_context:[hits], "
                    "abstained, state, entropy_norm}; on abstain reference_context is [] "
                    "and abstained is true. Each hit carries trust ('established' = "
                    "approver-vouched, 'provisional' = demand-promoted, unverified), ts, "
                    "polarity (do|dont|neutral), kind (its category), and anchor (the WHERE "
                    "— file/module/symbol); prefer higher-trust, newer versions. "
                    + QUERY_GUIDANCE,
     "inputSchema": {"type": "object", "required": ["query"],
                     "properties": {"query": {"type": "string"}}}},
    {"name": "hive_health",
     "description": "Cheap liveness/identity snapshot (+ trust_counts, n_misses_7d). "
                    "Fail-closed {ok:false,error,db_path} on a probe failure. "
                    "include_gaps=true adds the clustered demand-gap report. "
                    "include_trends=true adds current-vs-previous 7d demand-health "
                    "trends (confident rate + demand entropy). "
                    "embedder_loaded reports whether this process's embedder is resident "
                    "(the container HEALTHCHECK is a separate process reading boot markers)."
                    "\n\n" + ONBOARDING_REFERENCE,
     "inputSchema": {"type": "object", "required": [],
                     "properties": {"include_gaps": {"type": "boolean"},
                                    "include_trends": {"type": "boolean"}}}},
]

# The canonical net-4 name set — the dropped-verb guard reads this.
TOOL_NAMES: frozenset[str] = frozenset(t["name"] for t in TOOL_DEFINITIONS)
