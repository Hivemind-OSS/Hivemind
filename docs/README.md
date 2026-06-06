# Hivemind v-min — Design & Build Plan

End-to-end software-design deliverable for the **containerized greenfield rebuild** of Hivemind
(single-tenant episodic recall store for an agent fleet; one MCP server, `hive_*` tools). Produced
by the 7-step design procedure (define → design-twice → contracts → risk-slice → build → refactor →
design doc), grounded in `../HIVEMIND_VMIN_SPEC.md` and the `../../AgentCortex/cls_memory` port
reference, reviewed against the `software-design-review` (APOSD + agent-native) rubric.

## Read in this order

| # | File | What it is | Step |
|---|---|---|---|
| 0 | [`00-PROBLEM.md`](00-PROBLEM.md) | Problem, core flows (incl. the containerized first-run flow), non-goals, hard constraints | 1 |
| 1 | [`01-DECISIONS.md`](01-DECISIONS.md) | The 7 design-twice decisions — two divergent options each, judged; winner + rejected alternative | 2 |
| 2 | [`02-CONTRACTS.md`](02-CONTRACTS.md) | **The single contract registry** — DDL, every port Protocol, the 8 MCP schemas, request flows, the `hive_init` handshake. *Authoritative over module prose.* | 3 |
| 3 | [`03-modules/`](03-modules/) | 12 module deep-specs (M01–M12), each with a first-class test contract + an independent design review | 3 |
| 4 | [`04-RISK-SLICE.md`](04-RISK-SLICE.md) | Risk ranking R1–R7 + the thin vertical slice (the §11 trace↔outcome join) that validates the design first | 4 |
| 5 | [`05-BUILD-PLAN.md`](05-BUILD-PLAN.md) | **The executable plan** — chunked, test-first, Phase 0 (slice) → Phase 1 (substrate) → Phase 2 (keystone-gated); Dockerfile/compose/`hive_init`/teardown/import inline; the §6.1 gate→test map; 37 mutation faults | 5 |
| 6 | [`06-DESIGN-DOC.md`](06-DESIGN-DOC.md) | The lightweight living map: module/port diagram, decisions log, **swap-seam map**, tech debt, repo-navigation note | 7 |
| 7 | [`07-REVIEW.md`](07-REVIEW.md) | The integrated design review (8/10+ across the board, swap-seam 9/10) + completeness critic. **Blockers it raised are CLOSED in §8.** | — |
| 8 | [`08-RESOLUTIONS.md`](08-RESOLUTIONS.md) | **Authoritative pins** (Clusters A–D) closing all review blockers; override any contradicting module text | 6 |

## Headline

- **Architecture:** hexagonal ports-and-adapters; pure `hive/domain/` core (AST-enforced no-I/O), swaps in `hive/adapters/`.
- **Shipping shape:** one Docker Compose service (MCP server + in-process git-producer + baked CPU `bge-small`), one SQLite-WAL volume, non-root.
- **Three swap seams** (the user mandate): `EmbeddingProvider`, `VectorIndex`, `OutcomeSource` — each swaps via one config key + one adapter file, core untouched.
- **Tests are a first-class contract:** every module ships its test-first contract; the build plan is test-first per chunk with RULE-2 mutation checks; the four product invariants each have a dedicated test + mutation.
- **The keystone:** move #6 (verifiable git-outcome credit) ships *observed-not-applied* in Phase 1; flipped into recall only if its §6.6 eval beats the recency/frequency baselines in Phase 2.

## What this deliverable does NOT do

- It does **not** write source files (design + plan only — `hive/` is fully specified, not coded).
- It does **not** execute the teardown of the existing system. `05-BUILD-PLAN.md` ships an idempotent,
  reversible `teardown.sh` (archives `~/cortex`, disables the `cortex-*` systemd units, strips the
  global hooks) and `import.sh` (re-embeds the old corpus through the scanner) — **run only on your
  explicit go-ahead.**
