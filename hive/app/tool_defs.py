"""TOOL_DEFINITIONS — the static ``tools/list`` schema table for the MCP surface.
EXACTLY 8 tools: hive_write / hive_capture / hive_recall / hive_supersede / hive_prune /
hive_flag / hive_outcome / hive_health. The onboarding
handshake (``hive_init``) is gone — the contract reaches every agent via the always-on
``initialize`` instructions (the served FLOOR), with a secondary reference carried in the
``hive_health`` description. On top of that floor an OPTIONAL, versioned rules block MAY be
installed as an enhancement layer (``onboard_ref.AGENT_RULES_BLOCK`` — version-stamped by
``CONTRACT_VERSION``, beaconed on every tool result, self-healing on drift); the floor itself still
installs nothing, and a missing/stale block degrades safely to it. ``hive_evidence`` deliberately does NOT exist (no
client-fed evidence in this build); the server-side approval QUEUE (hive_pending /
hive_approve / hive_reject) was removed with the move to client-gated capture; and the
AgentCortex-era consolidate / schemas / recall_cold / restore_cold / reconsolidate / audit
verbs are absent by construction.

A pure module constant — no runtime state. The same table is the source of truth
for (a) the ``tools/list`` reply and (b) the pre-dispatch schema-validation belt in
mcp_server (``required[]`` / ``type`` / ``enum`` are read straight off it), so the
advertised contract and the enforced contract cannot diverge.
"""
from __future__ import annotations

from hive.app.onboard_ref import BAD_VS_STALE, ONBOARDING_REFERENCE, WRITE_VS_CAPTURE
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

