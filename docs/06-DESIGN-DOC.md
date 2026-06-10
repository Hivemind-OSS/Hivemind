# 06 — Lightweight Living Design Doc (the map, not the contract)

> Step 7. One- to two-page orientation map. The **contract** is `02-CONTRACTS.md`; the
> **authoritative pins** are `08-RESOLUTIONS.md`; the **executable order** is
> `05-BUILD-PLAN.md`. This file is the cheap-to-write record of *what was decided and why*
> so the context is never re-derived.

## §1 — System summary

Hivemind v-min is a **single-tenant episodic recall store for an agent fleet**, shipped as
**one MCP server** (8 `hive_*` tools) in a **single Docker Compose service image** (server +
in-process git-producer + baked CPU `bge-small`), persisting to **one SQLite-WAL file** on one
named volume. The architecture is **hexagonal ports-and-adapters**: a pure `hive/domain/` core
(encode → recall/abstain → admit → credit) depends only on Protocols; every I/O and every
swap axis is a thin adapter, and the core is unit-tested entirely against in-memory fakes. The
recall path is **dense cosine-kNN** over a PCA-projected 256-d value with a **normalized-entropy
abstention gate** (`H/ln(N_eff) > 0.5 ⇒ return nothing`); the never-hallucinate, approved-only,
secret-safe, and verifiable-credit invariants are made **structural** (frozen self-asserting
dataclasses + a query-layer `status='approved'` JOIN + a pre-stage secret scan + a git-fact-only
credit path). The one differentiated capability — **move #6**, crediting recalled memories by the
*verifiable git outcome* of the task they fed — ships **observed-not-applied in Phase 1** and is
**flipped into recall only if its §6.6 keystone eval passes** in Phase 2. Target: ~13× the recall
of today's baseline (recall@5 0.026 → ~0.35) at honest abstention, in ~5k LOC vs ~40k.

## §2 — Module / port map

```
                         MCP harness (Claude Code / Codex / …)  ── 8 hive_* tools over stdio
                                              │
┌─────────────────────────────────────────────┼──────────────────────────────────────────────┐
│ hive/app/   (composition + I/O edge)         ▼                                               │
│   mcp_server.py · container.py · registry.py · config.py · onboard.py(hive_init,M07)         │
│   producer_loop.py · observability.py · health.py                                            │
└───────────┬───────────────────────────┬───────────────────────────────┬─────────────────────┘
            │ depends on ports only      │                               │
┌───────────▼───────────────────────────▼───────────────────────────────▼─────────────────────┐
│ hive/domain/   ★ PURE  (no sqlite/torch/subprocess/os/git/time — AST-enforced)               │
│   ports.py  models.py  errors.py                                                             │
│   recall.py   ── RecallPipeline + NormalizedEntropyGate       (M04, C3+C4) never-hallucinate │
│   surfacer.py ── UtilitySurfacer  (free-standing, A1)         (M08) weight·f(u), demotes     │
│   admission.py ── AdmissionService.stage/approve/reject       (M05, C6) secret floor + FSM   │
│   secret_scan.py ── scan(text)->ScanVerdict                   (M05/§9)                        │
│   attribution.py ── Attributor.split + PredictionBiasMonitor  (M08, C9) A3/A6 pure credit    │
│   join.py     ── OutcomeJoiner  §11 associate/settle/clawback (M09, C10)                      │
│   produce.py  ── OutcomeProducer.step(now) in-proc 1-writer   (M09, C10)                      │
└───────────┬──────────────┬──────────────┬──────────────┬───────────────┬─────────────────────┘
   PORT     │              │              │              │               │   (Protocols in ports.py)
 Embedding  │   Vector     │  Episode/    │  Outcome     │  Secret       │  Clock
 Provider ◀─┤   Index ◀────┤  UtilityStore│  Source ◀────┤  Scanner      │
            │              │              │              │               │
┌───────────▼──────────────▼──────────────▼──────────────▼───────────────▼─────────────────────┐
│ hive/adapters/  (swap = one file + one config key)        │ hive/store/ hive/ops/ hive/research/ │
│  embedding/{local_st,fake,head}  ⟵ SWAP-1 embedder.kind   │  sqlite_episode_store.py   migration │
│  index_exhaustive.py  ⟵ SWAP-2 index.backend (authoritative)│ sqlite_utility_store.py    backup    │
│  source_git.py        ⟵ SWAP-3 producer.kind              │  (one SQLite-WAL file /data/shared.db)│
│  scanner_regex.py · clock_system.py                       │  research/ = DEV-TIME ONLY (eval)    │
└──────────────────────────────────────────────────────────────────────────────────────────────┘

   DATA FLOW
   capture :  agent → hive_write → secret_scan → AdmissionLedger.stage(status=pending) ─┐
   approve :  human "yes" (native chat) → hive_approve → status=approved + index add ────┘  (only path)
   recall  :  agent → hive_recall → encode → index.search → gate → [abstain | surfacer] → trace+exposure
   learn   :  commit → producer.step (associate→settle→clawback→emit→drain→posterior) → surfacer biases recall
```

