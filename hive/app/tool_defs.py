"""TOOL_DEFINITIONS — the static ``tools/list`` schema table for the MCP surface.

EXACTLY 8 tools: hive_write / hive_capture / hive_recall / hive_supersede / hive_prune /
hive_flag / hive_outcome / hive_health. The v3 surface (U4-THIN-AGENT): no approver field
exists anywhere — the write side is serve-as-provisional-then-heal; retirement verbs are
ids-only calls the SERVER machine-gates (an unqualified target is a benign no-op, never an
error); anchors are structured ``(repo, anchor)`` rows against the operational repo
registry; recall takes a repo scope (``repos`` + ``anchor_prefix``). The onboarding
apparatus is gone: no ``hive_init``, no install payload, no ``include_onboarding`` flag —
the usage contract reaches every agent via the always-on ``initialize`` instructions
(``hive.app.contract.SERVER_INSTRUCTIONS``), fresh at every connect.

A pure module constant — no runtime state. The same table is the source of truth for
(a) the ``tools/list`` reply and (b) the pre-dispatch schema-validation belt in mcp_server
(``required[]`` / ``type`` / ``enum`` / array ``items`` are read straight off it), so the
advertised contract and the enforced contract cannot diverge.
"""

from __future__ import annotations

from typing import Any

from hive.app.contract import BAD_VS_STALE, WRITE_VS_CAPTURE
from hive.domain.kinds import KINDS, KIND_NAMES, QUERY_GUIDANCE

# Compact call-adjacent guidance, PROJECTED from the registry so the advertised glosses
# and enum cannot drift from the served/stored vocabulary. The kind vocabulary detail
# stays lean because these descriptions are resident the whole session.
_KIND_ENUM: list[str] = sorted(KIND_NAMES)  # the SOFT-validated enum the belt reads
_KIND_GLOSSES = "; ".join(f"{name}={spec['gloss']}" for name, spec in KINDS.items())
_WRITE_GUIDANCE = (
    " Pick the kind that fits (it rides every recall hit, is never embedded, and is NOT a "
    "recall filter): " + _KIND_GLOSSES + ". Bind the WHERE as anchors=[{repo, anchor}] "
    "(registered repo name + path/file.py::symbol), or repos=[...] for repo scope without a "
    "code anchor; neither = a general, fleet-wide memory. Write one dense, self-contained "
    "fact; verbosity flattens the embedding."
)

# The optional opaque metadata map on the two write-side verbs — one shared spec so the
# advertised grammar cannot fork between them. Tool-agnostic by design: it names no producer.
_META_PROPERTY: dict[str, Any] = {
    "type": "object",
    "additionalProperties": {"type": "string"},
    "description": "Optional machine-metadata map, carried on the memory and served back "
    "verbatim on recall hits — never interpreted by the server, never "
    "embedded. Namespace each key by its producing tool as 'tool/attr' "
    "(lowercase); values are compact string handles, not payloads.",
}

# The structured code-binding grammar, ONE spec shared by hive_write and hive_capture so the
# advertised anchor grammar cannot fork. The belt validates each item as an object with
# exactly {repo, anchor}; the boundary additionally checks repo against the registry
# (an unregistered name is a clean refusal, nothing stored). The server mints fingerprints
# per anchor and judges drift server-side — callers never supply fingerprint material.
_ANCHORS_PROPERTY: dict[str, Any] = {
    "type": "array",
    "items": {
        "type": "object",
        "required": ["repo", "anchor"],
        "properties": {"repo": {"type": "string"}, "anchor": {"type": "string"}},
        "additionalProperties": False,
    },
    "description": "Code bindings: [{repo, anchor}] — repo is a REGISTERED repo name, "
    "anchor the WHERE (path/file.py::symbol, or a bare path). The server mints fingerprints "
    "and judges drift per anchor; an unregistered repo refuses the call.",
}

# Repo-scope membership without a code anchor (write/capture): registered names only.
_REPOS_PROPERTY: dict[str, Any] = {
    "type": "array",
    "items": {"type": "string"},
    "description": "Repo scope without a code anchor: registered repo names. Omitted with "
    "no anchors = a general (fleet-wide) memory.",
}

