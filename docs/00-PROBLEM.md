# 00 — Problem Definition (Hivemind v-min, containerized rebuild)

> Step 1 of the design procedure: *define the problem, not the solution.*

# 00 — PROBLEM: Hivemind v-min (containerized rebuild)

## What this document is
This defines the **problem**, not the solution. It is the contract the design must
satisfy; it deliberately resists naming an architecture. Source of truth: `HIVEMIND_VMIN_SPEC.md`
(§1–§12), with the working `cls_memory` tree as the port reference.

## The problem
An agent fleet (solo dev → startup → small company; multiple MCP-speaking harnesses:
Claude Code, Codex, Conductor, …) keeps re-discovering the same hard-won insights — bug
root causes, fixes, decisions, gotchas — because nothing durable remembers them across
sessions, agents, and repos. We need a **single-tenant episodic recall store** that lets
any agent **capture** a high-signal insight, **recall** the relevant ones for a new task
**or honestly say nothing**, and **fetch** the exact text — and that **learns** which
memories actually helped from *verifiable* evidence, not from what an agent claims.

The store must be trustworthy enough to share: an insight is only readable after a
**human approves** it, a raw **secret can never** be persisted, and recall must
**refuse rather than guess**. It must be cheap to adopt — a fresh agent pointed only at
the repo brings the whole thing up in **containers** and **wires itself** into its own
harness — and it must be honest about its one bet: that crediting memories by
machine-checkable git outcomes makes recall measurably better, a claim whose prior
scaling result was **refuted** and which this MVP exists to re-test on the smallest
honest slice.

## Who it is for and the trust setting
A **high-trust** environment where ~99% of writes derive from in-codebase / git / GitHub
work. The **MCP server is the sole trust boundary**; per-harness hooks are optional
convenience. Single deployment per org; the instance *is* the data boundary.

## Must-deliver capabilities (outcomes, not designs)
- **Capture → approve**: an agent or trigger proposes an insight; it is staged, never
  recallable, until a human approves it in the harness's native chat.
- **Recall or abstain**: return the most relevant approved insights, or **nothing** when
  not confident — and a refused query is *never* rescued downstream.