## §3 — Decisions-made log (the 7 design-twice calls — full detail in `01-DECISIONS.md`)

| # | Decision | Winner | Rejected alternative — and WHY it lost |
|---|---|---|---|
| **D1** | Architecture style | **Hexagonal ports-and-adapters** | *Layered (storage→core→serving)* — the three mandated swap axes would not be **structural**: an external-vector-DB or remote-embedder swap becomes a refactor, not an adapter, and the domain can't be tested in isolation. Hexagonal makes each swap an adapter and the pure core fake-testable. |
| **D2** | Embedding provider seam | **`EmbeddingProvider` port** (in-proc `bge-small` default + out-of-proc sidecar adapter contract); the encode chain `embed→pca→normalize` is the SINGLE source for capture+recall | *Port the bare `TextEmbedder` Protocol as-is* — couldn't move the provider out-of-process (GPU/remote) without touching every encode caller, and left the `embedder.py:289-306` random-head/lazy-PCA **split-brain** (capture and recall could disagree). |
| **D3** | Storage / vector-index seam | **Split `EpisodeStore` (durable truth) + `VectorIndex` port** (exhaustive **authoritative**, `rebuild_from_store()`) | *Single `MemoryStore` owning rows+index together* — an HNSW/external backend swap becomes a store refactor, and co-owning the index opens a two-writer drift window. The split makes the index a **derived, swappable cache** the store reconstructs; exhaustive-authoritative kills the `approx_threshold` ANN trap. |
| **D4** | Onboarding / `hive_init` | **Pure-MCP handshake**: `hive_init` returns a content-hash-verified `InstallPlan` the agent applies (marker+version rules-file block) and verifies via `hive_health` | *Repo bootstrap script `./hive up`* — a second install surface, weaker idempotency, and it splits the trust boundary off the server. Pure-MCP keeps the server the sole boundary and is idempotent on re-run. |
| **D5** | Config + provider registry | **Layered config** (baked defaults ← `/data/hive.toml` ← `HIVE_*` env) **+ provider registry** + reload-tier-as-data + single-source derived constants | *Monolithic `CortexConfig`* — reproduces CONFIG_DRIFT traps (the abstention floor set in one gate not both) and buries swap selection. Anchor: the verified `CORTEX_D` env-collision (`config.py:762-773`). |
| **D6** | Producer packaging + §11 join | **`OutcomeSource` port + pure `OutcomeJoiner`** (B's I/O-sealing) **driven by A's in-process single-writer `step()` tick** (synthesis) | *In-process baked join* — the §11 policy (the riskiest new code) wouldn't be unit-testable in isolation or swappable. Sealing git I/O behind a port and routing all policy through a clock-injected pure joiner makes **swap axis = test axis = policy axis**. |
| **D7** | Admission / approval surface | **`AdmissionLedger` idempotent FSM** (pending→approved\|rejected, terminal, replay-safe) + **approved-only JOIN at the query layer** | *Direct `status`-column mutation with a post-filter* — a post-filter can leak a pending row. The query-layer JOIN makes a pending row **structurally unreachable**; the FSM makes approval idempotent. |
| **D8** (2026-06-10) | Autonomous capture lifecycle | **v2 mechanical demand** (AUTONOMY-PLAN): `hive_capture` lands quarantined-unservable; promotion = measured recall-miss demand from ≥1 non-writer identity with no servable competitor; decay by TTL; human `hive_write(replaces=)` is the only retirement of established. Every load-bearing signal is server-observable (writes, recalls, token identities, time). | *v1 evidence economy* (tiers, `hive_evidence`, `trustctl` review queues) — made humans and cooperating agents **load-bearing fuel** that would not reliably arrive; promotion would starve. Cut to AUTONOMY-PLAN Appendix A with add-back paths. Promotion deliberately means *demanded-unique-not-self-demanded*, NOT *true* — the guards are the per-hit trust label, cheap supersession, and decay. |

**Cross-cutting locked facts:** hexagonal; single Docker service; one SQLite-WAL volume; **exactly 6 MCP tools** (v3 client-gating dropped the queue verbs; the lifecycle build added `hive_capture`); geometry `bge-small(384)→PCA→d=256` dense cosine; exhaustive index authoritative; normalized-entropy abstention `H/ln(N_eff)>0.5`; serving = `lifecycle.is_servable` (established, or fresh provisional, labeled).

## §4 — Swap-seam map (the explicit user mandate)

| Axis | Port (`hive/domain/ports.py`) | Default adapter | Swap = | Stays untouched |
|---|---|---|---|---|
| **Embedding provider** | `EmbeddingProvider.encode/encode_batch` | `adapters/embedding/local_st.py` (`bge-small`, CPU, baked) | add one adapter (`openai.py`/`sidecar.py`) + set `embedder.kind`; registry `EMBEDDING_PROVIDERS` | the encode-chain callers (recall, admission) + the frozen PCA head codec |
| **Embedding storage / vector index** | `VectorIndex.search` + `MutableVectorIndex` + `rebuild_from_store` | `adapters/index_exhaustive.py` (signed cosine, no ANN branch) | one adapter (`index_hnsw.py`/external) declaring `is_authoritative()`; set `index.backend` | `EpisodeStore` (durable rows/blobs/ledgers); recall refuses a non-authoritative index unless opted in |
| **Outcome producer** | `OutcomeSource.poll` → `GitFacts` | `adapters/source_git.py` (`git log/show/blame`) | one **in-process** source adapter (`WebhookOutcomeSource`) + set `producer.kind` | the pure `OutcomeJoiner` §11 policy (associate/settle/clawback/family) |

**Non-swappable boundaries (by design):** the single-tenant instance boundary; the never-hallucinate
gate; the secret floor; the server-as-trust-boundary. **Demoted from "one-config swap":** a
**cross-container producer sidecar** — two OS processes writing one WAL is the out-of-scope
multi-writer case (§9). Only the **in-process** source adapter swap is clean; a separate-container
producer is a future adapter needing its own cross-process single-writer design (`BEGIN IMMEDIATE` +
`busy_timeout`).

## §5 — Known shortcuts / tech debt (accepted, with the reason)

- **Phase-1 inert utility** — `utility.utility_rerank=False`; outcomes are *collected* but never bias recall until the §6.6 keystone passes. The surfacer is un-crippled but a no-op until confident posteriors exist.
- **In-process producer** — chosen for single-writer simplicity over a sidecar; revisit only at fleet scale.
- **Agent-relayed approval** — the server trusts the agent to relay the human "yes" (§9 high-trust residual); a fleet tier would harden it with authenticated admission.
- **`EpisodeStore` god-port** (15+ methods across episodes/blob/ledger/migration) — accepted to keep the single-writer transaction as one object (resolution **B5**); a future ledger extraction forces a multi-call-site edit. The widest surface in the system; method groups are pre-segregated.
- **Open test-decisions D1–D4** (spec §5) — dense-vs-sparse ranker, β recalibration, `H_frac_max` floor, embedder tier — ship with the pinned defaults; each is a single eval A/B that decides the winner before ship.
- **Deferred §8.3 hybrid** (BM25/FTS5 + RRF + cross-encoder) and **Phase-2 live-loop hardening** — gated TODOs after the dense-only benchmark / keystone are green.
- **In-RAM index ⇒ boot-rebuild is the recovery guarantee** (resolution **B3**): the in-tx index add is a best-effort warm cache; durable truth is `status='approved'`, and the index is rebuilt from `scan_approved` on boot.
- **Residuals (§9):** the consuming model is trusted to treat recalled text as reference (not command); auto-capture coverage is uneven across harnesses.

**All pass-1 review blockers (UtilitySurfacer collision, query→family seam, gate label, ledger location, ε placement, isolation writer, keystone oracles, ProjectionHead codec, import secret-scan, posterior CI method) are CLOSED — see `08-RESOLUTIONS.md` Clusters A–D.**

## §6 — How an agent should navigate this repo

1. **`docs/02-CONTRACTS.md`** — the single contract registry (DDL, every port Protocol, the 8 MCP schemas, request flows, the `hive_init` handshake). Read this first; it is authoritative over module prose.
2. **`docs/08-RESOLUTIONS.md`** — authoritative pins (Clusters A–D) that override any contradicting module text.
3. **`docs/05-BUILD-PLAN.md`** — the executable, test-first chunk order (Phase 0 vertical slice → Phase 1 substrate → Phase 2 keystone-gated). Build in this order.
4. **`docs/03-modules/M0x.md`** — per-module depth + each module's test contract. **`docs/01-DECISIONS.md`** — the *why* behind each boundary.
5. **In code:** `hive/domain/` is pure (an AST test forbids `sqlite3|torch|subprocess|os|git|time` imports there); ports live in `hive/domain/ports.py`; all swaps live in `hive/adapters/` selected by `hive/app/registry.py` + `hive/app/config.py`.