# The eight hive_* verbs. Frozen via the module boundary; handlers read args
# permissively (.get) so the ONLY required-field guard is _validate over this table.
TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "hive_write",
        "description": "Save a memory that SERVES NOW: it lands trust=provisional, recallable "
        "immediately, and is healed afterward by the server's outcome/drift "
        "machinery (established = outcome-verified on the canonical line). "
        "Recall the topic first so you don't write a duplicate of an existing "
        "memory. Pass replaces=<episode_id> when this CORRECTS "
        "an existing memory: an unknown target fails the whole call; a known "
        "target is retired ONLY when the server verifies a qualifying machine "
        "signal for it, else the write still lands and the envelope reports "
        "supersede_noop (the old row stays). Set polarity='dont' when the memory "
        "is a PROHIBITION (don't do X) and 'do' for a prescription; it rides "
        "every recall hit so the rule is never read as its opposite (default "
        "'neutral')." + _WRITE_GUIDANCE + " " + WRITE_VS_CAPTURE,
        # NOTE: no ``proposed_by`` property — the caller cannot assert an identity (INV-2);
        # ``proposed_by`` is always the authenticated label, threaded via handle(identity=…).
        "inputSchema": {
            "type": "object",
            "required": ["text"],
            "properties": {
                "text": {"type": "string"},
                "replaces": {"type": "integer"},
                "polarity": {"type": "string", "enum": ["do", "dont", "neutral"]},
                "kind": {"type": "string", "enum": _KIND_ENUM},
                "anchors": _ANCHORS_PROPERTY,
                "repos": _REPOS_PROPERTY,
                "meta": _META_PROPERTY,
            },
        },
    },
    {
        "name": "hive_capture",
        "description": "Capture an insight of UNCLEAR value — anything durable you can ground "
        "in VERIFIABLE EVIDENCE you observed (a bug you hit, a behavior you "
        "saw, a decision you made — not speculation). It lands quarantined — "
        "stored and embedded but NOT served — until measured demand from other "
        "agents promotes it. Use for durable insights: bugs+fixes, dead-ends, "
        "decisions, gotchas. No replaces — it cannot retire anything. Set "
        "polarity='dont' for a PROHIBITION (don't do X) or 'do' for a "
        "prescription so the recalled rule is never read as its opposite "
        "(default 'neutral')." + _WRITE_GUIDANCE + " " + WRITE_VS_CAPTURE,
        "inputSchema": {
            "type": "object",
            "required": ["text"],
            "properties": {
                "text": {"type": "string"},
                "polarity": {"type": "string", "enum": ["do", "dont", "neutral"]},
                "kind": {"type": "string", "enum": _KIND_ENUM},
                "anchors": _ANCHORS_PROPERTY,
                "repos": _REPOS_PROPERTY,
                "meta": _META_PROPERTY,
            },
        },
    },
    {
        "name": "hive_recall",
        "description": "Retrieve servable memories. Scope with repos=['name'] or "
        "['name@branch'] (drift judged at that branch tip; default = the "
        "repo's canonical ref); OMIT repos for a GLOBAL search. General "
        "(repo-less) memories are always candidates; anchor_prefix keeps only "
        "hits with an anchor under that path (general hits stay). Returns "
        "{reference_context:[hits], abstained, state, top_cos}; on abstain "
        "reference_context is []. Each hit carries trust ('established' = "
        "outcome-verified on the canonical line, 'provisional' = unverified), "
        "ts, polarity (do|dont|neutral), kind, repos, anchors ([{repo, "
        "anchor}]), and drift (fresh | anchor_changed | anchor_missing | "
        "blast_radius_changed | branch_scoped | unverifiable | n/a — most "
        "severe across its anchors; unverifiable is not judged, never proof of "
        "freshness). Treat a drifted hit as REFERENCE ONLY. " + QUERY_GUIDANCE,
        "inputSchema": {
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {"type": "string"},
                "repos": {"type": "array", "items": {"type": "string"}},
                "anchor_prefix": {"type": "string"},
            },
        },
    },
    {
        "name": "hive_supersede",
        "description": "Retire one existing memory in favor of another — MACHINE-GATED: the "
        "server retires the loser only when it verifies a qualifying machine "
        "signal (anchor drift at the canonical tip, hurt evidence from another "
        "identity or a verified outcome, a mechanical contradiction, or the "
        "winner is a near-dup successor); otherwise the call is a benign noop "
        "envelope and nothing is retired — never an error. Nothing new is "
        "written. For a NEW corrected memory that retires an old one, use "
        "hive_write(replaces=) instead. Candidates surface in "
        "hive_health(include_conflicts=true).",
        "inputSchema": {
            "type": "object",
            "required": ["loser", "winner"],
            "properties": {"loser": {"type": "integer"}, "winner": {"type": "integer"}},
        },
    },
    {
        "name": "hive_prune",
        "description": "Retire a memory that is INCORRECT, MALICIOUS, or MISLEADING — it is "
        "deprecated (never recalled again) with no replacement and stays in "
        "the audit ledger. MACHINE-GATED: the server retires only when it "
        "verifies a qualifying machine signal for the target (drift, hurt "
        "evidence from another identity or a verified outcome, contradiction); "
        "an unqualified call is a benign noop — nothing retired, never an "
        "error. NOT for neutral or merely off-topic memories (omit those; "
        "leave them as-is). To CORRECT a memory (retire-and-replace), prefer "
        "hive_write(replaces=). Resolves a hurt candidate surfaced by "
        "hive_outcome or a review worklist. " + BAD_VS_STALE,
        "inputSchema": {
            "type": "object",
            "required": ["episode_id"],
            "properties": {"episode_id": {"type": "integer"}},
        },
    },
    {
        "name": "hive_flag",
        "description": "Advisory ONLY: flag two recalled memories as in conflict or one "
        "superseding the other. It NEVER retires anything and NEVER qualifies "
        "the retirement gate — it records a note surfaced in "
        "hive_health(include_conflicts=true) to resolve via hive_supersede. "
        "kind='conflict' (they disagree, winner unknown) or kind='supersedes' "
        "(winner names the surviving memory). Use for the semantic cases the "
        "automatic near-dup scan can't reach (different wording or location). "
        "Pass resolution to record your rationale (it is secret-scanned).",
        "inputSchema": {
            "type": "object",
            "required": ["a", "b", "kind"],
            "properties": {
                "a": {"type": "integer"},
                "b": {"type": "integer"},
                "kind": {"type": "string", "enum": ["conflict", "supersedes"]},
                "winner": {"type": "integer"},
                "resolution": {"type": "string"},
            },
        },
    },
    {
        "name": "hive_outcome",
        "description": "After a task, log which recalled memories materially HELPED or HURT "
        "your work, by their episode_id from reference_context. Omit neutral "
        "or contextually-irrelevant ones. Records EVIDENCE only — it changes "
        "no trust and retires nothing by itself. Helped rows fuel promotion "
        "(a canonical-line verified win establishes); hurt rows feed the "
        "machine retirement gate (a hurt row from ANOTHER identity qualifies "
        "the target for prune/supersede).",
        "inputSchema": {
            "type": "object",
            "required": [],
            "properties": {
                "helped": {"type": "array", "items": {"type": "integer"}},
                "hurt": {"type": "array", "items": {"type": "integer"}},
            },
        },
    },
    {
        "name": "hive_health",
        "description": "Cheap liveness/identity snapshot (+ trust_counts, n_misses_7d). "
        "Fail-closed {ok:false,error,db_path} on a probe failure. "
        "include_gaps=true adds the clustered demand-gap report (misses carry "
        "their repo scope). "
        "include_trends=true adds current-vs-previous 7d demand-health "
        "trends (confident rate + demand entropy). "
        "include_conflicts=true adds the conflict/redundancy worklist "
        "(near-dup contradictions + supersession candidates + agent-flagged "
        "advisories, bucketed by repo and anchor) to resolve via "
        "hive_supersede. "
        "include_suspect_consensus=true adds the suspect-consensus worklist: "
        "provisional promotions whose demand had thin effective independence "
        "(promoted on correlated asks, the confidently-wrong-but-popular "
        "case) to re-examine. "
        "include_stale_suspects=true adds the graph-propagated staleness "
        "worklist: servable memories whose anchor sat in the blast radius of "
        "a breaking/removed change — re-verify each against the code, then "
        "retire via hive_supersede/hive_prune if truly stale. "
        "include_census_health=true adds {repos: a census/sync block per "
        "registered repo (days since last change_outcome, sync state; a dark "
        "feed reads null), fleet: the sync daemon's OWN last_sync_ts + "
        "last_error — a fleet last_error is the daemon itself failing, under "
        "which every repo block is a stale snapshot}. "
        "include_meta_versions=true adds the live-corpus per-key "
        "token-version histogram (+absent). "
        "embedder_loaded reports whether this process's embedder is resident "
        "(the container HEALTHCHECK is a separate process reading boot "
        "markers).",
        "inputSchema": {
            "type": "object",
            "required": [],
            "properties": {
                "include_gaps": {"type": "boolean"},
                "include_trends": {"type": "boolean"},
                "include_conflicts": {"type": "boolean"},
                "include_suspect_consensus": {"type": "boolean"},
                "include_stale_suspects": {"type": "boolean"},
                "include_census_health": {"type": "boolean"},
                "include_meta_versions": {"type": "boolean"},
            },
        },
    },
]

# The canonical tool name set — the dropped-verb guard reads this.
TOOL_NAMES: frozenset[str] = frozenset(t["name"] for t in TOOL_DEFINITIONS)
