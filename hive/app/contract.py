"""The served usage contract — the v3 floor, delivered fresh at every connect.

Agents are thin, repo-agnostic MCP clients: they recall and store; the server owns mint,
staleness, poison, outcome, promotion, and the retirement gate. The whole contract an agent
needs is therefore what the MCP ``initialize`` result's ``instructions`` field serves at
connect (the spec-canonical channel every client surfaces to the model automatically) — there
is no install step, no versioned rules block, no client-side hook set, and no re-onboard loop.
Sessions pick up a changed contract on reconnect.

Every string delivered over a capped MCP metadata field (``initialize.instructions``, each
tool ``description``) fits under ``METADATA_FIELD_LIMIT`` — the measured client truncation
point — so it always arrives whole (BUG-023). The fit is asserted in tests, never assumed.

Exactly five public names, nothing else:

- ``METADATA_FIELD_LIMIT`` — the capped-field byte budget every served metadata string obeys.
- ``SERVER_INSTRUCTIONS`` — the v3 usage contract: recall-first, scoped store/recall,
  write-vs-capture, outcome evidence, machine-gated retirement.
- ``REMEDIATION_NOTICE`` — the per-hit rider the server attaches to a recall hit whose anchor
  it already knows is stale, so a stale memory carries its options on every harness with zero
  client tooling. Serving-side text (a tool RESULT, the uncapped channel).
- ``WRITE_VS_CAPTURE`` — the store-verb decision rule, composed verbatim into the floor and
  the write/capture tool descriptions (one source, no drift).
- ``BAD_VS_STALE`` — the retire diagnosis, composed verbatim into the floor and the
  hive_prune description (one source, no drift).
"""

# The measured truncation point for a capped MCP metadata field (``initialize.instructions``
# and each tool ``description``): the delivery path hard-clips any string longer than this,
# mid-word, with no ellipsis. Applies ONLY to those two field kinds — never the result payload
# of a tool call, which is proven uncapped (BUG-023).
METADATA_FIELD_LIMIT: int = 2048

# The store-verb decision rule (v3): the write side is serve-as-provisional-then-heal — no
# approver exists anywhere on the surface. Composed verbatim into SERVER_INSTRUCTIONS and the
# hive_write / hive_capture descriptions, one source, no drift.
WRITE_VS_CAPTURE: str = (
    "WRITE by default: hive_write serves NOW (trust=provisional) and is healed afterward by "
    "outcome and drift evidence; hive_capture is the unclear-value tail — quarantined until "
    "fleet demand promotes it."
)

# The retire diagnosis — STALE (replace) vs BAD (prune) — composed verbatim into
# SERVER_INSTRUCTIONS and the hive_prune description, one source, no drift.
BAD_VS_STALE: str = (
    "STALE (has a SUCCESSOR) -> hive_supersede/replaces=; BAD (incorrect/misleading) -> "
    "hive_prune. Old ts: not a guarantee."
)

# The per-hit rider the SERVER attaches to a recall hit it already knows is stale, so the
# stale memory carries its options on EVERY harness with zero client tooling. Rewritten for
# the agent-adjudicated flow: no human escalation exists — the agent CALLS retirement, and the
# server refuses (a benign no-op) unless it verifies a qualifying machine signal itself.
# hive_flag is deliberately ABSENT — it is a memory-vs-memory conflict tool (two episode ids),
# not an option for a single stale anchor, and it never qualifies the gate.
REMEDIATION_NOTICE: str = (
    "This memory's anchor no longer matches the code (stale) — treat it as REFERENCE ONLY, do "
    "not follow it. Record evidence now: hive_outcome(hurt=[<episode_id>]). To retire it, "
    "diagnose per BAD_VS_STALE: it has a SUCCESSOR (STALE) -> hive_supersede(loser, winner) or "
    "hive_write(replaces=...) carrying the corrected fact; it was wrong even when written "
    "(BAD) -> hive_prune(episode_id). Retirement is MACHINE-GATED: the server itself verifies "
    "a qualifying machine signal for the target (anchor drift at the canonical tip, hurt "
    "evidence from another identity or a verified outcome, mechanical contradiction) — an "
    "unqualified call is a benign no-op: nothing retired, never an error."
)

# The v3 usage contract, served via the MCP ``initialize`` result's ``instructions`` field —
# the always-on floor every connecting agent receives. Fits under METADATA_FIELD_LIMIT by
# terse wording, never by dropping a required directive; call-adjacent detail (exact schema
# grammar, envelope shapes, worklist flags) rides the per-tool descriptions instead.
SERVER_INSTRUCTIONS: str = (
    "Hivemind — a STIGMERGIC shared episodic memory for a FLEET (NO direct communication): "
    "hive_recall answers only when confident — no trace is served as a command. ONE store, "
    "partitioned by repo; agents are thin clients — the server owns staleness, outcomes, "
    "promotion, retirement. MAINTAINER: keep it LEAN — a FLOW not a stock, a BIGGER store "
    "is a WORSE one.\n\n"
    "EVERY TASK:\n"
    "- RECALL FIRST, SINGLE-POINTED — one intent per query, never bundle. Scope: "
    'repos=["name"] / ["name@branch"]; omit = GLOBAL; anchor_prefix narrows by path. '
    "Empty/abstained: proceed, NEVER invent a memory.\n"
    "- Each hit carries trust (established = outcome-verified on the canonical line; else "
    "provisional), polarity, kind, repos, anchors, drift. Drifted = REFERENCE ONLY — "
    "re-verify against the code; never follow a 'dont' as a 'do'.\n"
    "- STORE, per durable lesson: WHAT + WHERE + WHY — anchors=[{repo, anchor}] (registered "
    "repo + path/file.py::symbol) or repos=[...] scope-only; neither = general. "
    + WRITE_VS_CAPTURE
    + " Don't duplicate — recall first. Worth storing: DURABLE, "
    "REUSABLE, NON-OBVIOUS, EVIDENCE-GROUNDED; never the obvious, transient, secret, or "
    "already-recallable — nothing clears the bar -> store nothing.\n"
    "- OUTCOME, task-end: hive_outcome(helped=[ids], hurt=[ids]) — evidence only; fuels "
    "promotion and retirement.\n"
    "- RETIRE (machine-gated): "
    + BAD_VS_STALE
    + " The server retires ONLY on a machine "
    "signal it verifies (drift, other-identity or verified hurt, contradiction); "
    "unqualified = benign no-op, never an error. hive_flag is advisory only — never "
    "retires, never qualifies the gate.\n"
    "- MAINTAIN: hive_health worklists (conflicts, stale suspects, gaps) surface what "
    "needs resolving.\n\n"
    "Identity: per-session (Mcp-Session-Id) — never the bearer token."
)