# The eight hive_* verbs. Frozen via the module boundary; handlers read args
# permissively (.get) so the ONLY required-field guard is _validate over this table.
TOOL_DEFINITIONS: list[dict] = [
    {"name": "hive_write",
     "description": "Save an APPROVED, immediately-recallable memory — the fast-path for "
                    "crucial, high-confidence facts that shouldn't wait for demand to promote "
                    "them (prefer hive_capture for everything else). Requires an approver's "
                    "explicit yes, passed as approved_by: your human (an in-chat yes) or, in an "
                    "orchestrated fleet, the orchestrator's sign-off — UNLESS AGI_MODE is on, where "
                    "an agent self-authorizes with approved_by=\"AGI_OVERRIDE\". The server scans for "
                    "secrets, then stores it in one call (no separate approval step). Recall the "
                    "topic first so you don't write a duplicate of — or a rival to — an existing "
                    "memory. Pass "
                    "replaces=<episode_id> when this CORRECTS an existing memory: the target is "
                    "retired immediately in favor of this one. Set polarity='dont' when the memory "
                    "is a PROHIBITION (don't do X) and 'do' for a prescription; it rides every "
                    "recall hit so the rule is never read as its opposite (default 'neutral')."
                    + _WRITE_GUIDANCE + " " + WRITE_VS_CAPTURE,
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
     "description": "Capture an insight WITHOUT asking — anything durable you believe useful and "
                    "can ground in VERIFIABLE EVIDENCE you observed (a bug you hit, a behavior you "
                    "saw, a decision you made — not speculation). It lands quarantined — stored and "
                    "embedded but NOT served — until measured demand from other agents "
                    "promotes it. Use for durable insights: bugs+fixes, dead-ends, "
                    "decisions, gotchas. No approver, no replaces — it cannot retire "
                    "anything. Set polarity='dont' for a PROHIBITION (don't do X) or 'do' for a "
                    "prescription so the recalled rule is never read as its opposite (default "
                    "'neutral')."
                    + _WRITE_GUIDANCE + " " + WRITE_VS_CAPTURE,
     "inputSchema": {"type": "object", "required": ["text"],
                     "properties": {"text": {"type": "string"},
                                    "polarity": {"type": "string",
                                                 "enum": ["do", "dont", "neutral"]},
                                    "kind": {"type": "string", "enum": _KIND_ENUM},
                                    "anchor": {"type": "string"}}}},
    {"name": "hive_recall",
     "description": "Retrieve servable memories. Returns {reference_context:[hits], "
                    "abstained, state, top_cos}; on abstain reference_context is [] "
                    "and abstained is true. Each hit carries trust ('established' = "
                    "approver-vouched, 'provisional' = demand-promoted, unverified), ts, "
                    "polarity (do|dont|neutral), kind (its category), and anchor (the WHERE "
                    "— file/module/symbol); prefer higher-trust, newer versions. "
                    + QUERY_GUIDANCE,
     "inputSchema": {"type": "object", "required": ["query"],
                     "properties": {"query": {"type": "string"}}}},
    {"name": "hive_supersede",
     "description": "Retire one existing memory in favor of another (human-vouched). The "
                    "loser is deprecated immediately; nothing new is written. Use for a STALE memory "
                    "(one that has a successor — the current truth) or a redundancy / supersession "
                    "candidate surfaced by "
                    "hive_health(include_conflicts=true). For a NEW corrected memory that "
                    "retires an old one, use hive_write(replaces=) instead. approved_by is the "
                    "approver (your human's in-chat yes, or an orchestrator's sign-off).",
     "inputSchema": {"type": "object", "required": ["loser", "winner", "approved_by"],
                     "properties": {"loser": {"type": "integer"},
                                    "winner": {"type": "integer"},
                                    "approved_by": {"type": "string"}}}},
    {"name": "hive_prune",
     "description": "Retire a memory that is INCORRECT, MALICIOUS, or MISLEADING — it is "
                    "deprecated (never recalled again) with no replacement and stays in the "
                    "audit ledger. NOT for neutral or merely off-topic memories (omit those; "
                    "leave them as-is). Needs an approver (approved_by: your human's in-chat "
                    "yes, or an orchestrator's sign-off); under AGI_MODE an agent may "
                    "self-authorize with approved_by='AGI_OVERRIDE'. To CORRECT a memory "
                    "(retire-and-replace), prefer hive_write(replaces=) instead. Resolves a "
                    "hurt candidate surfaced by hive_outcome or a review worklist. "
                    + BAD_VS_STALE,
     "inputSchema": {"type": "object", "required": ["episode_id", "approved_by"],
                     "properties": {"episode_id": {"type": "integer"},
                                    "approved_by": {"type": "string"}}}},
    {"name": "hive_flag",
     "description": "Advisory ONLY: flag two recalled memories as in conflict or one "
                    "superseding the other. It NEVER retires anything — it records a note "
                    "surfaced in hive_health(include_conflicts=true) for a human to resolve "
                    "(via hive_supersede). kind='conflict' (they disagree, winner unknown) or "
                    "kind='supersedes' (winner names the surviving memory). Use for the "
                    "semantic cases the automatic near-dup scan can't reach (different wording "
                    "or location). Pass resolution to record your rationale (it is "
                    "secret-scanned).",
     "inputSchema": {"type": "object", "required": ["a", "b", "kind"],
                     "properties": {"a": {"type": "integer"}, "b": {"type": "integer"},
                                    "kind": {"type": "string",
                                             "enum": ["conflict", "supersedes"]},
                                    "winner": {"type": "integer"},
                                    "resolution": {"type": "string"}}}},
    {"name": "hive_outcome",
     "description": "After a task, log which recalled memories materially HELPED or HURT your "
                    "work, by their episode_id from reference_context. Omit neutral or "
                    "contextually-irrelevant ones. Records EVIDENCE only — it changes no trust "
                    "and retires nothing. Helped memories become establish candidates and "
                    "corroborate effective-independence; hurt memories become prune/supersede "
                    "candidates for human review (or autonomous action under AGI_MODE).",
     "inputSchema": {"type": "object", "required": [],
                     "properties": {"helped": {"type": "array",
                                               "items": {"type": "integer"}},
                                    "hurt": {"type": "array",
                                             "items": {"type": "integer"}}}}},
    {"name": "hive_health",
     "description": "Cheap liveness/identity snapshot (+ trust_counts, n_misses_7d). "
                    "Fail-closed {ok:false,error,db_path} on a probe failure. "
                    "include_gaps=true adds the clustered demand-gap report. "
                    "include_trends=true adds current-vs-previous 7d demand-health "
                    "trends (confident rate + demand entropy). "
                    "include_conflicts=true adds the conflict/redundancy worklist "
                    "(near-dup contradictions + supersession candidates + agent-flagged "
                    "advisories) to resolve via hive_supersede. "
                    "include_suspect_consensus=true adds the suspect-consensus worklist: "
                    "provisional promotions whose demand had thin effective independence "
                    "(promoted on correlated asks, the confidently-wrong-but-popular case) "
                    "for a human to re-examine. "
                    "embedder_loaded reports whether this process's embedder is resident "
                    "(the container HEALTHCHECK is a separate process reading boot markers)."
                    "\n\n" + ONBOARDING_REFERENCE,
     "inputSchema": {"type": "object", "required": [],
                     "properties": {"include_gaps": {"type": "boolean"},
                                    "include_trends": {"type": "boolean"},
                                    "include_conflicts": {"type": "boolean"},
                                    "include_suspect_consensus": {"type": "boolean"}}}},
]

# The canonical tool name set — the dropped-verb guard reads this.
TOOL_NAMES: frozenset[str] = frozenset(t["name"] for t in TOOL_DEFINITIONS)