- **Fetch**: content-hash → exact verbatim text.
- **Learn (move #6 — the keystone)**: credit recalled memories by the **verifiable** git
  outcome of the task they fed (settled merge = small +, revert / same-blame-line bug-fix
  = large −), scoped to a task-family, biasing future recall — never via agent self-report.
- **Containerized self-onboarding**: clone → read README → bring up the containers →
  first MCP contact → run the **init tool** that *both* links the agent to the server
  *and* writes the required operating instructions into the harness's native rules file →
  fully available, no further manual wiring.

## Invariants the design may never violate
1. **Never-hallucinate** (abstain-no-resurrect).
2. **Approved-only recall**, server-enforced (not hook-enforced).
3. **Secret-safe**: deterministic scan refuses/redacts *before* staging.
4. **Verifiable-credit-only**: utility moves only on git facts; blame-LINE overlap (not
   same-file) gates the bug-on-files clawback; no signal ⇒ no credit.
5. **No network on the hot path**; CPU-local baked embedder; in-process producer off the
   hot path.
6. **Fixed geometry** (do not re-derive): bge-small-en-v1.5 → PCA → d=256 → dense
   cosine-kNN; **exhaustive index authoritative** (never silently flips to ANN);
   normalized-entropy gate abstains at H/ln(N_eff) > 0.5.
7. **Swappability**: embedding provider, vector storage/index, and outcome producer each
   behind a port — swap by config/adapter, no core refactor.
8. **Single-tenant-per-instance**; `tenant_id` is a constant label, not a filter.

## What success looks like (the bar)
- recall@5 **≥ 0.33** (~0.35 expected), recall@10 ~0.42, abstention AUROC **~0.77** —
  on **≥10k** real episodes with natural query→gold pairs, no contrived masking,
  bootstrap-CI-significant.
- Secret scan refuses a planted credential; a pending write is provably unrecallable; a
  W_version bump re-embeds the store and reproduces the numbers.
- Move-#6 join round-trips: commit → traces → settled +; planted revert and
  blame-overlapping bug-fix each clawback; a coincidental same-file edit does **not**.
- **The keystone is binary**: utility-weighted recall beats a **recency** (and frequency)
  baseline on family-scoped task success, CI-significant, non-saturating, with
  within-family transfer — *and the lift traces to the ungameable clawback*. A clean
  **negative is a successful outcome**: it kills six unbuilt moves cheaply.

## Out of scope (one line each)
Consolidation/GMM · federation · differential privacy · BM25/graph channels &
cross-encoder rerank · cold/archive tier · bi-temporal & supersession · multi-tenant ·
adversarial-fleet security (poisoning, Sybil, clawback-gaming, per-source trust).

## Hard constraints on this pass
Containerized (Docker Compose, single non-root multi-stage image; SQLite on a named WAL
volume; secrets via env, fail-fast). **TDD-first**: every module ships failing tests
before code, covering happy path + every failure mode + every invariant, with mutation
testing on every gate/ranker/state-machine/credit path. **Docs only** this pass — design
+ build plan; teardown/import are scripts-not-executed; no source files are written."]

## Must do (capabilities as outcomes)
- Capture: accept a high-signal text insight from any agent/hook and hold it OUT of the recallable set until a human approves it — nothing an agent can read was admitted unreviewed.
- Recall: given a query, return the most relevant approved insights OR return nothing when not confident — abstention is a first-class result, never a degraded guess.
- Fetch: resolve a stored item's content hash to its exact verbatim text (no paraphrase, no lossy reconstruction).
- Learn (move #6, the keystone): credit each recalled insight by the VERIFIABLE git outcome of the task it fed (a merge that survives = small settled +, a revert or a same-blame-line bug-fix = large -), scoped to one task-family, and let that bias future recall — never from agent self-report.
- Containerized onboarding: a fresh agent pointed only at the repo URL must be able to clone, read the README, bring the whole system up in containers, make first MCP contact, and run one init tool that BOTH establishes the agent<->server link AND writes the required operating instructions into the harness's native rules file — after which the system is fully functionally available with no further manual wiring.
- Prove honest recall quality on a realistic corpus: recall@5 >= 0.33 and abstention-gate AUROC ~= 0.77 on >=10k real episodes with natural query->gold pairs, measured with no contrived masking.
- Secret-safety floor: a deterministic credential scan refuses or redacts a raw secret BEFORE it is staged, so the store and its backups never persist one.
- Run as ONE MCP server (the hive_* tools) that behaves identically under any MCP-speaking harness, with the SERVER as the sole trust boundary — hooks are optional convenience, never load-bearing.
- Survive a geometry change: a W_version bump must re-embed and re-key the whole store from verbatim text and reproduce the recall numbers — no silent corruption.

## Core flows (in order)
- FIRST-RUN ONBOARDING (verbatim-faithful to the ask): operator points a fresh agent at the repo -> agent clones the repo -> agent reads the README -> agent brings up the containerized system (Docker Compose: single service image running MCP server + in-process git-producer + baked CPU embedder, SQLite on a named WAL volume) -> first MCP contact (initialize / tools/list succeeds, health is green) -> agent calls the init tool, which (a) establishes the agent<->server link/identity and (b) writes the required operating instructions (when to capture, the approval relay, the Hive-Trace commit-trailer convention, the recall-is-reference-context framing) into the harness's NATIVE rules file -> system reports ready for full functional availability.
- CAPTURE -> APPROVE -> RECALL: an agent (or a git-commit/mark/log-bug trigger) calls hive_write -> server runs the deterministic secret scan (refuse/redact) -> row is STAGED status=pending, returns its id -> agent surfaces the pending write(s) for approval in the harness's native chat ('save these N insights?') -> a human approve flips status=approved, stamps approver/time, and indexes the row -> a later hive_recall over approved rows only returns it as ranked reference context, or abstains; a pending row is never recallable.
- LEARN LOOP (move #6): hive_recall issues a trace_id and logs which memories (with recall margin) were injected -> agent does the task and commits, optionally stamping a Hive-Trace: <traces> trailer -> the in-process git producer (off the hot path) associates the commit SHA with that agent's in-window traces (trailer overrides the window when present), derives family_scope (git-remote x language x workflow) at link time, and writes a small provisional + to task_outcomes -> the settlement sweep settles the + only after settle_days clean; a revert, or a bug-fix commit whose diff overlaps the original commit's blame lines, fires a large - clawback -> the reward drains from the sink, splits across exposed memories by recall margin, and updates the (episode, family) Beta-Bernoulli utility posterior -> once the posterior CI excludes 0, the surfacer biases recall (weight x f(utility), demotion allowed), with an epsilon-slice held out.
- ONE-TIME CORPUS IMPORT (to seed the eval): re-embed-from-text the old store's verbatim insights through the new PCA head into the fresh store, dropping the old bi-temporal/supersession columns — a one-shot off-hot-path import that produces the >=10k-episode eval corpus.
- OPS / RECOVERY: health is pollable; daily SQLite snapshot backups are retained; a geometry W_version bump triggers a re-embed migration whose round-trip reproduces the recall numbers; a human can zero/roll back the versioned utility layer without losing any memories.

## Explicit non-goals
- Consolidation / GMM / schema-formation — clusters measured as noise (silhouette 0.036); the store stays append-only and the loop credits episodes, not schemas.
- Federation across instances — single-tenant-per-instance is the hard boundary; no cross-instance pooling.
- Differential privacy / secure aggregation — unproven moat, post-MVP.
- Hybrid recall channels (BM25/FTS5 + RRF fusion) — deferred to the gated TODO that runs only after the dense-only benchmark is green and re-tested on the code corpus.
- Cross-encoder reranking — conditional-only benefit + carries the rerank_top_k truncation bug; off for the MVP.
- Graph recall / relational graph — fleet/moat tier, deferred.
- Cold / archive tier (recall_cold, restore_cold) — post-MVP; no cold tables or tools.
- Bi-temporal validity and supersession columns — not needed for episodic recall; dropped on import.
- Multi-tenant isolation beyond the single-instance boundary — tenant_id is a constant label, not a query filter.
- Adversarial-fleet security (memory poisoning / prompt-injection contagion, Sybil / multi-writer abuse, utility-suppression / clawback-gaming, per-source trust & write-conflict resolution) — named out of scope for the high-trust MVP; the approval relay and consuming-model-trust seams are accepted residuals, not solved.
- The shadow-mode config controller / auto-tuner flipping live — stays gated regardless; the MVP only feeds and consumes the outcome-crediting path.
- Agent self-reported outcome as a credit signal (the noisy/gameable L0 hive_outcome) — replaced by verifiable-only credit.
- A separate approval UI — approval is relayed through the harness's native chat, not a bespoke interface.

## Hard constraints
- DATA TO STORE: one append-only episodes table (id, tenant_id, text, value=float[d] PCA vector, weight=capture salience only & immutable, ts, source, tags, content_hash=sha256(text), status pending|approved, proposed_by, approved_by, approved_ts) + a content-hash blob store for verbatim fetch + three move-#6 ledgers (exposure: trace_id->episode_id with recall_margin & task_ref; task_outcomes: producer state machine keyed by commit SHA with family_scope/files_touched/state/reward/settle_at; utility: Beta-Bernoulli (wins,losses,n_sources,version) keyed on (episode_id, family_scope)). family_scope is NOT an episode column — it is derived per-credit-event by the producer.
- NEVER-HALLUCINATE: recall returns nothing rather than a weak guess; a refused (EMPTY) query is provably never rescued by any later stage (abstain-no-resurrect).
- APPROVED-ONLY RECALL: the SERVER (not a hook) enforces that recall reads status='approved' rows only; the pending->approved trio (hive_pending/hive_approve/hive_reject) is the sole admission path; a pending row is never embeddable into another agent's context.
- SECRET-SAFE: a deterministic credential scan (sk-/AKIA/ghp_/xox/JWT/PEM/connection-strings + entropy) refuses or redacts BEFORE staging; raw secrets never reach the store or logs.
- VERIFIABLE-CREDIT-ONLY: utility moves only from machine-checkable git facts (merge that survives, revert, blame-overlap bug-on-files) — never from agent self-report; no verifiable signal => no credit; bug-on-files clawback requires blame-LINE overlap, not same-file, so a coincidental same-file edit must NOT clawback.
- NO NETWORK ON THE HOT PATH: capture/recall/fetch are local-only; the CPU embedder is baked into the image and loaded once, frozen; the git producer runs in-process and off the hot path.
- CPU-LOCAL FIXED GEOMETRY (do not re-derive): embedder BAAI/bge-small-en-v1.5 (384d) on the sentence-transformers path; projection_head=pca; dense value d=256; dense cosine-kNN ranker; exhaustive vector index made AUTHORITATIVE so growing N can never silently flip to the ANN/approx path; abstention = normalized-entropy gate, abstain when H/ln(N_eff) > 0.5.
- INTEGRATIONS: exactly one MCP server exposing hive_write/hive_recall/hive_fetch + the admission trio + the init tool; integrates with git/CI by READING git (commit trailer Hive-Trace is the only new agent behavior; no trusted MCP message carries an outcome).
- SCALE NEEDED NOW: evaluate at realistic N >= 10k episodes (so approx_threshold and decay/prune behaviors are actually exercised), with bootstrap CIs on every reported delta — ship only CI-significant improvements.
- SWAPPABILITY (explicit user ask): the embedding PROVIDER, the embedding STORAGE / vector index, and the outcome PRODUCER must EACH sit behind a port and swap via config/adapter with NO core refactor.
- CONTAINERIZED, NON-ROOT: Docker Compose single service image; multi-stage build ending in a non-root USER; SQLite on a named volume in WAL mode; a compose file present so an embedder/producer sidecar is a later swap (not built now); secrets read strictly from env vars, fail-fast at startup if a required one is missing.
- TDD-FIRST (hard mandate): every module ships a Test Contract — failing tests written BEFORE implementation covering happy path + EVERY failure mode + EVERY invariant; mutation testing (RULE 2: introduce a deliberate fault -> the named test goes red -> restore -> green) is required for every gate/ranker/state-machine/credit path; prefer contracts that cannot lie (types/JSON schemas/runtime assertions) over prose.
- DELIVERABLE SCOPE OF THIS PASS: design + build-plan DOCS ONLY; teardown.sh/import.sh are PLAN+SCRIPT only (archive ~/cortex via mv, disable cortex-* timers, strip global hooks) and are NOT executed; no source files are written.
