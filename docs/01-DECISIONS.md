# 01 — Design-Twice Decisions

> Step 2: for each major decision, two genuinely different approaches were generated in parallel, then judged. The rejected alternative and the reason are recorded so context is not re-derived later.

## Index
1. **Module decomposition / architecture style for the Hivemind v-min greenfield rebuild: Classic inward-pointing LAYERED architecture (A) vs. Hexagonal Ports-and-Adapters with a pure domain core (B).** — winner: **B**
2. **Embedding provider swap seam for Hivemind v-min — where to draw the port/interface boundary so the embedding PROVIDER swaps via config/adapter with no core refactor, while preserving never-hallucinate, hot-path no-network, and TDD-first mandates.** — winner: **B**
3. **Embedding storage / vector index swap seam for Hivemind v-min C5 Store + C3 ranker.** — winner: **synthesis**
4. **Containerized onboarding + hive_init handshake (first-run flow) for Hivemind v-min: how the first-run bootstrap and the server-served hive_init handshake should be shaped.** — winner: **synthesis**
5. **Config system + provider registry for Hivemind v-min: how to shape the config tree, the three mandated swap seams (embedder / vector index / outcome producer), the never-hallucinate floor wiring, and the reload-tier enforcement.** — winner: **synthesis**
6. **Outcome producer (git watcher) packaging + the trace<->outcome join for Hivemind v-min component C10. Two designs: A = one in-process reducer (`OutcomeProducer.step(now) -> ProducerTick`) over a single read-only `GitFacts` Protocol, with the §11 join (associate/settle/clawback) baked into the tick; B = a two-part cut — an `OutcomeSource` port that yields normalized `GitFacts` (all I/O quarantined) feeding a separate PURE `OutcomeJoiner` state machine (all §11 policy, clock-injected, git-free), wired by a thin `ProducerTick` driver.** — winner: **synthesis**
7. **Admission / native-chat approval surface (hive_pending / hive_approve / hive_reject + the hive_write scan→stage change): how to structure the pending→approved state machine, the secret-scan gate, and the approved-only recall boundary, scored against the design rubric (interface depth, cognitive load, information hiding, special-vs-general, future bolt-on, agent-navigability, enforced-not-prose contracts) plus the swappability / never-hallucinate / TDD-first mandates.** — winner: **synthesis**

---

## D1. Module decomposition / architecture style for the Hivemind v-min greenfield rebuild: Classic inward-pointing LAYERED architecture (A) vs. Hexagonal Ports-and-Adapters with a pure domain core (B).

- **Option A:** APPROACH A — Classic LAYERED. Modules grouped by technical layer (types/config, ports/, core/, adapters/, serving/, producer/, ops/) with a strictly inward dependency rule enforced by an AST import-linter test. The three mandated swap seams are three Protocol files in ports/; concrete I/O lives in adapters/; core/ imports only ports + types and is fully fake-testable. The decomposition axis is "technical layer," which optimizes perfectly for the swap mandate but smears each vertical capability (notably move #6: attribution + surfacer + producer-tick + outcome-sink port + sqlite-sink adapter) across five files in five layers.
- **Option B:** APPROACH B — HEXAGONAL (Ports & Adapters). A pure domain core (hive/domain/) exposing exactly four verbs (admit, recall, fetch, credit) depending on six Protocols (3 swap-axis ports: EmbeddingProvider, VectorIndex, OutcomeProducer; 3 I/O ports: EpisodeStore, SecretScanner, Clock). Adapters/ hold all I/O; app/ is the composition root with a single build_container(cfg). Purity boundary (domain/ may not import sqlite3|torch|subprocess) enforced by a CI grep-lint. The deciding insight: each product invariant is enforced by control flow inside one pure file — never-hallucinate lives entirely in domain/recall.py (the abstain return precedes the resolve step, so a refused query has nowhere to be resurrected), the secret floor lives in domain/admission.py (scan before stage), and the whole move-#6 credit math lives in domain/credit.py (typed Reward/GitCommitFact in, typed posterior out, zero I/O). One deliberate compromise: a wide EpisodeStore "god-port" fusing episodes + blobs + 3 ledgers + CAS to keep the §12 single-writer transaction as one object.
- **Winner:** B — See above (winner rationale in the why_it_won analysis): B localizes the keystone move #6 into one pure file, makes never-hallucinate and the secret floor structural by control flow, matches A's swap-seam mandate (and exceeds it via the contract-test exactness guarantee), and makes the whole domain fake-testable in milliseconds — winning the spec's primary axis (de-risk + iterate move #6, agent-navigability) without ceding the swap axis A was built for.
- **Rejected because:** A (LAYERED) rejected, with the specific costs that lost it:

- SMEARED KEYSTONE (the decisive defect). By A's own §A.6 admission, move #6 lives across core/attribution.py + core/surfacer.py + producer/tick.py + ports/outcome_sink.py + adapters/sqlite_sink.py. APOSD Information Leakage (one decision reflected in five modules → change amplification) + agent-native Scattered Truth. For a 5k-LOC MVP whose sole job is de-risking move #6 (§6.6 keystone, the one thing built before #1/#2/#4/#5/#7), forcing the agent to load five files in five layers to reason about the credit path is the wrong cost on the most-touched code. This is not a tie-breaker; it is the product's center of gravity landing on A's worst case.

- SHOTGUN SURGERY ON FEATURES. Adding family_scope to the credit path (a real, near-term change per §11) touches types.py + a ports Protocol + a core rule + sqlite_sink.py + producer/tick.py — five layers for one vertical concern. The layered axis optimizes "swap a technology" and pessimizes "ship/iterate a feature," and this system iterates features on the keystone far more than it swaps embedders.

- PASS-THROUGH RISK ON THE MIDDLE LAYER. A's §A.6 #2 concedes the pressure for shallow core/ "service" classes that merely forward to a port (RecallService.fetch → store.by_content_hash) — APOSD Pass-Through Method / Shallow Module, the most common red flag. Requires ongoing discipline A cannot enforce structurally.

- THE PRODUCER DOESN'T FIT A LAYER. A's §A.6 #4 admits producer/ is simultaneously a driver (runs a loop, like serving/) and a core consumer; strict inward layering has to call it a peer layer and "wave at the asymmetry." Hexagonal models it correctly as a driving adapter on the same footing as MCP — evidence the hexagonal framing is the more honest fit for THIS system's shape, not just a stylistic preference.

What I did NOT hold against A (and why the synthesis was unnecessary): A's import-linter test (test_core_imports_no_adapters via AST walk) is a genuinely stronger purity enforcement than B's grep-lint, and I FOLDED IT INTO the winner rather than rejecting B. A's choice to keep native_dim/W_version visible at the embedder port (vs B hiding them) is also defensible for the reembed migration (§6.1 #4) — likewise folded in. So the only thing truly rejected is A's organizing AXIS (group-by-layer), not its individual good ideas.

B's own weaknesses were weighed and found non-fatal: (1) the wide EpisodeStore god-port (15+ methods, 4 responsibilities) is a real ISP smell, but it is a CONSCIOUS trade to keep the §12 single-writer transaction as one object, and it tears along clean method-group lines if ledgers ever migrate out — accepted, with the mitigation that the four method-groups are pre-segregated. (2) Purity-by-CI-lint-not-compiler is softer than a compiled module system — mitigated by importing A's AST import-linter as the enforcement (compiler-grade for Python's reality) plus the fakes making the pure path the path of least resistance. (3) Port-reshape cost if L2 counterfactual-replay arrives — explicitly scoped as a research bet (§8.1 L2), so an L1-shaped OutcomeProducer.reward_for port is correct for the MVP, not a defect. (4) Indirection floor for a first-time reader — mitigated by rigid domain/↔adapters/ parallel naming an agent navigates deterministically. None of these touches the keystone or a product invariant; A's scatter does both.

### Chosen design
# Hivemind v-min — Chosen Architecture: HEXAGONAL (Ports & Adapters)

**Winner: Approach B**, hardened with two ideas the contrast with A surfaced:
(1) A's **AST import-linter** replaces B's grep-lint as the purity gate (compiler-grade for Python);
(2) A's choice to keep `native_dim` / `W_version` visible on the embedder port is folded in (the reembed migration §6.1#4 needs them).

The package is `hive`. The organizing axis is **the domain verb, not the technical layer**: a pure core of four verbs (`admit`, `recall`, `fetch`, `credit`) over six Ports; all I/O in adapters; one composition root.

---

## 1. Module layout (the repo IS the agent's memory map)

```
hive/
├── domain/                      # PURE. CI-forbidden to import sqlite3 | torch | subprocess | os | git
│   ├── ports.py                 # the SIX Protocols — the entire contract surface
│   ├── models.py                # frozen dataclasses: Episode, StagedEpisode, Candidate,
│   │                            #   RecallResult, Reward, GitCommitFact, Family, ScanVerdict, UtilityPosterior
│   ├── recall.py                # RecallService.recall  — NEVER-HALLUCINATE lives here, by control flow
│   ├── admission.py             # AdmissionService.write/approve/reject — SECRET FLOOR + pending state machine
│   ├── credit.py                # CreditService + CreditSurfacer — ALL of move #6, one file
│   └── errors.py                # AbstainNoData, SecretRefused, NotApproved, …
├── adapters/                    # IMPURE. one Port impl per file, one outside-world import per file
│   ├── embedding_st.py          # EmbeddingProvider  ← bge-small + ProjectionHead.pca  (PORT+FLIP §10)
│   ├── index_exhaustive.py      # VectorIndex        ← exact cosine-kNN, AUTHORITATIVE (no approx flip §4.3)
│   ├── store_sqlite.py          # EpisodeStore       ← WAL + BEGIN IMMEDIATE + version/CAS (PORT+SIMPLIFY §10)
│   ├── producer_git.py          # OutcomeProducer    ← git CLI watcher (BUILD-NEW §11)
│   ├── scanner_regex.py         # SecretScanner      ← deterministic credential scan (BUILD-NEW §9)
│   └── clock_system.py          # Clock              ← time.time wrapper
├── app/                         # COMPOSITION ROOT — the only code that knows both sides
│   ├── container.py             # build_container(cfg) -> Container  (the ONLY swap-point switch)
│   ├── config.py                # HiveConfig: frozen group dataclasses + from_env(), fail-fast (§9 secrets)
│   ├── mcp_server.py            # MCP transport: hive_* tools → domain verbs (thin, no logic)
│   └── producer_loop.py         # in-process poll/settle/DRAIN tick (§12 single-writer; Fix #1 lives here)
├── ops/                         # off the hot path
│   ├── migration.py             # reembed-from-text + one-time import (PORT+SIMPLIFY §10/§12)
│   ├── backup.py                # daily snapshot (PORT §10)
│   └── eval/                    # eval membrane, dev-time only (PORT as-is §10)
└── tests/
    ├── fakes/                   # FakeEmbedding, FakeIndex, FakeStore, FakeProducer, FakeScanner, FakeClock
    ├── unit/                    # domain tested against fakes ONLY — milliseconds, no model/SQLite/git
    ├── contract/               # PortContractTests run against {fake, real} — the exactness/exact-cosine proof
    ├── acceptance/             # §6.1 gates end-to-end on a real store
    └── test_purity.py          # AST import-linter (from A) — domain/ may not import I/O
```

Agent navigation is deterministic: "where is never-hallucinate?" → `domain/recall.py`. "where is move #6?" → `domain/credit.py`, one file. "swap the embedder?" → `adapters/embedding_st.py` + one line in `container.py`. The impl of any `XPort` is always `adapters/<x>_*.py`.

---

## 2. The six Ports — `hive/domain/ports.py` (entire contract surface)

```python
from __future__ import annotations
from typing import Optional, Protocol, Sequence, runtime_checkable
import numpy as np
from hive.domain.models import (
    Episode, StagedEpisode, Reward, GitCommitFact, Family, ScanVerdict, UtilityPosterior,
)

# ── SWAP AXIS 1 — embedder + PCA head folded into one value-producing port ──
@runtime_checkable
class EmbeddingProvider(Protocol):
    d: int                 # PROJECTED width = 256 (the value dim callers see)
    native_dim: int        # 384 pre-PCA (folded in from A — reembed migration needs it, §6.1#4)
    W_version: int         # bump on any tier-D change → triggers reembed (§4.1)
    def encode(self, text: str) -> np.ndarray: ...                     # -> float32[d], L2-normalized
    def encode_batch(self, texts: Sequence[str]) -> np.ndarray: ...    # -> float32[n, d]
    def encode_native_batch(self, texts: Sequence[str]) -> np.ndarray: ...  # -> float32[n, native_dim], for reembed

# ── SWAP AXIS 2 — exact cosine-kNN, AUTHORITATIVE-exhaustive (§4.3 anti-trap) ──
@runtime_checkable
class VectorIndex(Protocol):
    is_exact: bool                                                     # contract-tested True; HNSW would be False
    def add(self, episode_id: int, value: np.ndarray) -> None: ...
    def remove(self, episode_id: int) -> None: ...
    def search(self, query: np.ndarray, k: int) -> list[tuple[int, float]]: ...  # (episode_id, cosine), EXACT best-first
    def __len__(self) -> int: ...

# ── I/O — durable episodes + 3 ledgers + blobs + CAS. Single-writer (§12). ──
# Deliberately wide (the conscious compromise): keeps the §12 single-writer txn as one object.
@runtime_checkable
class EpisodeStore(Protocol):
    # episodes
    def stage(self, ep: StagedEpisode) -> int: ...                    # status=pending; returns id
    def approve(self, ids: Sequence[int], approver: str, now: int) -> list[int]: ...
    def reject(self, ids: Sequence[int]) -> list[int]: ...
    def get(self, episode_id: int) -> Optional[Episode]: ...
    def pending(self, since: Optional[int]) -> list[StagedEpisode]: ...
    def recall_scan(self) -> list[Episode]: ...                       # status='approved' ONLY (server-enforced §6.1#5b)
    def by_content_hash(self, h: bytes) -> Optional[Episode]: ...     # fetch + dedup
    # content blob (content-addressed)
    def put_blob(self, content: bytes) -> bytes: ...                  # -> sha256 digest
    def get_blob(self, content_hash: bytes) -> Optional[bytes]: ...
    # ledgers (move #6)
    def record_exposure(self, trace_id: str, rows: Sequence[tuple[int, float]]) -> None: ...  # (episode_id, recall_margin)
    def exposed_for(self, trace_id: str) -> list[tuple[int, float]]: ...
    def link_task(self, trace_id: str, fact: GitCommitFact, family: Family, settle_at: int) -> None: ...
    def due_settlements(self, now: int) -> list[str]: ...             # task_refs whose settle_at passed
    def settle(self, task_ref: str, reward: float) -> None: ...
    def clawback(self, task_ref: str, reward: float) -> None: ...
    def update_posterior(self, episode_id: int, family: Family, dwins: float, dlosses: float, source: str) -> None: ...
    def posterior(self, episode_id: int, family: Family) -> Optional[UtilityPosterior]: ...
    # meta + CAS (single-writer §12)
    def meta_get(self, key: str) -> Optional[str]: ...
    def meta_set(self, key: str, value: str) -> None: ...

# ── SWAP AXIS 3 — verifiable git/CI facts; the ONLY credit source (verifiable-credit-only) ──
@runtime_checkable
class OutcomeProducer(Protocol):
    def poll(self, since: int) -> list[GitCommitFact]: ...
    def reverts_of(self, sha: str) -> list[GitCommitFact]: ...
    def reward_for(self, fact: GitCommitFact) -> Optional[Reward]: ...        # §4.7 schedule
    def blame_overlap(self, fix: GitCommitFact, original_sha: str) -> bool: ... # Decision B clawback precision
    def resolve_squash(self, sha: str) -> str: ...                            # squash-merge join survival §11

# ── I/O — secret floor, deterministic, runs BEFORE stage (§9) ──
@runtime_checkable
class SecretScanner(Protocol):
    def scan(self, text: str) -> ScanVerdict: ...   # ScanVerdict(action: clean|redact|refuse, redacted_text, matched_kinds)

# ── I/O — wall clock as a port so settle_days windows are deterministically testable ──
@runtime_checkable
class Clock(Protocol):
    def now(self) -> int: ...
```

---

## 3. Domain core — invariants enforced by control flow

### 3.1 `domain/recall.py` — never-hallucinate is unfakeable here
```python
class RecallService:
    def __init__(self, embed: EmbeddingProvider, index: VectorIndex, store: EpisodeStore,
                 gate: NormalizedEntropyGate, clock: Clock, surfacer: CreditSurfacer,
                 recall_top_n: int = 10):
        assert index.is_exact, "v-min requires the authoritative-exhaustive index (§4.3)"

    def recall(self, query: str, family: Optional[Family]) -> RecallResult:
        q = self.embed.encode(query)                              # port
        hits = self.index.search(q, self.recall_top_n)            # port, EXACT
        if not hits:
            return RecallResult.empty()                           # EMPTY_NO_DATA
        scored = self.surfacer.reweight(hits, family)             # cos × f(utility), pure
        suppress, h_norm, margin = self.gate.evaluate(scored.candidates)
        if suppress:
            self._log_abstain(query, h_norm, margin)
            return RecallResult.abstain(h_norm)                   # best=None — FUNCTION ENDS. nothing to resurrect.
        trace_id = new_trace_id()
        episodes = [self.store.get(eid) for eid, _ in scored.top]
        self.store.record_exposure(trace_id, scored.margins)      # only AFTER confident
        return RecallResult.confident(trace_id, episodes, h_norm)
```
The abstain branch returns before the resolve/exposure step; there is no later stage in the function, so a refused query has nowhere to be rescued (§6.1#3 abstain-no-resurrect, product invariant #1).

### 3.2 `domain/admission.py` — secret floor before stage
```python
class AdmissionService:
    def write(self, text, weight, source, proposed_by) -> WriteResult:
        verdict = self.scanner.scan(text)                         # FLOOR — before anything persists
        if verdict.action == "refuse":
            self._log_secret_refused(verdict); raise SecretRefused(verdict.matched_kinds)
        body = verdict.redacted_text if verdict.action == "redact" else text
        h = self.store.put_blob(body.encode())
        sid = self.store.stage(StagedEpisode(text=body, content_hash=h, weight=weight,
              source=source, proposed_by=proposed_by, status="pending", ts=self.clock.now()))
        return WriteResult(pending_id=sid, verdict=verdict)

    def approve(self, ids, approver) -> list[int]:
        admitted = self.store.approve(ids, approver, self.clock.now())
        for eid in admitted:                                      # index ONLY on approval
            self.index.add(eid, self.store.get(eid).value)        # pending rows never enter the index
        return admitted
```
Two independent enforcements of §6.1#5b: `recall_scan()` is approved-only AND the index is populated only here.

### 3.3 `domain/credit.py` — ALL of move #6, one file
`CreditService.link` (window-primary, stamp-override, derive_family from git facts), `.settle_due` (settlement sweep), `.apply_outcome` (drain one Reward, split by recall_margin, Beta-Bernoulli posterior), and `CreditSurfacer.reweight` (`f ∈ [0.5,1.5]`, demotes only when posterior CI excludes 0 — un-cripples the verified `1.0+max(0,u)` floor in service.py:184). Consumes typed `Reward`/`GitCommitFact`, writes typed posteriors — zero `subprocess`/`sqlite3`/`time.time()`.

---

## 4. Swap mechanism — one switch, `app/container.py`
```python
def build_container(cfg: HiveConfig) -> Container:
    embed    = make_embedding(cfg.embedding)   # st | openai | e5            ← swap axis 1
    index    = make_index(cfg.index)           # exhaustive | hnsw           ← swap axis 2
    producer = make_producer(cfg.producer)     # git | github_api            ← swap axis 3
    store    = SqliteStore(cfg.store.path)
    scanner  = RegexSecretScanner(cfg.security.patterns)
    clock    = SystemClock()
    credit   = CreditService(store, cfg.utility)
    return Container(
        recall    = RecallService(embed, index, store, NormalizedEntropyGate(cfg.recall.h_frac_max),
                                  clock, CreditSurfacer(store, cfg.utility), cfg.recall.recall_top_n),
        admission = AdmissionService(scanner, store, index, clock),
        credit    = credit,
        loop      = ProducerLoop(producer, store, credit, clock, cfg.producer),  # Fix #1: drain on producer tick
    )
```
The `make_*` factories are the ONLY code that pattern-matches a provider string. Core sees only Ports. HNSW at scale is a new adapter that must pass `PortContractTests` (it can't, on `is_exact`, until the recall path explicitly opts into approximate mode) — the §4.3 approx_threshold landmine cannot re-enter through a config knob.

---

## 5. Failure-mode logging points (global standard §6, structured JSON)
- `embedding_st.py`: model load start/success/failure (boundary); reembed batch progress (§6.1#4).
- `index_exhaustive.py`: WARN if `search` called with k > len; assert is_exact at construction.
- `store_sqlite.py`: every `BEGIN IMMEDIATE` retry (backoff attempt n), CAS version conflict, WAL checkpoint; ERROR on SQLite I/O with episode_id context.
- `domain/recall.py`: abstain decision (query hash, h_norm, margin) at INFO; empty-no-data at DEBUG.
- `domain/admission.py`: secret-refused (matched_kinds, NEVER the secret) at WARN; approve/reject milestones at INFO.
- `domain/credit.py`: provisional+ logged, settle, clawback (with blame-overlap=true/false), posterior update at INFO; stamp-hit-rate + credit-density counters (Phase-2 readiness gate §12).
- `producer_git.py`: empty watch_repos ⇒ WARN "producer idle, loop starved not broken" (§4.8); git CLI failure ⇒ ERROR with repo + since_ts; squash-resolve fallback at DEBUG.
- `app/producer_loop.py`: tick start/end, n_settled, n_clawed, drain watermark advance at INFO.
- No secret/PII ever written to store or logs; episode_ids and content_hash hex only.

---

## 6. Test-first verification (TDD mandate)

**Fakes** (~30–50 lines each, deterministic, no I/O): FakeEmbedding (hash→unit vec), FakeIndex (dict+numpy cosine, is_exact=True), FakeStore, FakeProducer (scripted commits/reverts/blames), FakeScanner (programmable verdicts), FakeClock (`advance(days=)`).

**Failing tests written before implementation** — file::test, assertion, failure caught, mutation:
- `test_purity.py::test_domain_imports_no_io` — AST-walk every `hive/domain/*.py`, assert no `sqlite3|torch|subprocess|os|git`. *Mutation:* `import sqlite3` into credit.py → red. (Catches: purity erosion / ball of mud.)
- `contract/test_ports.py::test_{embedder,index,store,producer,scanner}_protocol[real,fake]` — isinstance vs runtime_checkable Protocol. (Catches: a swap candidate that fails the seam.)
- `contract/test_index.py::test_index_is_exact[FakeIndex,ExhaustiveIndex]` — same assertion proves the REAL index exact at N≥10k. (Catches: §4.3 approx landmine; an HNSW adapter that can't pass can't be wired.)
- `unit/test_recall.py::test_abstain_returns_empty_no_best` — gate suppress ⇒ best is None, survivors empty. *Mutation:* gate `>`→`<` (verified real in gate_bundle.py) → red. (§6.1#3.)
- `unit/test_recall.py::test_abstain_no_resurrect` — suppressed recall is never mutated to non-None by any path. (BUG-001 / invariant #1.)
- `unit/test_recall.py::test_empty_index_abstains_not_raises` — clean RecallResult.empty() on len 0.
- `unit/test_admission.py::test_secret_refused_never_staged` — refuse ⇒ `fake_store.stage` call count == 0, blob never written. *Mutation:* move `stage()` before `scan()` → red. (§6.1#5a.)
- `unit/test_admission.py::test_pending_never_in_index` — after write fake_index empty; after approve eid present. (§6.1#5b.)
- `unit/test_admission.py::test_recall_scan_excludes_pending` — recall_scan omits pending. *Mutation:* read all rows → red.
- `unit/test_credit.py::test_credit_split_by_margin` — margins 0.7/0.3 ⇒ wins 0.7R/0.3R. *Mutation:* flat +1-to-all → red. (§2 co-occurrence discount.)
- `unit/test_credit.py::test_surfacer_can_demote` — confident-negative posterior ⇒ f<1. *Mutation:* keep `1+max(0,u)` → red. (§10 PORT+FIX.)
- `unit/test_credit.py::test_utility_inert_until_ci_confident` — wide CI ⇒ f==1.0. (§4.7.)
- `unit/test_credit.py::test_provisional_settles_after_settle_days` — FakeClock.advance(days=7) flips 0→+0.2.
- `unit/test_credit.py::test_revert_claws_back` and `::test_bugfix_blame_overlap_claws_back` — large −.
- `unit/test_credit.py::test_coincidental_same_file_NO_clawback` — same file, no blame overlap ⇒ NO loss. *Mutation (MANDATED §6.1#6):* disable blame_overlap (clawback on same-file) → red. (The expensive false-positive direction.)
- `unit/test_credit.py::test_squash_merge_join_survives` — resolve_squash keeps trace+blame target. (§11.)
- `unit/test_loop.py::test_drain_fires_on_producer_tick_no_consolidation` — drain runs on ProducerLoop tick with no consolidation timer. *Mutation:* move drain into a (nonexistent) consolidate → red. (Fix #1, §4.4 — verified: today the drain sits in service.py:942's consolidation tick.)
- `acceptance/test_migration.py::test_wversion_bump_reembeds_and_reproduces_recall` — bump W_version, reembed-from-text via encode_native_batch, recall@5 within CI of pre-bump. (§6.1#4.)

`unit/` runs in milliseconds with no model, no SQLite, no git — so the never-hallucinate gate, secret floor, and the entire move-#6 credit math are provable on every commit, which is exactly what the first-class-test mandate and the §6.1/§6.6 gates demand.

**Open questions:**
- EpisodeStore is a deliberately wide god-port (15+ methods, 4 responsibilities) fused to keep the §12 single-writer transaction as one object — confirm this stays acceptable, or split into EpisodeStore/BlobStore/ExposureLedger/UtilityLedger if/when ledgers ever move to a separate DB (the method-groups are pre-segregated so it tears cleanly).
- Purity is enforced by an AST import-linter test (test_purity.py, imported from A's design), not the language — confirm CI runs it as a blocking gate so domain/ purity cannot silently erode.
- The OutcomeProducer.reward_for(fact)->Reward port is L1-shaped on purpose (§8.1); if the §6.6 keystone reveals L2 counterfactual-replay is needed, that port + CreditService need a real reshape — accepted as a scoped research-bet boundary, but flag it in the design doc's known-limits.
- Clock is one of six ports purely for test determinism (settle_days windows); it is 4 lines and earns its keep in test_credit.py — confirm we are comfortable keeping it a port rather than inlining time.time().

---

## D2. Embedding provider swap seam for Hivemind v-min — where to draw the port/interface boundary so the embedding PROVIDER swaps via config/adapter with no core refactor, while preserving never-hallucinate, hot-path no-network, and TDD-first mandates.

- **Option A:** Approach A: Port the reference `TextEmbedder` Protocol + `auto_embedder` factory as-is (trimmed to the locked geometry). The seam is the model wrapper: `embed(text) -> float[d]`, `embed_batch`, `embed_native_batch`, with `d`/`native_dim` attributes. Default binding = trimmed `SentenceTransformerEmbedder` (bge-small + PCA head); remote provider = sibling class satisfying the same Protocol, injected at the composition root. Keeps the reference shape verbatim including the lazy PCA fit inside the ST embedder.
- **Option B:** Approach B: Raise the seam one level up to an `EmbeddingProvider` port that owns the ENTIRE value-encode chain (`embed -> pca -> normalize`) and returns the persisted `value[d]`. PCA head is fit OFFLINE/frozen (passed into the adapter, never lazily fit on the hot path), versioned by `w_version`. Port surface = `encode`, `encode_batch`, `encode_native_batch` (migration-only), `health`, plus `d`/`native_dim`/`provider_id`/`w_version`. Two adapters: `LocalSentenceTransformerProvider` (default, baked, in-process) and a contract-only `RemoteLoopbackProvider` (loopback-only rail, built later). Factory + one config key (`embedding.transport`) is the whole swap.
- **Winner:** B — Approach B wins on the single most load-bearing axis for this store — information hiding vs leakage — and it wins because of a defect I verified in the actual port source, not a rhetorical flourish.

VERIFIED DEFECT (the deciding fact). In `embedder.py`, the reference seam Approach A ports verbatim leaks its projection policy across the boundary in a call-order-dependent way:
- `embedder.py:289-299`: `SentenceTransformerEmbedder.embed(text)` on the `pca` path, when no batch has been seen yet, FALLS BACK to `ProjectionHead.random(...)` and assigns it to `self.projection` permanently.
- `embedder.py:301-306`: `embed_batch(texts)` calls `_ensure_pca(E)` which fits a real `ProjectionHead.pca(...)` head on the first batch.
Therefore the projection an episode receives depends on whether `embed()` or `embed_batch()` ran first. The spec mandates capture and recall use the SAME encode chain (HIVEMIND_VMIN_SPEC.md:91), and `hive_write` stages one insight at a time (per-insight `embed`) while recall can batch — so the reference seam can hand capture a random projection and recall a PCA projection of the same text. That is a latent split-brain in a never-hallucinate store: two geometries silently co-mingling under one `W_version`. Approach A inherits this verbatim; its own honest-weaknesses section does not even name it. Approach B's entire thesis is built to kill exactly this — frozen offline fit, no lazy fit, no random fallback, `encode(t) == encode_batch([t])[0]` as a mutation-tested invariant.

RUBRIC SCORING (principles.md / red-flags.md / agent-native.md):
- Interface depth: B is deeper. A's surface returns `float[d]` but lets the projection policy (lazy fit / random fallback) escape — the caller cannot tell whether a returned vector is PCA- or random-projected. That is textbook information leakage (red-flags.md) and a shallow module masquerading as deep. B encloses the policy; the surface (`encode -> value[d]`) is the same width but the contract behind it is genuinely complete.
- Cognitive load on callers: B lower. A caller of A must understand the native(384)/projected(256) distinction AND the call-order fit semantics to use it safely. B's hot-path caller sees only `encode -> value[d]`; `native_dim`/`encode_native_batch` are explicitly fenced to the migration, the one place that legitimately needs them.
- Special-vs-general split: B is cleaner. The migration-only native path is named and isolated; the hot path is the general case. A mixes the migration seam and the hot path on the same flat surface.
- Enforced-not-prose contract: both ship Protocols + Test Contracts, but B's contract pins the invariant that actually matters (`encode == encode_batch[0]`, unit-norm, no-lazy-fit, dim==geometry.d) as parametrized tests run against BOTH the fake and the real adapter, plus a mutation matrix where each fault maps to a named red test. A's Test Contract is solid but cannot catch the split-brain because A keeps the lazy-fit behavior — there is no test that can pass while the behavior is present and also forbid the behavior.
- Swappability mandate: both satisfy zero-core-refactor. B is marginally better because selection is one config key (`embedding.transport=local|loopback`) routed through a fail-fast factory, whereas A's cross-family swap is instance-injection only (config can only reach the in-family model change). Both are honest that a remote URL is wiring not a tunable; B additionally adds a loopback-only rail that operationalizes the hot-path no-network invariant (HIVEMIND_VMIN_SPEC.md:56) as an `__init__`/`health` assertion with a named mutation test.
- Agent-navigability: B's `hive/ports/embedding.py` + `hive/adapters/embedding/{local_st,remote_loopback,factory}.py` layout makes the seam a discrete directory an agent can locate; the port file is the single contract surface. A keeps everything in one `embedder.py` with five classes (two of which — OpenAI, the auto fallback — must be trimmed), which is denser to navigate and carries the over-trim footgun A itself flags (losing the test fake).

The clinching argument: in a never-hallucinate store, the correctness of EVERY downstream component (cosine-kNN ranker C3, normalized-entropy gate C4, the value BLOB in C5) rests on the value being unit-norm and produced by one stable chain. B turns the spec's `:91` "same encode chain" diagram into a typed, mutation-tested contract and deletes a verified bug; A faithfully ports a seam that is the right SHAPE but carries that bug. Fidelity to a flawed reference is not a virtue when the flaw is a silent geometry split.
- **Rejected because:** Approach A rejected primarily because it ports `embedder.py:289-306` verbatim, inheriting the call-order-dependent lazy-PCA-fit / random-fallback split-brain — a real defect (verified in source, not hypothesized) that directly threatens the spec's `:91` same-encode-chain invariant and the never-hallucinate guarantee, and which A's own honest-weaknesses list fails to surface. Secondary rejections:
(1) A's seam leaks projection policy across the boundary (the caller cannot tell PCA from random projection) — information leakage per red-flags.md, making it a shallower module than its narrow surface suggests.
(2) A exposes the migration-only native(384) path on the same flat surface as the hot path with no special/general fence, inviting the exact native/projected conflation A admits is a sharp edge.
(3) A's "swap via config" only reaches in-family model changes; cross-family is instance-injection — same end result as B but A presents it as the headline win when it is actually the narrower mechanism.
(4) The trim itself is risky (drop the auto/openai branches but PRESERVE HashingNgramEmbedder as the test fake) — A names this footgun; B avoids it by writing a purpose-built `FakeProvider` against the port rather than depending on a class that must survive a deletion pass.

What was SALVAGED from A into the chosen design (so the rejection is not total): A's correct insistence that the seam is a `runtime_checkable Protocol` at the type boundary (kept); A's `native_dim` vs `d` distinction as load-bearing for migration (kept, but fenced to `encode_native_batch`); A's observation that the swap seam IS the test seam — the fake lets the whole store/ranker/gate suite run at hash-speed offline (kept and strengthened: B's `FakeProvider` emits the final d-dim value directly so it conforms to the same port the real adapter does); A's honesty that a remote URL belongs in wiring not the frozen config schema (kept). A's PORT+FLIP framing against spec `:728-729` is also correct and is preserved — B is still a PORT of the same `ProjectionHead.pca` and ST wrapper, just with the lazy-fit branch deleted and the boundary raised.

### Chosen design
# Chosen Design — Embedding Provider Swap Seam (`EmbeddingProvider` port, value-chain boundary)

**Winner: Approach B, with A's good parts folded in.** The seam is a port that owns the entire `embed -> pca -> normalize` chain and returns the persisted `value[d]`. The PCA head is fit offline and frozen; the hot path never lazily fits and never falls back to a random head. Default adapter is in-process/baked; a remote sidecar is a later adapter behind a loopback-only rail. Swap = factory + one config key.

This realizes: SWAPPABILITY MANDATE (provider behind a port), never-hallucinate (one stable unit-norm chain feeds ranker+gate), hot-path no-network (default in-process; remote rail is loopback-only), and TDD-first (every invariant is a parametrized test + a named mutation).

---

## 1. The port (the single contract surface)

```python
# hive/ports/embedding.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Protocol, runtime_checkable
import numpy as np

Value = np.ndarray  # shape (d,), dtype float32, L2-normalized: ‖v‖₂==1 (all-zeros ONLY for empty text)

@runtime_checkable
class EmbeddingProvider(Protocol):
    """text → value[d]: the SINGLE encode chain (embed → pca → normalize) used by
    BOTH capture and recall (HIVEMIND_VMIN_SPEC.md:91). Implementations own model load
    + projection internally; callers see only d-dim normalized values.

    Determinism: same text MUST yield the same value within a (provider_id, w_version),
    byte-stable enough that cos(encode(x), encode(x)) == 1.0. NO lazy fit, NO random
    fallback — two calls in any order are identical (kills the reference embedder.py:289-306
    call-order split-brain).

    Hot path (capture/recall/fetch): NO network egress (spec:56). A remote adapter may
    talk ONLY to loopback (UDS / 127.0.0.1) to a co-located sidecar.
    """
    d: int            # projected/value dim — MUST == config.geometry.d (256). Store+ranker+gate size on this.
    native_dim: int   # pre-projection dim (384 for bge-small). Used ONLY by migration.
    provider_id: str  # stable identity, e.g. "st:BAAI/bge-small-en-v1.5#pca". Mismatch ⇒ refuse cross-provider recall.
    w_version: int    # geometry.W_version of the frozen PCA head. Mismatch with store ⇒ migration required.

    def encode(self, text: str) -> Value:
        """One text → one d-dim L2-normalized value. HOT PATH. No lazy fit, no fallback."""
        ...

    def encode_batch(self, texts: list[str]) -> np.ndarray:
        """(n,) → (n, d) row-normalized values, SAME chain as encode().
        INVARIANT: encode(t) elementwise-equals encode_batch([t])[0]."""
        ...

    def encode_native_batch(self, texts: list[str]) -> np.ndarray:
        """(n,) → (n, native_dim) PRE-projection normalized embeddings. MIGRATION-ONLY.
        The PCA head is NOT applied. The re-embed-from-text migration fits a fresh
        native→d head through this (spec:738, :872; ref embedder.py:308). Hot path never calls it."""
        ...

    def health(self) -> "ProviderHealth":
        """Liveness + identity probe. Local: model loaded, head fit, dims agree.
        Remote: loopback reachable, remote provider_id/w_version match ours.
        Called at startup (fail-fast) and by hive_health. NEVER on the hot path."""
        ...

@dataclass(frozen=True)
class ProviderHealth:
    ok: bool
    provider_id: str
    d: int
    native_dim: int
    w_version: int
    detail: str   # human-readable; logged, never trusted as control flow
```

**Invariants the type asserts / the tests pin (cannot lie):**
1. `encode(t).shape == (d,)` and `d == geometry.d == 256`.
2. `abs(norm(encode(t)) - 1.0) < 1e-5` for non-empty `t` (cosine ranker + entropy gate assume unit norm).
3. `encode(t) == encode_batch([t])[0]` (capture path == recall path).
4. `encode` performs no lazy fit and no random fallback; order-independent.
5. `provider_id` / `w_version` immutable post-construction and travel with every written `value` via the store geometry header.

---

## 2. The frozen PCA head (ports `ProjectionHead.pca`, deletes the lazy/random branches)

```python
# hive/adapters/embedding/head.py
@dataclass(frozen=True)
class FrozenPcaHead:
    """Read-only native→d PCA projection. ONLY constructed by ProjectionHead.pca(fit_samples, d_out)
    at BUILD/IMPORT time (spec:871 corpus import fits it), serialized (W, d_in, d_out, version)
    into the image or the store geometry blob, reloaded read-only. The reference's lazy first-batch
    fit (embedder.py:281-287) and random fallback (embedder.py:293-296) are NOT ported."""
    W: np.ndarray   # (d_out, d_in) float32
    d_in: int       # 384
    d_out: int      # 256
    version: int    # == w_version

    def apply(self, e: np.ndarray) -> np.ndarray:        # (d_in,) → (d_out,) unit-norm
        out = self.W @ e.astype(np.float32)
        n = float(np.linalg.norm(out))
        return (out / n).astype(np.float32) if n > 0 else out.astype(np.float32)

    def apply_batch(self, E: np.ndarray) -> np.ndarray:  # (n, d_in) → (n, d_out) row-norm
        out = (self.W @ E.astype(np.float32).T).T
        norms = np.linalg.norm(out, axis=1, keepdims=True); norms[norms == 0] = 1.0
        return (out / norms).astype(np.float32)
```

The PCA fit math is the verbatim port of `embedder.py:74-112` (eigendecomposition of sample covariance, top-`d_out` unit eigenvectors). Only the LAZY/RANDOM plumbing around it is dropped.

---

## 3. Adapters

### 3a. Local in-process adapter (DEFAULT, baked, hot path)

```python
# hive/adapters/embedding/local_st.py
class LocalSentenceTransformerProvider:  # implements EmbeddingProvider
    def __init__(self, model_name: str, head: FrozenPcaHead, d: int):
        import logging; self._log = logging.getLogger("hive.embedding.local_st")
        from sentence_transformers import SentenceTransformer   # baked dep
        try:
            self._model = SentenceTransformer(model_name)        # loaded once, frozen (spec:869)
        except Exception as e:
            self._log.error("model_load_failed", extra={"model": model_name, "error": repr(e)})
            raise
        self._head = head                                        # FIT OFFLINE, passed in — never fit here
        self.native_dim = self._model.get_sentence_embedding_dimension()  # 384
        self.d = d                                               # 256
        self.provider_id = f"st:{model_name}#pca"
        self.w_version = head.version
        if head.d_in != self.native_dim or head.d_out != self.d:
            self._log.error("head_geometry_mismatch",
                extra={"head": f"{head.d_in}->{head.d_out}", "model": f"{self.native_dim}->{d}"})
            raise ValueError(f"head {head.d_in}->{head.d_out} != model {self.native_dim}->{d}")
        self._log.info("provider_ready", extra={"provider_id": self.provider_id,
            "d": self.d, "native_dim": self.native_dim, "w_version": self.w_version})

    def encode(self, text: str) -> Value:
        e = self._model.encode([text], normalize_embeddings=True)[0].astype(np.float32)  # (384,)
        return self._head.apply(e)                               # (256,) — deterministic, no fallback

    def encode_batch(self, texts: list[str]) -> np.ndarray:
        E = self._model.encode(texts, normalize_embeddings=True).astype(np.float32)
        return self._head.apply_batch(E)

    def encode_native_batch(self, texts: list[str]) -> np.ndarray:   # migration only
        return self._model.encode(texts, normalize_embeddings=True).astype(np.float32)

    def health(self) -> ProviderHealth:
        try:
            probe = self.encode("healthcheck")
            ok = probe.shape == (self.d,) and abs(float(np.linalg.norm(probe)) - 1.0) < 1e-5
            return ProviderHealth(ok, self.provider_id, self.d, self.native_dim, self.w_version,
                                  "ok" if ok else "encode probe failed shape/norm")
        except Exception as e:
            self._log.error("health_probe_failed", extra={"error": repr(e)})
            return ProviderHealth(False, self.provider_id, self.d, self.native_dim, self.w_version, repr(e))
```

### 3b. Remote loopback adapter (CONTRACT + STUB + TESTS only; built later per LOCKED DECISIONS)

```python
# hive/adapters/embedding/remote_loopback.py
class RemoteLoopbackProvider:  # implements EmbeddingProvider
    """Talks to a co-located embedder sidecar over LOOPBACK ONLY (UDS preferred, else 127.0.0.1:PORT).
    Same encode chain executed in the sidecar (e.g. GPU bge-large). Selected by embedding.transport=loopback.
    Wire: POST {endpoint}/encode {texts:[str]} -> {values: float32[n][d], provider_id, w_version};
          /encode_native (migration), /identity, /healthz. Sidecar owns the head → wire never carries
          384-dim except on /encode_native.
    HARD RAILS (enforced in __init__, re-checked in health()):
      - endpoint MUST be a UDS path OR 127.0.0.1/::1 — any other host raises at startup (preserves spec:56).
      - remote provider_id/w_version (via /identity) MUST match configured geometry — else fail-fast."""
    def __init__(self, endpoint: str, expect_id: str, expect_w: int):
        import logging; self._log = logging.getLogger("hive.embedding.remote_loopback")
        if not _is_loopback(endpoint):
            self._log.error("non_loopback_endpoint_rejected", extra={"endpoint": endpoint})
            raise ValueError(f"endpoint must be loopback/UDS, got {endpoint!r}")  # hot-path egress guard
        # ... /identity fetch, mismatch fail-fast, log provider_ready ...
```

---

## 4. Swap mechanism (factory + one config key)

```python
# hive/adapters/embedding/factory.py
def build_provider(cfg: EmbeddingConfig, head_store: HeadStore) -> EmbeddingProvider:
    import logging; log = logging.getLogger("hive.embedding.factory")
    head = head_store.load_frozen_head(cfg.w_version)   # offline-fit head; fail-fast if absent
    if cfg.transport == "local":
        return LocalSentenceTransformerProvider(cfg.st_model_name, head, d=cfg.d)
    if cfg.transport == "loopback":
        return RemoteLoopbackProvider(cfg.endpoint, expect_id=_expected_id(cfg), expect_w=cfg.w_version)
    log.error("unknown_transport", extra={"transport": cfg.transport})
    raise ValueError(f"unknown embedding.transport={cfg.transport!r}")   # fail-fast, never default
```

**Config keys (extend the spec `embedding.*` block, all tier-D):**

| Key | Default | Notes |
|---|---|---|
| `embedding.transport` | `local` | `local` (baked, in-process) \| `loopback` (sidecar). The swap switch. |
| `embedding.st_model_name` | `BAAI/bge-small-en-v1.5` | spec:260 |
| `embedding.st_projection_head` | `pca` | spec:257; only head shipped |
| `embedding.endpoint` | `null` | UDS path or `127.0.0.1:PORT`; required iff `transport=loopback`; validated loopback-only |
| `geometry.d` | `256` | port `d`, spec:258 |
| `geometry.W_version` | bump on tier-D | spec:262; travels with every `value`, gates migration |

The store (`episodes.value` BLOB), the dense cosine-kNN ranker (C3), the normalized-entropy gate (C4), and the migration consume only `EmbeddingProvider` — none name a concrete adapter. That is the structural "no core refactor" guarantee.

---

## 5. Failure-mode logging points (per global standards §6)

- `model_load_failed` (error): sentence-transformers load throws — model name, repr(error). Boundary failure.
- `head_geometry_mismatch` (error): head dims != model dims at construction — fail-fast before any encode.
- `provider_ready` (info): success checkpoint — provider_id, d, native_dim, w_version.
- `health_probe_failed` (error): encode probe raised or failed shape/norm.
- `non_loopback_endpoint_rejected` (error): remote endpoint is not loopback — hot-path egress guard.
- `remote_identity_mismatch` (error): sidecar `/identity` provider_id/w_version != configured — geometry-mix guard.
- `unknown_transport` (error): factory got an unconfigured transport — fail-fast.
- `missing_frozen_head` (error, in `HeadStore.load_frozen_head`): warm store booting on absent/mismatched PCA head.
Never log raw text, model weights, or endpoint credentials; log identifiers (provider_id, w_version) only.

---

## 6. Test-first verification (Test Contract — written BEFORE impl)

`FakeProvider` implements the port deterministically with NO ML deps (hash char-trigrams → 256-dim → L2-normalize, emitting the final value). It satisfies every invariant, so the store/ranker/gate/migration suites run fast and offline against the port. The real `LocalSentenceTransformerProvider` runs in a smaller dep-gated suite.

**Port-conformance (parametrized over BOTH `FakeProvider` and `LocalSentenceTransformerProvider`) — `tests/ports/test_embedding_contract.py`:**

| Test | Assertion | Failure it catches |
|---|---|---|
| `test_encode_dim_matches_geometry_d` | `encode("x").shape == (256,)` | wrong-dim adapter corrupting the value BLOB / ranker |
| `test_encode_is_unit_norm` | `abs(norm(encode("hello"))-1) < 1e-5` | un-normalized value → cosine ranker + entropy gate miscompute (never-hallucinate breach) |
| `test_encode_matches_batch` | `allclose(encode(t), encode_batch([t])[0])` | capture/recall split-brain (the embedder.py:289-306 bug) |
| `test_encode_deterministic_order_independent` | encode A; encode B; re-encode A == first A | re-introduced lazy/random fallback |
| `test_no_lazy_fit_no_random_fallback` | head identity + w_version stable across N encodes | re-introduced first-batch fit |
| `test_empty_text_is_safe` | `encode("")` finite (all-zeros allowed), no NaN/raise | empty-insight crash on capture |
| `test_native_batch_is_pre_projection` | `encode_native_batch(["x"]).shape[1] == 384 != 256` | migration reading already-projected vectors → corrupt re-embed |
| `test_provider_id_and_w_version_immutable` | attrs frozen; reassignment raises | geometry drift mixing value spaces |

**Adapter-specific:**

| Test | Assertion | Failure it catches |
|---|---|---|
| `test_local_rejects_head_geometry_mismatch` | 384→128 head with d=256 raises ValueError | stale head producing wrong-dim values |
| `test_remote_rejects_non_loopback_endpoint` | `endpoint="https://api.example.com"` raises at `__init__` | hot-path network egress (spec:56) |
| `test_remote_rejects_provider_id_mismatch` | sidecar `/identity` mismatch ⇒ fail-fast | mixing two geometries across the wire |
| `test_factory_unknown_transport_raises` | `transport="hash"` raises, never degrades | reference's silent hash fallback reaching the shipped path |
| `test_factory_missing_head_fails_fast` | `load_frozen_head(absent_version)` raises | warm store booting on missing PCA head |

**Mutation tests (RULE 2 — fault → named red test → restore → green):**

| Fault injected | Test that MUST go red |
|---|---|
| `FrozenPcaHead.apply` drops final `out/norm` | `test_encode_is_unit_norm` |
| `LocalSentenceTransformerProvider.encode` returns `e` (384) pre-head | `test_encode_dim_matches_geometry_d` |
| Re-introduce reference lazy random fallback in `encode` | `test_encode_matches_batch` + `test_encode_deterministic_order_independent` |
| `encode_native_batch` applies `self._head.apply_batch(E)` | `test_native_batch_is_pre_projection` |
| `RemoteLoopbackProvider.__init__` skips loopback check | `test_remote_rejects_non_loopback_endpoint` |
| Factory falls through to a default on unknown transport | `test_factory_unknown_transport_raises` |

---

## 7. What bolts on cleanly (the payoff)

- **GPU / bge-large / hosted provider:** write one `RemoteLoopbackProvider`, flip `embedding.transport`, bump `geometry.W_version`, run the existing re-embed migration. Core untouched (spec D4 escalation, :395).
- **Model upgrade in-family (bge-small → bge-large/E5):** refit `FrozenPcaHead` offline, bump `w_version`, re-embed via `encode_native_batch` + `ProjectionHead.pca` (also the one-time corpus import, spec:738/:872).
- **Reserved second "mechanism vector" embedding (spec:167):** a parallel `EmbeddingProvider` instance with its own head — the port is already the right unit to instantiate twice. Designed-not-built.

---

## 8. Why this beat Approach A (rejected-alternatives log entry)

Approach A ports `embedder.py` verbatim, including `embed()`'s lazy-PCA / random-fallback (lines 289-299) vs `embed_batch()`'s PCA fit (lines 301-306). I verified in source that this makes an episode's projection depend on call order — capture (`embed`, per-insight) can get a random head while recall (batchable) gets PCA, violating the spec's same-encode-chain invariant (:91) and silently co-mingling two geometries under one `W_version`. A's own honest-weaknesses section never names this. Chosen design deletes the lazy/random branches and pins `encode == encode_batch[0]` as a mutation-tested invariant.

Kept FROM A: the `runtime_checkable` Protocol-at-the-type-boundary framing; the `native_dim` vs `d` distinction as load-bearing for migration (but fenced to `encode_native_batch`); the insight that the swap seam IS the test seam (a fake lets the whole store/ranker/gate suite run offline at hash-speed); the honesty that a remote URL is wiring, not a frozen-config tunable; and the PORT+FLIP disposition against spec:728-729 (this is still a port of `ProjectionHead.pca` + the ST wrapper — just with the lazy branch removed and the boundary raised one level to enclose the projection policy).

**Open questions:**
- beta re-tune coupling (spec:259, :279): geometry.beta for the dense-cosine [-1,1] range must be re-tuned at the geometry change and is tier-B. The port boundary correctly excludes beta (it belongs to the gate C4, not the embedder), but the W_version bump that the embedder triggers and the beta re-tune that the gate needs are coupled at every tier-D migration — confirm the migration tooling re-runs the beta sweep, not just the re-embed.
- provider_id enforcement locus: the port exposes provider_id/w_version but enforcing 'all approved values were written by the same provider_id/w_version' requires the STORE (C5) to stamp and check a geometry header on every value. The port makes this cheap (one assertion) but cannot unilaterally guarantee it — confirm C5's design includes the geometry-header stamp/check so two geometries cannot co-mingle if a future migration is interrupted.
- batch-vs-single equivalence narrows provider choice: encode(t)==encode_batch([t])[0] holds for a linear PCA head + deterministic model, but a future instruction-tuned model that pools differently for batch vs single would fail the contract test (i.e. be rejected as an invalid provider). This is the intended trade for capture==recall, but it is a real constraint on the set of swappable models — flag it in the provider-swap runbook.
- loopback is policy not sandbox: the loopback-only rail is an __init__/health string assertion; a misconfigured sidecar that itself egresses to the internet defeats spec:56 from one layer down. True enforcement needs a network namespace / egress firewall on the sidecar container — out of v-min scope, but should be named in the deploy doc as the residual hole.
- cold-start ergonomics: frozen-offline fit means an empty store cannot self-bootstrap on first write — load_frozen_head fails fast until the build/import fit step has run. For the spec flow this is correct (corpus import fits the head, :871), but confirm the import.sh / first-run path fits the head BEFORE the server accepts the first hive_write, or document the two-step (fit head → serve) explicitly.

---

## D3. Embedding storage / vector index swap seam for Hivemind v-min C5 Store + C3 ranker.

- **Option A:** APPROACH A — Single MemoryStore: SQLite owns durable rows + verbatim blobs + the exhaustive cosine scan as ONE object, ONE lock, ONE transaction domain; the vector scan is a PRIVATE method selected by a backend enum, with no public index mutation surface.
- **Option B:** APPROACH B — Split EpisodeStore (durable source of truth) from a swappable VectorIndex Protocol (derived cache), joined by rebuild_from_store() and the sole coupling store.scan_approved(); never-flip promoted to a typed is_authoritative boolean the ranker asserts.
- **Winner:** synthesis — The spec is double-signaled (§2/C5 co-locates the index inside the Store and §10 lists Store as one port — favoring A; while C3 ranker is a separate port that already calls candidates()/search() on an index object, and the SWAPPABILITY MANDATE + §4.3's typed authoritative fix favor B). The synthesis resolves it by taking A's single-consistency-domain win (the durable Store is the SOLE mutator of the index, calling add/remove inside the same write lock/tx that flips status, so the table and the searchable set can never diverge) AND B's typed-swappable-port win (the index is a VectorIndex Protocol fed only by scan_approved, with is_authoritative making never-flip a type-level assertion and rebuild_from_store making it a derived cache). Callers get no index mutation verb at all — a deeper module than B (no add/remove leak that produces B's own confessed drift window) and a more swappable, less-coupled seam than A (external-storage swap becomes an adapter, not a Store refactor; the C3 ranker stays a separate component as §10 requires).
- **Rejected because:** Pure A rejected: its private-method scan makes external vector storage (pgvector/Qdrant) a MemoryStore refactor, violating the SWAPPABILITY MANDATE's 'no core refactor', and folds the C3 ranker into the C5 Store though §10 keeps them separate ports and the existing ranker already calls a method on an index object; its never-flip guarantee rests on reviewer discipline rather than a type. Pure B rejected: a free-standing index with public add/remove and its own copy of the approved id-set creates a two-writer drift window between approve() and add() (B's own weakness #1) — a steady-state never-hallucinate breach self-healed only at reboot, and the public mutation surface is an information leak a caller can desync. The synthesis deletes both failure modes at once: Store-owns-and-drives-the-index (no two-writer window, no external-swap refactor, no caller-desync leak) while keeping the typed port, scan_approved single-feed, rebuild_from_store derivation, and is_authoritative assertion.

### Chosen design
# Chosen design — SYNTHESIS: durable `EpisodeStore` owns `status` + the value blob and is the SOLE mutator of a swappable, derived `VectorIndex` port (fed only by `scan_approved`, guaranteed-exact by a typed `is_authoritative`)

## 0. The one-line shape
`EpisodeStore` (SQLite/WAL, source of truth, owns rows+blobs+`status`+ledgers+CAS) **drives** a `VectorIndex` (RAM-only derived cache, swappable backend) through exactly two crossing points — `store.scan_approved()` (the only feed) and `index.rebuild_from_store(store)` (the only bulk path) — and mirrors `approve`/`reject` into the index **inside the Store's own write lock/tx**. Callers never touch the index. This takes A's *one-consistency-domain / single-writer / no-desync* win and B's *typed-swappable-port / never-resurrect / authoritative-as-a-type* win, and deletes A's external-swap-refactor weakness and B's two-writer-drift weakness.

## 1. Ports (exact signatures)

```python
# hive/types.py
from __future__ import annotations
import enum
from dataclasses import dataclass
from typing import Optional
import numpy as np
Sha256 = bytes  # 32-byte digest (ported types.py)

class Status(enum.Enum):
    PENDING  = "pending"
    APPROVED = "approved"

@dataclass(frozen=True)
class Episode:
    id: int; tenant_id: str; text: str
    value: np.ndarray            # float32[d], PCA-projected, L2-normalized
    weight: float; ts: int; source: str; tags: str
    content_hash: Sha256; status: Status
    proposed_by: str
    approved_by: Optional[str]; approved_ts: Optional[int]
    version: int; W_version: int

@dataclass(frozen=True)
class Hit:
    episode_id: int
    score: float                 # POSITIVE cosine in [-1, 1], NOT |q.x|
```

### 1.1 `EpisodeStore` — durable, authoritative, sole index-mutator
```python
# hive/store.py  (port: storage/persistence.py + row_codec.py + blob_store.py)
from typing import Iterator, Optional, Sequence

class CASConflictError(RuntimeError): ...

class EpisodeStore:
    """SQLite single-file (WAL, foreign_keys ON). SOURCE OF TRUTH for rows,
    the float32 value blob, content_hash, status, the move-#6 ledgers, and the
    optimistic-CAS version. OWNS its VectorIndex: the index is injected but only
    the Store mutates it, under the Store's write lock, in the same tx that
    writes status. Knows d only to validate blob length.
    search(): O(N_approved . d) via the index. Single SQLite writer (BEGIN
    IMMEDIATE + ported full-jitter backoff, persistence.py); WAL readers concurrent."""

    def __init__(self, path: str, d: int, index: "VectorIndex",
                 tenant_id: str = "default") -> None: ...
        # constructor calls index.rebuild_from_store(self) ONCE — the only boot path.

    # --- capture / admission state machine (server-enforced, §8.2) ---
    def stage(self, ep: Episode) -> int: ...
        # INSERT one PENDING row + blob in ONE tx. content_hash UNIQUE => dedup
        # returns existing id on conflict. Does NOT touch the index (pending
        # rows are never searchable). O(d).
    def approve(self, eids: Sequence[int], approver: str, ts: int) -> list[int]: ...
        # PENDING->APPROVED, stamp approved_by/ts, AND self._index.add(eid, value)
        # for each — ALL under self._wlock in one tx. Row-flip and index-add
        # cannot diverge (A's win). Returns ids actually flipped. O(k.d).
    def reject(self, eids: Sequence[int]) -> list[int]: ...
        # DELETE pending rows only (never approved); self._index.remove(eid) for
        # any that were somehow indexed. Under the same lock/tx. Returns deleted.
    def pending(self, since: Optional[int] = None) -> list[Episode]: ...

    # --- the SOLE feed into the index (the one crossing point) ---
    def scan_approved(self) -> Iterator[tuple[int, np.ndarray]]: ...
        # yields (eid, value[d]) for status='approved' ONLY, ascending eid.
        # Total, ordered, deterministic projection. Data-only: no index ref,
        # no callback. Directionality store->index is structural.

    # --- recall (read-only; delegates ranking to the port) ---
    def search(self, value_q: np.ndarray, top_n: int) -> list[Hit]: ...
        # asserts index.is_authoritative when cfg.require_exact; then returns
        # index.search(value_q, top_n). Returns [] on empty store (abstain-safe).

    # --- fetch / credit-path reads ---
    def get(self, eid: int) -> Optional[Episode]: ...
    def fetch_text(self, content_hash: Sha256) -> Optional[str]: ...
    def values_for(self, eids: Sequence[int]) -> dict[int, np.ndarray]: ...
    def count(self, status: Optional[Status] = None) -> int: ...

    # --- tier-D migration (W_version bump) ---
    def rebuild_index(self, reproject) -> int: ...
        # re-embed every approved value via reproject(text)->value, rewrite the
        # value blobs, bump W_version in meta, then self._index.rebuild_from_store
        # (self) — all atomic under the write lock. Readers see the OLD index
        # until commit. Returns rows re-keyed. (§6.1#4 round-trip.)
    def health(self) -> dict: ...
```

### 1.2 `VectorIndex` — derived, swappable, NO public mutation surface for callers
```python
# hive/index.py  (port+rename: storage/vector_index.py)
from typing import Protocol, runtime_checkable

@runtime_checkable
class VectorIndex(Protocol):
    """Derived dense index over APPROVED values. A swappable RAM-only CACHE:
    every state is reproducible from EpisodeStore via rebuild_from_store().
    Cosine over L2-normalized vectors == inner product.
    add/remove are called ONLY by EpisodeStore (under its lock) — never by a
    recall caller. That is the deep-module narrowing: the only public verb a
    ranker uses is search()."""

    # mutation — Store-only callers
    def add(self, eid: int, value: np.ndarray) -> None: ...     # called inside approve()
    def remove(self, eid: int) -> None: ...                     # called inside reject()

    # the one query verb the ranker path uses
    def search(self, value_q: np.ndarray, k: int) -> list[Hit]: ...
        # (eid, POSITIVE cosine) DESCENDING; len == min(k, len(self)).
        # Positive-cosine baked into the contract: no adapter can reintroduce
        # the |q.x| sign trap (vector_index.py:74,433 np.abs).

    # the derivation contract — the heart of the seam
    def rebuild_from_store(self, store) -> int: ...
        # atomically clear + re-add every (eid, value) from store.scan_approved();
        # returns count indexed. PURE function of the store's approved set.

    @property
    def is_authoritative(self) -> bool: ...
        # True  => search() is EXACT top-k over ALL approved (exhaustive).
        # False => approximate (hnsw); MUST be explicitly opted into.
        # A typed assertion, NOT a hot-path approx_threshold integer (§4.3).

    def __len__(self) -> int: ...
```

## 2. Adapters

```python
# hive/index.py
import threading, numpy as np

class ExhaustiveVectorIndex:                      # AUTHORITATIVE default
    """Full matmul over the (N_approved, d) L2-normalized value matrix.
    NO approx_threshold, NO candidate_k branch — growing N can NEVER flip it.
    O(N.d) time, O(N.d) space."""
    def __init__(self, d: int) -> None:
        self._d = d; self._lock = threading.Lock()
        self._ids: list[int] = []; self._mat: Optional[np.ndarray] = None
    @property
    def is_authoritative(self) -> bool: return True          # no setter, permanent
    def add(self, eid, value):
        with self._lock:
            v = value.astype(np.float32).reshape(1, -1)
            self._mat = v if self._mat is None else np.vstack([self._mat, v])
            self._ids.append(eid)
    def remove(self, eid):
        with self._lock:
            if eid in self._ids:
                i = self._ids.index(eid); del self._ids[i]
                self._mat = np.delete(self._mat, i, axis=0)
    def search(self, value_q, k) -> list[Hit]:
        with self._lock:
            if self._mat is None or self._mat.shape[0] == 0: return []   # abstain-safe
            scores = self._mat @ value_q.astype(np.float32)              # POSITIVE cosine
            kk = min(int(k), scores.shape[0])
            idx = np.argpartition(-scores, kk - 1)[:kk]
            idx = idx[np.argsort(-scores[idx])]
            return [Hit(self._ids[i], float(scores[i])) for i in idx]
    def rebuild_from_store(self, store) -> int:
        with self._lock:
            ids, rows = [], []
            for eid, value in store.scan_approved():            # the SOLE feed
                ids.append(eid); rows.append(value.astype(np.float32))
            self._ids = ids
            self._mat = np.vstack(rows) if rows else None
            return len(ids)
    def __len__(self): return len(self._ids)

class HnswVectorIndex:                              # future; opt-in only
    """Ports HNSWVectorIndex (vector_index.py:285). is_authoritative=False, so
    the ranker's require_exact assertion refuses it unless explicitly opted in.
    approx_threshold/candidate_k are INTERNAL knobs here — they cannot leak into
    an exhaustive deployment (§4.3 'landmine detonator removed from the surface')."""
    @property
    def is_authoritative(self) -> bool: return False
    # add/remove/search/rebuild_from_store/__len__ : same Protocol.
```

```python
# hive/build.py  (port+simplify: service.py:68 _build_vector_index)
def build_vector_index(backend: str, d: int) -> VectorIndex:
    if backend == "exhaustive": return ExhaustiveVectorIndex(d)
    if backend == "hnsw":       return HnswVectorIndex(d)
    raise ValueError(f"unknown vector_index_backend: {backend!r}")   # validated at config load
```

## 3. Swap mechanism (config keys + steps)
- Config key `index.vector_index_backend` (tier C, §4.3): `exhaustive` (default) | `hnsw`. `recall.require_exact` (default `True`). `recall.approx_threshold` survives ONLY as an internal `HnswVectorIndex` beam knob — deleted from the recall path.
- Swap = (1) flip `index.vector_index_backend`; (2) restart; (3) `EpisodeStore.__init__` calls `build_vector_index(...)` then `index.rebuild_from_store(self)` ONCE. **No durable row moves; boot path == swap path** (no separate "load index from disk", so no stale-index-file failure mode — satisfies §6.3 "force a clean store").
- External-storage swap (pgvector/Qdrant, future): write a `PgVectorIndex(VectorIndex)` adapter; its `rebuild_from_store` loops `scan_approved` into the external store. **No EpisodeStore edit** — closes A's weakness. The Store still owns `status`; the external thing is still just a derived cache fed by `scan_approved`.

## 4. Failure-mode logging points (per global standard §6, spec §6.4)
- `EpisodeStore.__init__`: log `{event:"index.rebuild_on_boot", backend, n_indexed, ms}`; if `n_indexed != count(APPROVED)` log `error` + force re-rebuild.
- `stage`: `debug` content_hash (hashed, never the secret); on `UNIQUE` conflict log `info dedup_hit`.
- `approve`: `info {event:"approve", k, approver}`; if `index.add` raises, log `error` with eids and **roll back the tx** (status flip and add are one tx — never half-applied).
- `reject`: `warn` if any eid was APPROVED (refused, not deleted).
- `search`: if `require_exact and not index.is_authoritative` → log `error` + raise `ConfigError` (never silently serve approx). On empty store log `debug abstain_empty`.
- `rebuild_index` (tier-D): `info` start/rows/ms; on `reproject` failure for row j, log `error` + abort tx (readers keep the old matrix — no half-migrated reads).
- Boundary logs (spec §6.4): embedder load, SQLite I/O, missing model, `hnsw` fallback, abstain decision — structured JSON `{timestamp,level,context,message,error,stack}`; secrets/PII never logged.

## 5. Test-first verification (failing tests BEFORE impl; per the hard mandate)

### 5.1 Parametrized conformance suite — `tests/test_vector_index_conformance.py::test_*[exhaustive|hnsw|list]`
`ListVectorIndex` (trivial numpy oracle, `is_authoritative=True`) is the ground truth.

| `test::name` | Assertion | Failure it catches |
|---|---|---|
| `test_rebuild_is_pure_function_of_store` | incremental-`add` index == `rebuild_from_store` index for 100 random q | index drifts from store (B's weakness, now tested) |
| `test_rebuild_is_idempotent` | `rebuild();s1; rebuild();s2; s1==s2` | rebuild appends instead of clearing |
| `test_rebuild_indexes_only_approved` | 10 approved + 5 pending → `rebuild()==10`; pending eids never in any `search` | **§6.1#5b: pending leaks into recall** |
| `test_search_is_positive_cosine_descending` | plant `+q` and `-q`; `search(q,2)[0]` is `+q`; `-q` last | the `\|q.x\|` sign trap |
| `test_search_len_clamped` | `len(search(q,1000))==min(1000,len)`; empty→`[]` | off-by-one / hnswlib "cannot return k" RuntimeError |
| `test_remove_then_search_excludes` | `add(e);remove(e)`; e never in `search` | anti-resurrection |
| `test_exhaustive_is_authoritative` | `ExhaustiveVectorIndex().is_authoritative is True` permanently (no setter) | a refactor making exhaustive silently approx |
| `test_exhaustive_matches_oracle_exactly` | `Exhaustive.search == List.search` (set+order) on 1000 rows | numeric divergence in the authoritative backend |

### 5.2 The seam (Store↔Index) — `tests/test_store_index_seam.py`
| `test::name` | Assertion | Failure it catches |
|---|---|---|
| `test_stage_is_never_in_search` | `stage(ep)` → `store.search(q,10)==[]` | **INVARIANT approved-only recall** (§240) |
| `test_approve_admits_atomically` | `stage→approve` → top Hit is that eid; `get(id).approved_by==approver` | row-flip/index-add divergence (kills B weakness #1) |
| `test_approve_index_failure_rolls_back_status` | inject `index.add` raise → status stays PENDING, not searchable | half-applied two-writer state |
| `test_reject_drops_pending_only` | `reject(approved)` → `[]`, row + index entry survive | an approved memory deletable via reject |
| `test_dedup_on_content_hash` | same text twice → same id, one row, one index entry | `UNIQUE(content_hash)` not enforced |
| `test_rebuild_after_dropped_index_recovers` | drop the RAM index; `rebuild_from_store`; identical recall | store can always reconstruct the index (the synthesis promise) |
| `test_swap_backend_preserves_coverage` | same store → exhaustive vs hnsw `rebuild` → identical reachable id-set; exhaustive exact / hnsw within tolerance | swap loses or moves rows |
| `test_require_exact_refuses_approx` | `require_exact=True` + `HnswVectorIndex` → `ConfigError`, never serves | **never-hallucinate** served by an approx index |
| `test_scan_approved_matches_raw_sql` | `list(scan_approved())` == `SELECT id,value WHERE status='approved' ORDER BY id` | the shared-oracle correlated-failure (B weakness #3) pinned independently |
| `test_rebuild_index_reproduces_recall` | snapshot top-10 for K queries; `rebuild_index(reproject)`; same top-10, `W_version` bumped | **§6.1#4 round-trip** corruption |
| `test_rebuild_index_atomic_under_crash` | raise in `reproject` at row j → store keeps OLD consistent matrix | partial migration leaks to readers |

### 5.3 Mutation tests (RULE 2 — mandatory for ranker/state/credit paths; §6.4)
| Fault introduced | Test that MUST go red |
|---|---|
| `_search` ranks by `-np.abs(scores)` (the `\|q.x\|` trap) | `test_search_is_positive_cosine_descending` + `test_exhaustive_matches_oracle_exactly` |
| add `if len>10_000: hnsw` inside Exhaustive.search (re-introduce auto-flip) | `test_exhaustive_matches_oracle_exactly` (diverges from oracle) |
| `approve` flips status but skips `index.add` | `test_approve_admits_atomically` |
| `stage` also calls `index.add` (leak pending) | `test_stage_is_never_in_search` |
| `reject` drops the `status=='pending'` guard | `test_reject_drops_pending_only` |
| `rebuild_from_store` drops the `status=='approved'` filter | `test_rebuild_indexes_only_approved` |
| `rebuild_from_store` removes `clear()` before re-add | `test_rebuild_is_idempotent` |
| hardcode `HnswVectorIndex.is_authoritative = True` | `test_require_exact_refuses_approx` (the load-bearing never-hallucinate proof) |
| `index.add` failure path swallows the exception instead of rolling back | `test_approve_index_failure_rolls_back_status` |

Each fault: introduce → named test red → revert → green; report the fault + the catching test + suite green (§6.4 "a gate whose test still passes when broken is not tested").

### 5.4 Fakes (small, behavior not mock-interaction)
- `FakeEmbedder` — deterministic `embed(text)->float32[d]` (seeded hash → L2-normalize), zero ML deps, CPU-instant.
- `EpisodeStore(":memory:")` — SQLite in-memory is the durability fake (ported `InMemoryBlobStore` pattern).
- `ListVectorIndex` — the numpy oracle for the conformance suite.

## 6. Schema (§3 model, §10 PORT+SIMPLIFY; durable layer only)
```sql
CREATE TABLE IF NOT EXISTS episodes (
  id INTEGER PRIMARY KEY, tenant_id TEXT NOT NULL, text TEXT NOT NULL,
  value BLOB NOT NULL,                          -- float32[d] LE (row_codec)
  weight REAL NOT NULL, ts INTEGER NOT NULL, source TEXT NOT NULL, tags TEXT,
  content_hash BLOB NOT NULL UNIQUE,            -- sha256(text); DB-layer dedup
  status TEXT NOT NULL DEFAULT 'pending',       -- pending | approved
  proposed_by TEXT NOT NULL, approved_by TEXT, approved_ts INTEGER,
  version INTEGER NOT NULL DEFAULT 0,           -- optimistic CAS
  W_version INTEGER NOT NULL);                  -- geometry; rebuild_index bumps it
CREATE INDEX IF NOT EXISTS idx_ep_approved ON episodes(status) WHERE status='approved';
CREATE TABLE IF NOT EXISTS blobs (              -- verbatim blob_store.py
  content_hash BLOB PRIMARY KEY, content BLOB NOT NULL, written_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
-- move-#6 ledgers (§3) live in the SAME file/lock/tx domain (one credit-path consistency domain)
CREATE TABLE IF NOT EXISTS exposure (trace_id TEXT, episode_id INTEGER, recall_margin REAL, task_ref TEXT, injected_ts INTEGER);
CREATE INDEX IF NOT EXISTS idx_exposure_trace ON exposure(trace_id);
CREATE TABLE IF NOT EXISTS task_outcomes (task_ref TEXT, trace_id TEXT, family_scope TEXT, repo TEXT, files_touched TEXT, state TEXT, reward REAL, merge_ts INTEGER, settle_at INTEGER, PRIMARY KEY (task_ref, trace_id));
CREATE TABLE IF NOT EXISTS utility (episode_id INTEGER, family_scope TEXT, wins REAL, losses REAL, n_sources INTEGER, version INTEGER, PRIMARY KEY (episode_id, family_scope));
```
The vector index holds NO durable state — it is `value` blobs re-materialized via `scan_approved`. `import.sh`/backup touch only this SQLite file (the index has no disk).

## 7. Why this beats both pure options (rejected-alternatives log entry)
- **vs A**: A swaps backend-of-scan cleanly but makes external vector storage a `MemoryStore` refactor (violates the SWAPPABILITY MANDATE's "no core refactor"), and folds C3-ranker scanning into C5-Store (spec §10 keeps them separate ports; the existing ranker already calls a `candidates()`/`search()` method on an index object). A's never-flip guarantee rests on reviewer discipline ("trust every mutation goes through approve/reject"); the synthesis makes it a typed `is_authoritative` assertion. Synthesis keeps A's single-consistency-domain win (Store owns `status` and drives `index.add` under one lock/tx) WITHOUT A's private-method coupling.
- **vs B**: B's free-standing index with public `add/remove` and its own copy of the approved id-set creates a two-writer drift window between `approve()` and `add()` (B's own confessed weakness #1) — a steady-state never-hallucinate breach self-healed only at reboot. Synthesis kills it: the Store is the SOLE index mutator, `add` happens inside the same tx that flips `status`, and callers get no mutation verb at all (deeper module, no leak). Synthesis keeps B's typed-port swappability, `rebuild_from_store` derivation contract, `scan_approved` single-feed (never-resurrect-pending), and `is_authoritative` never-flip-as-a-type.

## 8. How future functionality bolts on
- **HNSW at scale**: `HnswVectorIndex` (`is_authoritative=False`), flip config + reboot → `rebuild_from_store`. No row/schema/recall-path change.
- **External vector DB**: `PgVectorIndex`/`QdrantIndex` adapter; `rebuild_from_store` loops `scan_approved` into it. No `EpisodeStore` edit.
- **§8.3 BM25 hybrid**: a SECOND derived index over the same store — `Bm25Index.rebuild_from_store` reads `scan_approved` into FTS5; RRF fuses `dense.search() × bm25.search()` at the service layer; the entropy gate (C4) still runs over the fused distribution.
- **Move-#6 surfacer**: `search` already returns `Hit(eid, score)`; the `weight × f(utility)` re-rank reads the `utility` posterior co-located in the same Store via `values_for`/`utility_for` — no cross-object join; ε-randomization wraps `search` at the service layer, store stays pure.

**Grounding (absolute paths):** spec `/home/null/Desktop/work/hivemind/HIVEMIND_VMIN_SPEC.md` (§2 C3/C5 lines 105-113, §3 line 240 approved-only, §4.3 lines 282-296 authoritative structural fix, §6.1#4/#5b round-trip + pending-never-recallable, §6.3 force-clean-store, §6.4 mutation+logging, §10 line 730 ranker-port / line 733 store-port). Port sources: `/home/null/Desktop/work/AgentCortex/cls_memory/cls_memory/storage/vector_index.py` (Exhaustive/HNSW + the `np.abs` `|q.x|` sign trap at 74,433,447 + the `candidates` surface), `.../serving/sources/native_source.py` (ranker calls `native_index.candidates(query_vec,k)` line 92 — the existing collaborator shape this design formalizes; positive-cosine workaround), `.../protocols.py:102` `EpisodeStore` + `:133` `VectorIndex` (the two latent Protocols), `.../serving/service.py:68` `_build_vector_index` factory, `.../core/episodic.py` (the silent `approx_threshold` flip this design removes), `.../ops/migration.py` (the hand-rolled rebuild loop promoted to `rebuild_from_store`), `.../storage/persistence.py` (WAL pragmas + `BEGIN IMMEDIATE` backoff + `update_cas`), `.../storage/blob_store.py` (content-hash blobs ported verbatim), `.../storage/row_codec.py` (float32 blob codec), `.../embedder.py` (`TextEmbedder` Protocol + `ProjectionHead.pca` = the `reproject` callable `rebuild_index` takes).

**Open questions:**
- §5-D1 sparse-key A/B: the synthesis bakes POSITIVE-cosine + (eid, score) into VectorIndex.search() (correct for the dense value), which forecloses the |q.x| signed-key ranker D1 would need. If D1 ever resurrects the sparse key it is a SEPARATE SparseKeyIndex port with its own contract, not this one — confirm D1 resolves dense-only (spec's stated default) before deleting geometry.D/k.
- is_authoritative is a coarse boolean (exact vs approx). §8.3 may later admit a high-recall hnsw under measured CI (recall@10 >= floor), which the binary cannot express — flag that it must grow into a recall-floor guarantee type once approximate ranking is admitted; today's binary is the honest never-hallucinate-MVP floor.
- Does the Store drive index.rebuild_from_store(self) at construction (Store-owns-lifecycle), or does the service wire Store->index at boot? Recommend Store-owns: the Store constructs/rebuilds its index from its own scan_approved so there is exactly one boot path == the swap path (B's collapse-two-paths win) AND one writer (A's win). Confirm this does not over-couple C5 to the index backend choice — mitigated because the backend is injected as a VectorIndex instance, not selected inside the Store.
- Rebuild-on-boot is O(N_approved.d) (~100MB / sub-second at N<=1e5). Confirm acceptable as the only index-load path, or add an optional persisted+validated index (compare len(index) vs store.count(status='approved'); mismatch => rebuild) once N grows 10x. The contract already names the recovery action.

---

## D4. Containerized onboarding + hive_init handshake (first-run flow) for Hivemind v-min: how the first-run bootstrap and the server-served hive_init handshake should be shaped.

- **Option A:** APPROACH A — Repo-shipped ./hive up bootstrap script (liveness only, knows nothing about MCP) + a TWO-PHASE hive_init MCP tool (phase 1 = GET marker-delimited rules block + sha256 block_hash, no state mutation; phase 2 = confirm_hash records the link in a NEW bootstrap table). Liveness and handshake are cleanly separated into two deep one-line/one-tool modules. The block is server-generated so the advertised tool list can't lie. hive_init deliberately touches NONE of the three swap ports. Cost: two entry points (shell + MCP) and a two-phase handshake whose second phase exists only so the persisted link can't lie.
- **Option B:** APPROACH B — Pure-MCP onboarding: the only manual step is `docker compose up` + one MCP-config line; everything else is a single hive_init tool returning a frozen typed InstallPlan (RulesBlock + TrailerTeaching + HookSpec[] + HealthProbe + link_token), applied by the agent (the universal installer), then confirmed via an EXTENDED hive_health(link_token=...). No new table (link lives in the existing SqliteMeta kv store). trailer_key flows from producer.stamp_trailer config (single source, kills CONFIG_DRIFT). The link_token's second job is to ALSO enroll the repo into producer.watch_repos — collapsing onboarding + trailer-teaching + hook-wiring + producer-enrollment + verification into one round-tripped handshake.
- **Winner:** synthesis — Neither pure approach is the right ship, but the synthesis (B's skeleton hardened with A's two non-negotiable safety properties) dominates both, and I verified every load-bearing claim against the tree.

What B gets RIGHT and must be kept (the spine):
- It is the literal realization of the spec's C7/§9 mandate: "a server-served hive-init prompt the agent runs to self-wire whatever hooks the local harness supports (the agent is the universal installer)." A turns that prompt into a tool too, but B turns it into a TYPED InstallPlan (frozen dataclasses + content hashes) — a contract that can't lie, which the rubric's agent-native amplifier prizes over A's prose-ier block.
- Tool-count discipline: B adds +1 tool and EXTENDS hive_health; A adds +1 tool AND a new bootstrap table. I verified HealthSnapshot is `TypedDict(total=False)` (types.py:459) and health() already has the exact fail-fast early-return shape (service.py:1354) — so B's optional `linked`/`link_repo` fields are a genuinely clean additive extension with a byte-identical no-token path, honoring the explicit `mcp_server.py:200` "tool count stays 11" discipline the tree prizes.
- No new table: I verified SqliteMeta.get/set (persistence.py:1148) is a real UPSERT kv store. B's link record is one namespaced JSON blob — strictly less surface than A's whole new CREATE TABLE bootstrap(...) under the single-writer CAS discipline.
- The CONFIG_DRIFT kill: B routes trailer_key from producer.stamp_trailer (single source) so the producer-watches and agent-stamps trailer can never silently diverge — the exact §11/§6.1#6 silent-join-failure. A hard-codes the trailer in its static block template; that is a latent drift A never addresses. This is decisive on the rubric's information-hiding axis.

What A gets RIGHT and the synthesis MUST graft onto B (B is dangerous without these two):
- A's two-phase confirm-by-hash is the honest answer to a real lie B tolerates. B's plain link_token (minted at plan time) is recorded for a block the agent might never write — B itself concedes in its own weakness #1 that "there is no server-side proof the rules block landed." A's phase-2 confirm_hash == sha256(installed block) means the recorded link CANNOT lie about WHICH block content the agent installed. Against the TDD-first mandate this is not cosmetic: A's test_phase2_stale_hash_refused (zero rows written on a wrong hash) is a real invariant with a real mutation test (flip `confirm != block_hash` to `==` → test goes red); B's link_token round-trip only proves the token came from this server, not that the block was written. The synthesis adopts A's content-hash confirm as the round-trip credential — strictly stronger than B's opaque token at zero extra round-trips (B already had a two-step apply→confirm flow; making the confirm a hash instead of a token is free).
- A's clean separation of liveness (./hive up) from handshake (hive_init) is correct and B under-specifies it. B hand-waves "docker compose up + one config line"; A's bounded _wait_healthy with a hard timeout that dumps logs and exits non-zero is the fail-fast boundary the global §5/§6 standard demands, and its mutation test (mock a never-healthy service → must exit before wall-clock) is real. The synthesis keeps ./hive up as a thin liveness wrapper, but — taking A's own honest weakness #4 — REQUIRES the container HEALTHCHECK to probe a hive_health that touches the LOADED embedder, so "healthy" can't be declared before the model is resident.

What I REJECT from B and why (this is the load-bearing cut):
- B's headline differentiator — "the link_token ALSO enrolls the repo into producer.watch_repos, collapsing onboarding + producer-enrollment into one mechanism" — is the one thing the synthesis explicitly drops to a logged WARN-only note, NOT an action. I verified producer.* config does NOT exist in config.py (it is build-new per §10/§4.8). So B couples hive_init — a substrate, Phase-1 onboarding concern — to C10, a build-new, Phase-2-adjacent component, BEFORE it exists. That violates the SWAPPABILITY MANDATE's spirit (the outcome producer must sit behind a port that swaps with NO core refactor) and the §12 phase split (Phase 1 ships substrate; the producer's watch set is its own config). It also smuggles a side effect (mutating the producer's watch config) into a handshake that should be pure-plus-one-write — a special-vs-general violation: onboarding is general, watch-enrollment is one specific downstream's concern. A's central virtue is exactly this restraint: "hive_init sits entirely above the three ports and touches none of them." The synthesis takes A's discipline here and overrides B: hive_init only TEACHES the Hive-Trace trailer convention (so move-#6 still works the moment a producer is configured) and at most emits a `notes` WARN if repo_path is not yet in watch_repos — it never writes producer config. That keeps the producer swap seam clean and keeps onboarding swappable along the harness axis only (HookCatalog), which is where the variation actually belongs.

Net: the synthesis = B's typed-InstallPlan skeleton + B's +1-tool / extend-health / no-new-table economy + B's single-source trailer_key, hardened by A's content-hash phase-2 confirm (the link can't lie about the installed block) and A's liveness/healthcheck rigor, MINUS B's premature producer-enrollment coupling. It beats pure-A by being cheaper (no new table, one fewer entry-point concept since the InstallPlan carries hooks A bolts on later) and lie-proof in the typed contract rather than the prose block; it beats pure-B by closing B's own conceded "no proof the block landed" hole and by refusing the producer coupling that would have broken the swap mandate.
- **Rejected because:** Pure A rejected: it pays for the can't-lie link with a SECOND persistence concern (a whole new `bootstrap` CREATE TABLE under the CAS discipline) when the verified-existing SqliteMeta kv store (persistence.py:1148) makes that table unnecessary — more surface for the same guarantee, against the rubric's deep-module/narrow-surface target. Its onboarding block is a static server-versioned string rather than a typed structure, so hooks/trailer/health bolt on as later additive fields instead of being first-class in the contract from day one (B's InstallPlan has them typed up front), and its hard-coded trailer in the template is a latent CONFIG_DRIFT B explicitly prevents. A is the more conservative, honest design but it is not the cheapest correct one.

Pure B rejected on two hard mandates: (1) Swappability — B couples hive_init to producer.watch_repos enrollment, but I verified producer.* config does not exist yet (build-new, §10/§4.8); coupling the onboarding handshake to a not-yet-built downstream component, and giving it the side effect of mutating that component's watch config, violates the port-isolation the SWAPPABILITY MANDATE requires and the §12 Phase-1/Phase-2 split. (2) The link can lie — B's own weakness #1 concedes there is no server-side proof the rules block landed; its opaque link_token proves token provenance, not installed-block content. Under the TDD-first mandate that is a missing invariant: there is no equivalent of A's test_phase2_stale_hash_refused (+ its mutation test) because B has nothing to assert the installed content against. Both gaps are fixable by grafting A's content-hash phase-2 confirm and dropping the producer coupling to a WARN-only note — which is precisely the synthesis.

### Chosen design
# Hivemind v-min — Containerized Onboarding + `hive_init` Handshake (CHOSEN: B-skeleton hardened by A)

## 0. One-sentence shape
`./hive up` guarantees a *healthy* server (liveness only); the agent then calls **one** new MCP tool `hive_init(repo_path, harness)` which returns a **frozen typed `InstallPlan`** (rules block + trailer teaching + harness hooks + a `block_hash` round-trip credential); the agent applies the plan idempotently into its harness rules file; then confirms via the **extended** `hive_health(confirm_block_hash=…)`. The link is recorded in the **existing `SqliteMeta` kv store** (no new table), keyed by the **content hash of the block the agent actually installed** (so the link can't lie). `hive_init` touches **none** of the three swap ports and only **teaches** the `Hive-Trace` trailer — it never mutates producer config.

This realizes spec C7/§9 ("server-served `hive-init` prompt the agent runs to self-wire whatever hooks the local harness supports — the agent is the universal installer") as a typed tool, keeps tool-count discipline (+1 tool, extend `hive_health`), and honors §12's Phase-1/Phase-2 split.

---

## 1. Ports & interfaces (Python type hints)

```python
# types.py — frozen contracts; the install structure that cannot lie.
from dataclasses import dataclass

@dataclass(frozen=True)
class RulesBlock:
    rules_file: str            # ABSOLUTE path resolved from harness
    marker_begin: str          # "<!-- hive-init:start -->"
    marker_end: str            # "<!-- hive-init:end -->"
    version: int               # embedded as "<!-- hive-init:version=N -->" in body
    body: str                  # FULL marker-delimited text (incl. version comment)
    block_hash: str            # "sha256:<hex>" of body — THE round-trip credential (from A)

@dataclass(frozen=True)
class TrailerTeaching:
    trailer_key: str           # == producer.stamp_trailer (SINGLE SOURCE; never hard-coded)
    instruction: str
    example: str               # "Hive-Trace: 7f3a1c 9b22e0"

@dataclass(frozen=True)
class HookSpec:
    path: str                  # ABSOLUTE write target (e.g. .git/hooks/post-commit)
    mode: str                  # "0755" script | "0644" config
    body: str
    merge: str                 # "overwrite" | "create-if-absent" | "json-merge"
    purpose: str

@dataclass(frozen=True)
class InstallPlan:
    plan_version: int
    repo_path: str
    harness: str
    rules_block: RulesBlock
    trailer: TrailerTeaching
    hooks: tuple[HookSpec, ...]    # () for harness="generic" — MCP-only is first-class
    server: "ServerFacts"         # name, version, geometry.W_version, tools=list(handlers)
    already_linked: bool          # idempotent re-run with an unchanged block_hash
    notes: tuple[str, ...]        # WARN surfaces (e.g. "repo not in producer.watch_repos yet")

# C7 onboarding port — pure, no I/O of its own; asks injected sub-ports.
class InstallPlanner(Protocol):
    def plan(self, *, repo_path: str, harness: str, identity: Identity,
             rules_file: str | None) -> InstallPlan: ...

class DefaultInstallPlanner:
    def __init__(self, hook_catalog: "HookCatalog", link_store: "LinkStore",
                 trailer_key: str):              # trailer_key READ from producer.stamp_trailer
        ...
```

Three sub-seams, each a swap point, **none** of which is one of the three mandated core ports (embedder / vector-index / outcome-producer):
- `HookCatalog.hooks_for(harness) -> tuple[HookSpec, ...]` — new harness = one entry. `"generic" -> ()`.
- `LinkStore` — thin facade over the verified-existing `SqliteMeta` (persistence.py:1148 UPSERT). No new table.
- `trailer_key` injected from `producer.stamp_trailer` config — kills the §11/§6.1#6 CONFIG_DRIFT class.

## 2. The two new/extended MCP surfaces

### 2.1 `hive_init` (new tool) — added to `TOOL_DEFINITIONS` (mcp_tools.py) + `_tool_handlers` (mcp_server.py:89)
```python
inputSchema = {"type":"object",
  "properties":{
    "repo_path":{"type":"string"},
    "harness":{"type":"string","enum":["claude-code","codex","cursor","windsurf","cline","opencode","generic"]},
    "rules_file":{"type":"string"}},   # optional override
  "required":["repo_path","harness"]}
```
Handler stays a thin adapter (matches the `_tool_write -> service.write_text` pattern):
```python
def _tool_init(self, args: dict) -> dict:
    plan = self.service.install_plan(self.identity,
              repo_path=args["repo_path"], harness=args["harness"],
              rules_file=args.get("rules_file"))
    log.info("hive_init.plan", extra={"harness": args["harness"],
             "block_hash": plan.rules_block.block_hash,
             "already_linked": plan.already_linked,
             "agent": self.identity.agent_id})
    return asdict(plan)
```
`service.install_plan(...)` is **pure** (composes block from template + live server facts; `block_hash = "sha256:"+sha256(body)`). It does **not** write the link — the link is recorded only on confirm, so a recall-only agent that reads the plan leaves no spurious row (A's `test_phase1_is_pure_no_write` invariant, carried over).

### 2.2 `hive_health` (EXTENDED, not duplicated) — the round-trip confirm + link record
`HealthSnapshot` is `TypedDict(total=False)` (verified types.py:459) and `health()` already has fail-fast early returns (verified service.py:1354), so this is a clean additive extension:
```python
# inputSchema gains:
"confirm_block_hash": {"type":"string"},  # the block_hash from hive_init, after writing it
"repo_path":          {"type":"string"}   # which repo this link is for
# health() return gains (ONLY when confirm_block_hash present):
#   "linked": bool, "link_repo": str, "link_stale": bool
```
With no `confirm_block_hash`, `health()` is **byte-identical** to today (the hot liveness probe stays clean — spec: "safe to poll frequently"). With it, the server: (1) recomputes the current canonical block_hash for that repo/harness; (2) if `confirm_block_hash == current` → UPSERT the link into `SqliteMeta` under `hive:link:<sha256(repo_path)>` and return `linked=True`; (3) if mismatch → `linked=False, link_stale=True` + WARN log, **no link written**. This is A's phase-2 confirm-by-hash, folded into the existing health tool instead of a second new tool — the recorded link can never lie about *which block content* the agent installed.

## 3. The link record (no new table)
```python
meta["hive:link:<sha256(repo_path)>"] = json({
  "block_hash": "sha256:…",   # the installed block's content hash (drift detector)
  "block_v": N, "repo_path": "/work/x", "harness": "claude-code",
  "agent_id": "…", "tenant_id": "…", "linked_ts": 1717531200})
```
Idempotent UPSERT (SqliteMeta already does `ON CONFLICT(key) DO UPDATE`). `already_linked` is computed by comparing the incoming block_hash to the stored one. `hive_health` (no token) can additionally report `links: [...]` so the link is inspectable.

## 4. The installed block (server-authored, behavioral-only)
Marker + embedded version, matching the user's own proven `init-harness`/`lattice-init`/`link-Reins` convention (`<!-- hive-init:start --> … <!-- hive-init:version=N --> … <!-- hive-init:end -->`). Carries **no** db_path/token/port (secret-safe by construction, §9). The advertised tool list is `list(self._tool_handlers.keys())`, never hand-maintained — the block **can't advertise a tool the server doesn't serve**. Content: recall-before-work, write-stages-for-approval, the `hive_pending/approve/reject` trio, and the `Hive-Trace` trailer teaching (trailer_key from config).

## 5. Idempotent rules-file write (agent side, server-stated algorithm)
1. Read `rules_file` (else empty).
2. Markers present → if embedded `version==N` AND `sha256(region)==content of block_hash` → NO-OP; else REPLACE marker region (outside bytes preserved byte-for-byte).
3. Markers absent → APPEND `\n{body}\n`.
4. Atomic temp+rename.
Then call `hive_health(confirm_block_hash=…, repo_path=…)` to record the link.

## 6. Swap mechanism (honors SWAPPABILITY MANDATE)
- **Embedder / vector-index / outcome-producer ports**: `hive_init` touches **none** of them. It reports `server.geometry.W_version` (reads, never computes). A sidecar embedder/producer is a `docker-compose.yml` change; `hive_init` is untouched.
- **Producer is explicitly NOT enrolled here** (the cut vs B). `producer.*` config is build-new (verified absent in config.py); `hive_init` only TEACHES the trailer and, if `repo_path` is not yet in `producer.watch_repos`, emits a `notes` WARN — it never writes producer config. Watch-set enrollment stays a Phase-2 producer concern behind its own port (§12).
- **Harness axis** varies only in `HookCatalog` (one entry per harness) + the block template (+`block_v` bump). The agent is the universal installer; the server stays harness-agnostic.

## 7. Failure-mode logging points (global §6)
- `./hive up`: `_wait_healthy` until-loop with hard `HIVE_UP_TIMEOUT_S`; on timeout dump last 50 log lines + exit non-zero (fail-fast). Container HEALTHCHECK must probe a `hive_health` that touches the **loaded embedder** so "healthy" ⇏ model not yet resident (A's weakness #4 fix).
- `hive_init.plan` (INFO): harness, block_hash, already_linked, agent.
- `hive_health.link_stale` (WARN): got vs expected block_hash — boundary failure surfaced, not swallowed.
- `hive_init.repo_not_watchable` (WARN) when repo not in `producer.watch_repos` — logged, never silently enrolled.
- Never log the rules body's secret-scan-relevant content; block is secret-safe by construction.

## 8. Test-first verification (Test Contract — failing tests BEFORE impl)
Fakes: `:memory:` SqliteBackend, `FakeIdentity`, fake `HookCatalog`; MCP driven by feeding `MCPRequest` to `MCPServer.handle` (no stdio/Docker/embedder/git).

`test_init.py`:
- `test_plan_is_pure_no_link_written` → after `hive_init`, `meta["hive:link:*"]` is None. *Catches:* GET mutating state.
- `test_plan_block_hash_matches_body` → `block_hash == "sha256:"+sha256(body)` and body has both markers + `version=N`. *Catches:* a drift detector that can't detect drift.
- `test_generic_harness_no_hooks` → `harness="generic"` ⇒ `hooks==()`, plan still complete. *Catches:* MCP-only path regressed into requiring hooks (§9 "hooks never load-bearing").
- `test_trailer_key_from_config_not_hardcoded` → set `producer.stamp_trailer="X-Trace"` ⇒ `plan.trailer.trailer_key=="X-Trace"` and body teaches `X-Trace`. *Catches:* CONFIG_DRIFT.
- `test_block_advertises_only_served_tools` → every tool in body ∈ `_tool_handlers`; the approval trio present. *Catches:* the can't-lie invariant breaking.
- `test_block_carries_no_secrets_or_paths` → body has no db_path/token/port substring. *Catches:* infra leak into a committed rules file.
- `test_producer_not_enrolled_only_warned` → unwatched repo ⇒ producer config UNCHANGED and a `notes` WARN present. *Catches:* B's premature coupling sneaking back in.

`test_init_link.py` (the confirm/round-trip, via extended `hive_health`):
- `test_confirm_records_link` → `hive_health(confirm_block_hash=H, repo_path=R)` ⇒ `linked=True`, exactly one `meta` link row with `block_hash=H`. *Catches:* link never persisting.
- `test_confirm_idempotent` → confirm twice ⇒ `already_linked=True`, still one row. *Catches:* duplicate-row sprawl.
- `test_confirm_stale_hash_refused` → wrong hash ⇒ `linked=False, link_stale=True`, **zero** link writes. *Catches:* recording a link for a block the agent never installed (the silent-corruption direction).
- `test_health_no_token_byte_identical` → `hive_health()` returns legacy snapshot with no `linked`/`link_repo` keys. *Catches:* onboarding leaking into the hot probe.

`test_init_writer.py`: `test_replace_marker_region_preserving_outside`, `test_append_when_no_markers`, `test_noop_when_version_and_hash_match`, `test_migrate_old_version_in_place`.

`test_hive_up.sh` (BATS/shell): fake `docker` on PATH scripting `starting→starting→healthy` ⇒ `cmd_up` exits 0 + prints the `call hive_init` line; never-healthy service ⇒ `_wait_healthy` exits non-zero **before** wall-clock + dumps logs.

**Mandatory mutation tests (RULE 2 — this is a state machine + credit-adjacent path):**
1. In the confirm path, flip `confirm_block_hash == current` to `!=` → `test_confirm_stale_hash_refused` MUST go red (a stale hash now records a link). Restore → green.
2. Change the link UPSERT to a plain INSERT → `test_confirm_idempotent` MUST go red (duplicate/raise). Restore → green.
3. Hard-code `trailer_key="Hive-Trace"` ignoring config → `test_trailer_key_from_config_not_hardcoded` MUST go red. Restore → green.
Report each fault + the catching test + green-after-restore.

## 9. What future change bolts on cleanly
- New harness → one `HookCatalog` entry; block is already harness-neutral.
- Block v2 (better guidance / new tool) → edit template, bump `block_v`; marker-replace migrates in place.
- Sidecar embedder/producer (the locked "later swap") → `docker-compose.yml` only; `hive_init` untouched.
- Producer watch-enrollment, when C10 lands → its OWN Phase-2 mechanism behind the producer port; `hive_init` keeps only the WARN note. (This is the deliberate seam left clean by rejecting B's coupling.)
- Link revocation → one `meta` key DELETE; no schema change.

## 10. Relevant files
- Authoritative spec: `/home/null/Desktop/work/hivemind/HIVEMIND_VMIN_SPEC.md` (C7, §9 Triggers row, §11 trailer, §12 phase split).
- Add `hive_init` schema: `/home/null/Desktop/work/AgentCortex/cls_memory/cls_memory/serving/mcp_tools.py` (`TOOL_DEFINITIONS`, verified list at :18).
- Add `_tool_init` + extend `_tool_health`: `/home/null/Desktop/work/AgentCortex/cls_memory/cls_memory/serving/mcp_server.py` (`_tool_handlers` :89; `_tool_health` :257; "tool count stays 11" discipline noted :200).
- Link store backing (NO new table): `/home/null/Desktop/work/AgentCortex/cls_memory/cls_memory/storage/persistence.py` (`SqliteMeta.get/set` UPSERT, verified :1148).
- Health type to extend: `/home/null/Desktop/work/AgentCortex/cls_memory/cls_memory/types.py` (`HealthSnapshot` TypedDict total=False, verified :459) and `service.py` `health()` (:1354).
- Marker/version convention reused from the user's own skills: `/home/null/.claude/skills/init-harness/SKILL.md`.

**Open questions:**
- Should the link record be keyed by repo_path alone (one link per repo, simplest) or by (repo_path, harness) (a repo opened from two harnesses keeps two links)? The synthesis uses sha256(repo_path) as the key; a multi-harness team in one repo would overwrite. Decide before Phase 1 if multi-harness-per-repo is in scope.
- Should hive_init be allowed to write HookSpec files at all in v-min, or is the everywhere-baseline strictly agent-initiated hive_write + a git post-commit hook (spec §9 residual: 'none on a bare MCP client')? The InstallPlan carries hooks as a typed field, but whether the agent applies them in the MVP vs. only the rules block is a product decision.
- What is the exact container HEALTHCHECK probe? It must touch the loaded embedder (not just process liveness) so './hive up' cannot declare healthy before the bge-small model is resident — but that probe must be cheap enough to run on Docker's healthcheck interval. Needs a measured cold-load time before fixing the interval/retries/start_period.
- Does hive_init need to read producer.stamp_trailer before any producer config group exists (it is build-new per §10)? In Phase 1 the trailer_key default ('Hive-Trace') must come from somewhere config-shaped even though the rest of producer.* is absent — decide whether to introduce the producer config group early (header only) just for stamp_trailer, or carry a standalone trailer_key until C10 lands.

---

## D5. Config system + provider registry for Hivemind v-min: how to shape the config tree, the three mandated swap seams (embedder / vector index / outcome producer), the never-hallucinate floor wiring, and the reload-tier enforcement.

- **Option A:** Port the monolithic CortexConfig dataclass tree trimmed to ~8 v-min groups; demote provider-selection strings into three plain dict[str,Callable] registries; pass the frozen cfg.recall object by identity to the (single) gate; drop the production=/LIVE_GEOMETRY dual-default machinery; provider selection is TOML-only because three groups share a `provider` field (inherits the CORTEX_D env-collision rule). Low-risk, maximal spec-fidelity, honestly heavier than 25 fields need.
- **Option B:** Four-layer resolver (BAKED<LIVE<FILE<ENV<OVERRIDES) with HIVE_GROUP__FIELD env namespacing that kills the CORTEX_D collision; a decorator-registered ProviderRegistry[T] class per port; a frozen Derived constants layer that computes the abstention floor ONCE and injects it into both the dense AND the (future §8.3) cascade gate so dual-gate drift is unrepresentable; an enforced tier guard that turns RELOAD_TIER into a reload() state machine refusing tier-D hot-swaps; a build-time authoritative-index assertion. Turns two named spec landmines structural, at the cost of a Derived god-object risk, verbose env keys, and a registry indirection hop.
- **Winner:** synthesis — The contrast revealed that A and B each own exactly one half of the right answer, and the deciding fact is one I verified in the spec: the cascade gate does NOT exist in v-min. Spec lines 77, 302-303, 546, 589 put hybrid BM25/channels/cross-encoder AND the second (cascade) gate behind §8.3 as a gated, default-OFF post-benchmark TODO. So B's headline differentiator — the Derived layer that makes dual-gate floor drift "structurally impossible" — defends a trap that is not in the shipping object graph. With one gate, A's move (pass the frozen cfg.recall OBJECT, by identity, to the one gate that exists) closes the identical drift class at ZERO added machinery, and A is honest that "in v-min the cascade collapses to the dense gate." Introducing a Derived god-object (B's own weakness #1: a coupling magnet the type system can't govern) to defend a one-gate system against a two-gate future is speculative generality — it violates APOSD #7's "somewhat general" caveat and the agent-native navigability budget for a benefit that doesn't pay until §8.3 lands. So I reject B's Derived layer for the v-min cut. BUT B also lands two present-tense wins that A genuinely lacks and that do NOT depend on the absent cascade gate: (1) The CORTEX_D env-ambiguity is a REAL verified reference bug (config.py:762-773 — colliding upper-cased keys skipped with a WARN, so the two most migration-dangerous tier-D fields are unsettable from env). A reproduces this exact collision class and CONCEDES it (A weakness #4: provider TOML-only). In a single-image Docker/12-factor product where the user's own standards mandate env config, that is a real cost. B's HIVE_GROUP__FIELD double-underscore namespacing kills the collision for EVERY field. I take it. (2) B turns RELOAD_TIER (verified real data at config.py:385) from documentation-only into an ENFORCED reload() state machine that refuses tier-D hot-swaps with the migration instruction — define-errors-out-of-existence (APOSD #11) and enforced-not-prose (agent-native §6), a deep-module win. A only "ports the tier table as pure data," leaving it prose-guarded. I take that too. On the swap mandate both pass; I keep A's plain-dict registries over B's decorator-registered ProviderRegistry class because at one provider per index port the decorator machinery is heavier than the mandate needs and the decorator registration is an agent-native Implicit-Wiring/navigability hop (B's own weakness #4), whereas a literal dict is greppable in one read — all providers visible at the registration site. The synthesis is therefore A's restraint as the skeleton (8 frozen groups, plain-dict registries, frozen cfg.recall passed by identity to the single gate, dual-default machinery deleted, Derived layer NOT built) + B's two enforced fixes grafted on (GROUP__FIELD env namespacing; the tier-guard state machine on the real RELOAD_TIER) + B's explicit authoritative-index build assertion. This beats pure-A because pure-A ships a known env-collision bug and a docs-only tier table; it beats pure-B because pure-B builds a god-object-risk Derived abstraction and a heavier registry to defend a trap (the cascade gate) the spec defers, paying speculative-generality cost the v-min LOC target (spec line 514: 4-6k vs 22k, 70-75% smaller) explicitly argues against. The §8.3 escalation note in the design tells the future implementer to introduce Derived AT THAT MOMENT — abstraction designed whole when the second consumer actually arrives (APOSD #15), not before.
- **Rejected because:** REJECTED from B — the Derived constants layer (as a v-min component): it is built to defend the dual-gate CONFIG_DRIFT trap, but the cascade gate is a §8.3 gated TODO that does not exist in the shipping v-min object graph (spec lines 77, 546, 589; channels default OFF, lines 302-303). With exactly one gate, drift is already closed by passing the frozen cfg.recall OBJECT (identity, not a copied float) to that gate — A's mechanism — at zero machinery. Derived is a self-admitted coupling magnet / god-object risk (B weakness #1) the type system cannot govern ("admit a field only when >=2 consumers" is human discipline, not an enforced contract), and it introduces the transient native_dim=-1 two-phase sentinel (B weakness #5). Building it now is speculative generality (APOSD #7 "somewhat" caveat; #15 design-the-abstraction-when-the-need-is-real) against the spec's 70-75% LOC-reduction mandate. Deferred, not deleted: the synthesis specifies introducing Derived at the moment §8.3 lands the second gate, with the test_gate_cannot_read_cfg contract pre-written so the cascade cannot reintroduce drift. REJECTED from B — the decorator-registered ProviderRegistry[T] class: at one provider per index port and two per producer port, the decorator+class indirection is heavier than the swap mandate requires and is an agent-native Implicit-Wiring hop (B weakness #4) — "which embedder runs" stops being a grep. A plain dict[str,Callable] keyed by group.provider satisfies the mandate (swap = one config line, new provider = one dict entry + one adapter), is greppable in a single read, and still fails fast on unknown keys via __post_init__ validation against the registry keyset. REJECTED from A — provider selection being TOML-only and the inherited CORTEX_D env-collision rule (A weakness #4): this reproduces a verified real reference bug (config.py:762-773) in a containerized 12-factor product whose own engineering standards mandate env-based config. B's HIVE_GROUP__FIELD namespacing is the correct fix and is adopted. REJECTED from A — leaving RELOAD_TIER as documentation-only "pure data": A names the tier table but does not enforce it; a tier-D geometry edit could be hot-applied and silently corrupt a warm store. B's enforced tier guard (refuse tier-D/C at the reload boundary with the migration instruction) is adopted. REJECTED from both — A's thinner ProducerConfig: B's is more spec-complete (carries epsilon_explore per §4.7 guardrail-1 which MUST stay >0, bugfix_pattern, stamp_trailer/require_stamp per §11); the synthesis uses B's fuller ProducerConfig group. REJECTED globally — the reference's production=True / LIVE_GEOMETRY dual-default machinery and the _LEGACY_FLAT_TO_GROUP back-compat block: greenfield has no library-vs-deployment split and no legacy flat readers; both A and B correctly drop these and the synthesis confirms it (removes ~120 lines and the two-names-for-one-knob hazard).

### Chosen design
# Chosen design — Config system + provider registry (synthesis: A's restraint + B's two enforced fixes)

## 0. Thesis
Eight frozen group dataclasses + one frozen root (`HiveConfig`), v-min optima baked as the dataclass defaults (no `production=` dual-default machinery). The three SWAPPABILITY ports — embedder / vector index / outcome producer — are each a **plain `dict[str, Callable]` registry** keyed by `group.provider`, resolved at the composition root. Env config uses **`HIVE_<GROUP>__<FIELD>` double-underscore namespacing** (kills the verified `CORTEX_D` collision bug). `RELOAD_TIER` is an **enforced** `reload()` state machine, not docs. The never-hallucinate floor is wired by passing the frozen `cfg.recall` **object** (by identity) to the single dense gate that exists in v-min; the `Derived` constants layer is **deferred to §8.3** (when the cascade gate's second consumer actually arrives), with its drift-forbidding test pre-written.

Grounding: ref `cls_memory/config.py` (groups/`from_flat`/`load`/`RELOAD_TIER`@385/`CORTEX_D` collision@762-773), `embedder.py` (`TextEmbedder` Protocol@37, `auto_embedder` if-ladder@399-425), `serving/gate_bundle.py` (`NormalizedEntropyGate.__init__(h_frac_max)`@82). Spec: §4 tiers, §4.3 approx_threshold trap (lines 290-295), §4.8 producer group, §5-D3/§6.6 floor drift (a §8.3-conditioned trap — cascade gate is gated-OFF per lines 77/546/589), §10 reuse/delete map, line 514 LOC target.

---

## 1. The group tree (`hive/config.py`)
```python
from __future__ import annotations
import logging, os
from dataclasses import dataclass, field, fields as _dc_fields
from typing import Any, Mapping, Optional

log = logging.getLogger("hive.config")

@dataclass(frozen=True)
class RuntimeConfig:                       # tier C (restart)
    db_path: str
    tenant_id: str = "local"               # single-tenant: constant label, never a key

@dataclass(frozen=True)
class GeometryConfig:                       # tier D (re-embed)
    d: int = 256                            # PCA-projected dense dim (FIXED GEOMETRY)
    native_dim: int = 384                   # bge-small native; validation only
    beta: float = 16.0                      # gate sharpness — MUST re-tune for dense cosine (§5-D2); tier B
    W_version: int = 1                      # bump on any tier-D change ⇒ re-embed migration

@dataclass(frozen=True)
class EmbeddingConfig:                       # tier D
    provider: str = "st"                    # registry key: "st" | "hash" | "openai"
    st_model_name: str = "BAAI/bge-small-en-v1.5"
    st_projection_head: str = "pca"         # "pca" only (random rejected, §4.1)
    projection_eps: float = 1e-3            # ZCA eigenvalue floor
    openai_model: str = "text-embedding-3-small"
    seed: int = 0

@dataclass(frozen=True)
class IndexConfig:                           # tier C
    provider: str = "exhaustive"            # registry key; AUTHORITATIVE
    authoritative: bool = True              # short-circuits any approx path (§4.3 structural fix)
    # NO approx_threshold here. Its absence + `authoritative` is the structural fix:
    # exhaustive is selected by name, the ranker gets the INSTANCE, growing N can never flip to ANN.
    hnsw_M: int = 16                        # inert until provider="hnsw"
    hnsw_ef_search: int = 200               # inert until provider="hnsw"

@dataclass(frozen=True)
class RecallConfig:                          # tier B (hot-swappable)
    H_frac_max: float = 0.5                 # the never-hallucinate floor (D3); validated (0,1]
    recall_top_n: int = 8                   # candidates returned (size-only, never gates)

@dataclass(frozen=True)
class ProducerConfig:                        # tier A — C10, §4.8 (fuller group from B)
    provider: str = "git"                   # registry key: "git" | "null"
    watch_repos: tuple[str, ...] = ()       # empty ⇒ idle, WARN-logged (loop starved, not broken)
    poll_interval_s: int = 300
    assoc_window_s: int = 1800
    settle_days: int = 7
    provisional_merge_reward: float = 0.2
    clawback_reward: float = -1.0
    epsilon_explore: float = 0.1            # §4.7 guardrail-1: ε-randomization, MUST stay > 0
    bugfix_pattern: str = r"^(fix|bug|hotfix|patch):"
    stamp_trailer: str = "Hive-Trace"
    require_stamp: bool = False

@dataclass(frozen=True)
class SecurityConfig:                         # tier A
    scan_secrets: bool = True               # deterministic credential scan BEFORE staging
    require_approval: bool = True           # recall reads status='approved' only (server-enforced)

@dataclass(frozen=True)
class ObservabilityConfig:                    # tier C
    telemetry_enabled: bool = True          # exposure ledger / outcome sink ON
    telemetry_db_path: Optional[str] = None # None ⇒ <db_dir>/telemetry.db
    log_level: int = logging.INFO

@dataclass(frozen=True)
class HiveConfig:                             # frozen root (CortexConfig → HiveConfig)
    runtime: RuntimeConfig
    geometry: GeometryConfig      = field(default_factory=GeometryConfig)
    embedding: EmbeddingConfig    = field(default_factory=EmbeddingConfig)
    index: IndexConfig            = field(default_factory=IndexConfig)
    recall: RecallConfig          = field(default_factory=RecallConfig)
    producer: ProducerConfig      = field(default_factory=ProducerConfig)
    security: SecurityConfig      = field(default_factory=SecurityConfig)
    observability: ObservabilityConfig = field(default_factory=ObservabilityConfig)

    def __post_init__(self) -> None:
        if not (0.0 < self.recall.H_frac_max <= 1.0):
            raise ValueError(f"H_frac_max must be in (0.0, 1.0], got {self.recall.H_frac_max!r}")
        if self.embedding.st_projection_head != "pca":
            raise ValueError("v-min requires st_projection_head='pca' (random rejected, §4.1)")
        if self.embedding.provider not in _EMBEDDER_REGISTRY:
            raise ValueError(f"embedding.provider must be one of {sorted(_EMBEDDER_REGISTRY)}")
        if self.index.provider not in _INDEX_REGISTRY:
            raise ValueError(f"index.provider must be one of {sorted(_INDEX_REGISTRY)}")
        if self.producer.provider not in _PRODUCER_REGISTRY:
            raise ValueError(f"producer.provider must be one of {sorted(_PRODUCER_REGISTRY)}")
```
**Deleted vs reference** (per §10): `SemanticConfig`, `ChannelsConfig`, `SalienceConfig`, `ConsolidationConfig` (the whole mutable-group exception — removing it removes the frozen-root-with-mutable-child smell), `PrivacyConfig`, `SecAggConfig`, `FederationConfig`, `RetentionConfig`, `LLMConfig`, `ValueGateConfig`, `DaemonConfig`; the `production=`/`LIVE_GEOMETRY` dual-default machinery; the `_LEGACY_FLAT_TO_GROUP` back-compat block (~120 lines, no legacy readers in greenfield). v-min targets are the dataclass defaults.

## 2. Two construction surfaces (`from_flat` ported in shape; `load` with B's env namespacing)
```python
@classmethod
def from_flat(cls, **flat: Any) -> "HiveConfig":
    """Pure flat→grouped builder; dataclass defaults; no env/TOML.
    Unknown key ⇒ TypeError (port of ref line 685). db_path required."""

@classmethod
def load(cls, *, db_path: str, tenant_id: str = "local",
         overrides_file: Optional[str] = None,        # default /data/hive.toml (named volume)
         env: Mapping[str, str] = os.environ,         # HIVE_<GROUP>__<FIELD>
         **overrides: Any) -> "HiveConfig":
    """Precedence low→high: dataclass defaults < hive.toml < HIVE_GROUP__FIELD env < overrides.
    Routes every flat key through from_flat. Corrupt TOML ⇒ defaults + WARN, never crashes."""
```
**Env namespacing (B's fix for the verified `CORTEX_D` bug, config.py:762-773):** `HIVE_GEOMETRY__D=256`, `HIVE_RECALL__H_FRAC_MAX=0.4`, `HIVE_EMBEDDING__PROVIDER=openai`. Double-underscore = group separator ⇒ every field reachable from env, no upper-case collision, `provider` settable from env (12-factor-correct for the Docker image). `load` logs every `HIVE_*` var seen-but-unrecognized at WARN (typo visibility; cannot hard-fail since it can't distinguish a typo from an unrelated env var).

## 3. The three swap seams — plain-dict registries (A's restraint over B's decorator class)
```python
# hive/ports.py
from typing import Protocol, Callable

class TextEmbedder(Protocol):            # ported verbatim, embedder.py:37
    d: int
    native_dim: int
    def embed(self, text: str) -> "Vec": ...
    def embed_batch(self, texts: list[str]) -> "np.ndarray": ...

class VectorIndex(Protocol):             # narrowed from storage/vector_index.py
    D: int
    def add(self, ids, vecs) -> None: ...
    def search(self, q, k: int): ...     # exact-knn; AUTHORITATIVE

class OutcomeProducer(Protocol):         # NEW, §11 / C10
    def poll(self) -> list["TaskOutcome"]: ...

# hive/registry.py — one greppable dict per port; all providers visible at the registration site.
_EMBEDDER_REGISTRY: dict[str, Callable[..., TextEmbedder]] = {
    "st":     lambda cfg: SentenceTransformerEmbedder(
                  model_name=cfg.embedding.st_model_name, d=cfg.geometry.d,
                  seed=cfg.embedding.seed, projection_head=cfg.embedding.st_projection_head),
    "hash":   lambda cfg: HashingNgramEmbedder(d=cfg.geometry.d, seed=cfg.embedding.seed),
    "openai": lambda cfg: OpenAIEmbedder(model=cfg.embedding.openai_model, d=cfg.geometry.d),
}
_INDEX_REGISTRY: dict[str, Callable[..., VectorIndex]] = {
    "exhaustive": lambda cfg: ExhaustiveCosineIndex(D=cfg.geometry.d),
    # "hnsw": lambda cfg: HnswIndex(D=cfg.geometry.d, M=cfg.index.hnsw_M, ef_search=cfg.index.hnsw_ef_search),
    #   ↑ a future entry, not shipped; ANN is unreachable until someone writes it here AND in TOML.
}
_PRODUCER_REGISTRY: dict[str, Callable[..., OutcomeProducer]] = {
    "git":  lambda cfg, store: GitOutcomeProducer(
                store, watch_repos=cfg.producer.watch_repos, poll_interval_s=cfg.producer.poll_interval_s,
                assoc_window_s=cfg.producer.assoc_window_s, settle_days=cfg.producer.settle_days,
                bugfix_pattern=cfg.producer.bugfix_pattern, stamp_trailer=cfg.producer.stamp_trailer,
                require_stamp=cfg.producer.require_stamp),
    "null": lambda cfg, store: NullProducer(),
}
```
**Composition root** (`hive/composition.py`, replaces the ref if-ladder + `_build_vector_index` switch):
```python
def build_runtime(cfg: HiveConfig, store: "Store") -> "Runtime":
    embedder = _EMBEDDER_REGISTRY[cfg.embedding.provider](cfg)
    index    = _INDEX_REGISTRY[cfg.index.provider](cfg)
    producer = _PRODUCER_REGISTRY[cfg.producer.provider](cfg, store)

    if embedder.d != cfg.geometry.d:                      # dim-mismatch guard, port of service.py:339
        raise ValueError(f"embedder.d={embedder.d} != geometry.d={cfg.geometry.d}; store corruption risk")

    # §4.3 authoritative-index assertion (B's structural fix made explicit):
    if cfg.index.provider == "exhaustive" and cfg.index.authoritative:
        assert isinstance(index, ExhaustiveCosineIndex), "authoritative exhaustive must be exact"
        # the ranker is handed the INSTANCE, not the backend string ⇒ 'grow N → silently ANN' is not a path.

    # never-hallucinate floor: the SINGLE gate that exists in v-min gets the frozen cfg.recall OBJECT.
    dense_gate = NormalizedEntropyGate(h_frac_max=cfg.recall.H_frac_max)  # ref gate_bundle.py:82
    # §8.3 DEFERRED: when the cascade gate lands, introduce hive/config/derived.py::Derived,
    # compute abstention_floor=cfg.recall.H_frac_max ONCE, inject into BOTH gates, and forbid
    # gates from reading cfg (test_gate_cannot_read_cfg, already written below). Do NOT build it now.
    return Runtime(embedder=embedder, index=index, producer=producer, dense_gate=dense_gate)
```
A swap is one config line (TOML or `HIVE_*__PROVIDER` env). New provider = one dict entry + one adapter file; the composition root, the config tree, and every fake-using test are untouched.

## 4. Reload-tier guard — `RELOAD_TIER` ENFORCED (B's fix, the deep win A left as docs)
```python
# hive/config/reload.py
RELOAD_TIER: dict[str, str] = {            # trimmed from ref config.py:385 to surviving fields
    "geometry.d": "D", "geometry.native_dim": "D", "geometry.W_version": "D",
    "embedding.provider": "D", "embedding.st_model_name": "D", "embedding.st_projection_head": "D",
    "index.provider": "C", "observability.telemetry_enabled": "C", "runtime.db_path": "C",
    "geometry.beta": "B", "recall.H_frac_max": "B", "recall.recall_top_n": "B",
    "producer.settle_days": "A", "producer.watch_repos": "A", "security.scan_secrets": "A",
    # ... every flat field has an entry; test_tier_table_covers_every_field enforces totality.
}
class TierViolation(Exception): ...

def classify_change(old: HiveConfig, new: HiveConfig) -> str:
    """Highest reload tier among changed group.field pairs. O(F) single pass, F≈30 fixed."""
    worst = "A"
    for dotted in iter_flat_fields(new):                  # raises KeyError if a field lacks a tier
        if get_flat(old, dotted) != get_flat(new, dotted):
            worst = max(worst, RELOAD_TIER[dotted], key="ABCD".index)
    return worst

def apply_reload(server, new: HiveConfig) -> "ReloadResult":
    tier = classify_change(server.cfg, new)
    if tier == "D":
        log.error("refused tier-D hot-reload", extra={"tier": "D"})
        raise TierViolation("tier-D (geometry/embedder) needs a re-embed: bump geometry.W_version "
                            "and run hive-migrate; do not hot-reload")
    if tier == "C":
        raise TierViolation("tier-C change needs a process restart")
    if tier == "B":
        server.swap_cfg(new)                              # atomic frozen-pointer flip; gate rebuilt from new
        log.info("hot-reloaded tier-B fields", extra={"tier": "B"})
    return ReloadResult(tier=tier, applied=(tier in ("A", "B")))
```
Tier-D is **refused at the boundary** with the migration instruction (APOSD #11 define-errors-out-of-existence; agent-native §6 enforced-not-prose) — a config edit alone can never corrupt a warm store.

## 5. Failure-mode logging points (per the user's logging mandate)
- `load`: WARN on corrupt/missing TOML (degrade to defaults, never crash); WARN per unrecognized `HIVE_*` env var; INFO on resolved config summary (no secrets — log provider names, dims, floor, NOT keys/paths-with-creds).
- `__post_init__`: the `ValueError`s above are startup fail-fast (missing/invalid → process exits, per secrets/fail-fast rule).
- `build_runtime`: ERROR on dim-mismatch before raising; WARN if `producer.watch_repos == ()` (loop idle); INFO on each port built with its provider name (the structured "dependencies resolved" checkpoint).
- `apply_reload`: ERROR on refused tier-D/C with the field set that triggered it; INFO on applied tier-B with the changed fields.
- Producer adapter boundary (separate module): WARN on git/CI poll failure + retry/backoff; never on hot path.

## 6. Test-first contract (failing tests BEFORE the modules exist; all run against fakes)
| Test (file::name) | Assertion | Failure caught |
|---|---|---|
| `test_config.py::test_defaults_are_vmin_geometry` | `geometry.d==256`, `embedding.st_projection_head=="pca"`, `recall.H_frac_max==0.5`, `index.authoritative is True` | a default drifted off FIXED GEOMETRY/floor |
| `test_config.py::test_from_flat_unknown_key_raises` | `pytest.raises(TypeError, match="unexpected config field")` | typo'd key silently ignored (ref:685) |
| `test_config.py::test_h_frac_max_range_validated` | raises for `0.0` and `1.5` | never-hallucinate floor out of range |
| `test_config.py::test_projection_head_must_be_pca` | `from_flat(..., st_projection_head="random")` raises | rejected random head re-admitted |
| `test_config.py::test_env_group_namespacing_no_collision` | `HIVE_GEOMETRY__D=256` sets `geometry.d`; no skip/WARN | the `CORTEX_D` ambiguity regression — the bug this design kills |
| `test_config.py::test_load_precedence_env_over_toml` | TOML `H_frac_max=0.4` + `HIVE_RECALL__H_FRAC_MAX=0.6` ⇒ `0.6` | precedence inverted |
| `test_config.py::test_load_toml_parse_error_degrades` | corrupt TOML ⇒ defaults + WARN, no crash | bad config file kills the server |
| `test_registry.py::test_embedder_swaps_via_config` | `provider="hash"` ⇒ `HashingNgramEmbedder` | swap seam ignores the config string |
| `test_registry.py::test_unknown_provider_fails_fast` | unknown provider ⇒ `ValueError` listing valid keys | typo'd provider silently defaults |
| `test_registry.py::test_service_accepts_fake_embedder` | inject `FakeEmbedder(d=256)`; `runtime.embedder is fake`, no registry lookup | injection seam broke (the TDD ergonomic) |
| `test_registry.py::test_dim_mismatch_raises` | `FakeEmbedder(d=128)` vs `geometry.d=256` ⇒ `ValueError` | swapped embedder corrupts blobs (ref:339) |
| `test_index_auth.py::test_exhaustive_ignores_n_growth` | write 50 episodes, `authoritative=True`, recall ⇒ exact top-1 | §4.3 ANN flip at N>threshold |
| `test_floor.py::test_single_gate_reads_cfg_recall_floor` | `runtime.dense_gate.h_frac_max == cfg.recall.H_frac_max` | floor not wired to the gate |
| `test_floor.py::test_cascade_drift_guard_pre_written` (xfail until §8.3) | `inspect.signature(GateBase.__init__)` has no `cfg`/`config` param | the §8.3 second-gate drift door, pre-closed |
| `test_reload.py::test_tierD_change_refused` | change `embedding.provider` ⇒ `TierViolation` mentioning `W_version` | geometry edit silently corrupting a warm store |
| `test_reload.py::test_classify_returns_highest_tier` | one A field + one D field ⇒ `"D"` | mixed-tier change under-classified, hot-applied |
| `test_reload.py::test_tier_table_covers_every_field` | `set(iter_flat_fields()) == set(RELOAD_TIER)` | a new field with no tier ⇒ CI fail (shift-left) |

**Mutation tests (RULE 2 — name the fault, the red test, restore, green):**
- *Fault:* `__post_init__` floor check to `0.0 <= ...` (accept 0.0). *Red:* `test_h_frac_max_range_validated`. Restore→green. Proves the never-hallucinate floor bound is live.
- *Fault:* drop `"hash"` from `_EMBEDDER_REGISTRY`. *Red:* `test_embedder_swaps_via_config` (`KeyError`). Proves the swap seam honors the config string.
- *Fault:* weaken `RELOAD_TIER["embedding.provider"]` `"D"`→`"B"`. *Red:* `test_reload.py::test_tierD_change_refused` (embedder swap would hot-apply). Proves the tier table is the enforced input to the guard, not docs.
- *Fault:* in `build_runtime`, pass `index` by backend-string instead of the instance / drop the `authoritative` assertion. *Red:* `test_exhaustive_ignores_n_growth`. Proves the approx-flip trap door is removed from the object graph.

Fakes are trivial (each Protocol is `embed/embed_batch/d/native_dim`; `add/search/D`; `poll`); no fake constructs a `HiveConfig`. The `test_cascade_drift_guard_pre_written` test is the one piece of B's insight kept now-for-cheap: it pre-forbids any future gate from reading `cfg`, so when §8.3 lands the second gate, `Derived` is introduced against an already-green contract — the abstraction designed whole at the moment its second consumer arrives (APOSD #15), not speculatively before.

## 7. What bolts on cleanly
- **New embedder** (vLLM/bge-large/hosted, D4 escalation): one `_EMBEDDER_REGISTRY` entry + one adapter + (if a new knob) one `EmbeddingConfig` field; pairs with a `W_version` bump + ported `ops/migration.py` reembed-from-text.
- **Sidecar embedder/producer** (locked "later swap"): a `RemoteEmbedder`/`WebhookProducer` adapter is just another registry entry; the core sees only the Protocol; the single-image build + named volume unaffected.
- **ANN when N grows**: add `"hnsw"` to `_INDEX_REGISTRY` (the commented stub) + flip TOML; `hnsw_M`/`ef_search` are already present-but-inert; authoritative-exhaustive default never changes so no auto-flip.
- **§8.3 cascade gate + hybrid channels**: introduce `Derived` THEN (the deferred B mechanism), inject `abstention_floor` into both gates; `test_cascade_drift_guard_pre_written` is already green so drift cannot be reintroduced.
- **Reserved L1/L2 + shadow config-controller (§9, inert)**: read the same frozen `HiveConfig`; the controller proposes `from_flat(**delta)` snapshots replayed through the eval membrane; the frozen root IS the atomic hot-swap pointer; the tier guard already refuses unsafe deltas.

## 8. Why this beats both pure options
Pure-A ships a known env-collision bug (provider TOML-only, the inherited `CORTEX_D` rule) in a 12-factor Docker image, and leaves `RELOAD_TIER` documentation-only so a tier-D edit can silently corrupt a warm store. Pure-B builds a `Derived` god-object-risk layer and a heavier decorator registry to defend the dual-gate drift trap — but that trap's second gate is a §8.3 gated-OFF TODO absent from the v-min object graph (spec 77/546/589), so it is speculative generality against the 70-75% LOC-reduction mandate (spec line 514). The synthesis keeps A's deep, narrow, fake-friendly skeleton and its restraint (no `Derived`, plain-dict registries, dual-default machinery deleted), grafts on B's two genuinely-present-tense fixes (`GROUP__FIELD` env namespacing; the enforced tier-guard state machine on the real `RELOAD_TIER`) and B's explicit authoritative-index assertion, and pre-writes the one cheap test that lets the deferred `Derived` land safely the day §8.3 needs it.

**Open questions:**
- Env precedence vs the never-hallucinate floor: should recall.H_frac_max be settable from HIVE_RECALL__H_FRAC_MAX at all, or pinned to TOML/overrides-only so an env typo can't loosen the floor below 0.5? The __post_init__ range check (0,1] catches out-of-range but not an in-range loosening (e.g. 0.9). Resolve in the security review.
- Tier of index.provider: spec §4.3 says index is tier C (restart). The reload guard refuses it as C — confirm an operator flipping exhaustive→hnsw is expected to take a restart (vs the design's comment implying a TOML flip is enough). Align the comment and the RELOAD_TIER entry.
- B's epsilon_explore (§4.7 guardrail-1, MUST stay >0) is carried on ProducerConfig but not validated in __post_init__. Add a `producer.epsilon_explore > 0` assertion? It guards the explore/exploit guardrail the verifiable-credit loop depends on — recommend yes.
- Where does the secret-scan (security.scan_secrets) live relative to config — is it a config flag only, or does config also need to fail-fast if scan_secrets=False is set in a production image? The product invariant is secret-safe-always; consider making scan_secrets immutable/ignored rather than a config knob.

---

## D6. Outcome producer (git watcher) packaging + the trace<->outcome join for Hivemind v-min component C10. Two designs: A = one in-process reducer (`OutcomeProducer.step(now) -> ProducerTick`) over a single read-only `GitFacts` Protocol, with the §11 join (associate/settle/clawback) baked into the tick; B = a two-part cut — an `OutcomeSource` port that yields normalized `GitFacts` (all I/O quarantined) feeding a separate PURE `OutcomeJoiner` state machine (all §11 policy, clock-injected, git-free), wired by a thin `ProducerTick` driver.

- **Option A:** In-process tick, single-writer-shared, join baked into the tick. One DEEP module `OutcomeProducer.step(now) -> ProducerTick` is the whole new mechanism: the server's existing single-writer loop calls it on `producer.poll_interval_s`, exactly relocating the drain that lives in `consolidate()` today (service.py:942, Fix #1). It runs the three §11 hops in a fixed, safety-load-bearing order (associate -> settle -> clawback -> emit_to_sink -> drain). Git enters through ONE read-only `GitFacts` Protocol (the named swap axis) returning frozen dataclasses (CommitFact/RevertFact/MergeFact) — never a live git handle. `step()` never raises (per-repo try/except -> tick.errors+1, loop liveness is an asserted invariant). Idempotent via a per-repo `producer_head` watermark. Two adapters: SubprocessGitFacts (git CLI, read-only verb allowlist) and FakeGitFacts (in-memory, the millisecond test substrate). 15 named tests + 5 mutation tests (M1 blame-guard, M2 settle-clock, M3 ordering, M4 watermark, M5 squash).
- **Option B:** `OutcomeSource` port + pure `OutcomeJoiner`. The cut explicitly separates the swap axis from the test axis by making them the SAME line. `OutcomeSource` (Protocol, `poll(since_ts) -> Iterator[GitFacts]` + `blame_spans(sha, paths)`) is the ONLY component that touches a VCS — all subprocess/filesystem/network/porcelain-parsing/squash-resolution is sealed behind it (LocalGitOutcomeSource default; WebhookOutcomeSource future). `OutcomeJoiner` is a PURE, clock-injected object that holds the ENTIRE §11 policy (window/stamp association, the provisional->settled_pos|clawed_back state machine, blame-OVERLAP DECISION, family derivation, squash survival) and is exhaustively unit-testable against hand-built `GitFacts` with zero git, zero clock, zero subprocess. A thin `ProducerTick` driver wires source->joiner->existing apply_outcomes_from_sink in-process under the single-writer CAS. The normalized `GitFacts` dataclass (with FactKind enum, BlameSpan, squashed_from, stamp_trace_ids) is the contract crossing the port; it carries no git-library types so the Joiner never imports a VCS. ~17 pure Joiner tests + a parametrized Source-CONTRACT test run against every adapter + a separate tmp-repo test for LocalGitOutcomeSource normalization only. The one named awkward seam: blame computation is git (Source) but the overlap decision is policy (Joiner), forcing a `blame_spans` round-trip or a pre-attach of `touched_blame`.
- **Winner:** synthesis — The two designs are not far apart — they share the same Protocol-as-swap-seam, the same frozen-dataclass-across-the-boundary discipline, the same in-process single-writer placement, the same downstream surfaces, and substantially the same test+mutation matrix. The contrast is purely about WHERE the §11 join logic lives relative to the I/O boundary, and on that single axis B is decisively right while A's `step()` orchestration is decisively right. The synthesis takes B's purity cut and re-houses A's tick orchestration on top of it.

B wins the load-bearing axis on three rubric grounds. (1) Information hiding / leakage (red-flags.md "Information Leakage", principles.md #16 separate what matters): in A, `OutcomeProducer.step()` both shells out (via GitFacts, fine) AND owns the join, so the riskiest, most-likely-wrong code in the entire system — the settlement state machine and the blame-overlap clawback that §6.1.6 makes a HARD ship gate with a mandatory mutation test — is co-located with the thing that varies per deployment. B's cut puts every §11 policy branch on the PURE side and seals ALL impurity (subprocess, porcelain drift, squash-resolution, locale/detached-HEAD) behind one `poll()`. The spec itself calls the parser "the largest surface area of new bug-risk and the place the §9 threat boundary is actually enforced" (A's own weakness #2) — B is the only design that keeps that surface from touching the credit policy. (2) Testability as an ENFORCED contract (agent-native.md §6, the TDD-first mandate): both test against fakes, but B's Joiner is pure and CLOCK-INJECTED, so the §6.1.6 acceptance gate — including the three mandatory guard tests (false-positive blame, squash survival, drain-on-tick) and the RULE-2 blame-overlap mutation target — lands entirely on a git-free, time-free object. A also injects a clock and uses FakeGitFacts, but its mutation targets (M1 blame-guard, M3 ordering) live inside a `step()` that is interleaved with per-repo git iteration and watermark side-effects, so the unit under mutation is larger and less isolated. The spec's own framing ("the riskiest new mechanism... both correctly decoupled along the mandated swap axis AND exhaustively unit-testable without a live git repo") is B's exact thesis: the swap axis and the test axis are the same cut. (3) How cleanly future functionality bolts on (principles.md #15 increments-are-abstractions): the named §4.7/§8.1 future signals (CI-status, deploy-success, incident-tracker clawback, the sidecar/webhook producer the compose file is staged for) all bolt onto B with ZERO change to the state machine because the Joiner is signal-source-agnostic; A reaches the same end state but its sidecar story requires `step()` to start returning a WriteBatch over RPC, i.e. a refactor of the orchestrator, not a new adapter.

But A is NOT merely the loser — A's `step()` is the better ORCHESTRATION primitive and B under-specifies exactly there. A's three decisive contributions that B lacks or muddies: (i) the FIXED, asserted hop ORDER (associate -> settle -> clawback -> emit -> drain) is a genuine safety property — A proves a provisional `+` reverted in the same tick nets to the clawback (T-ORDER / mutation M3), which B never makes explicit (B even admits "settle_sweep assumes monotone arrival" without pinning the within-tick race); (ii) the `ProducerTick` audit dataclass (associated/settled/clawed_back/drained/stamp_hits/window_assoc/errors) is a precise ENFORCED contract that directly feeds the §12 Phase-2 readiness gate (stamp-hit-rate + credit density) and the §6/global-CLAUDE structured-failure-logging mandate — B's thin driver returns bare ints and never surfaces the gate signal as a typed record; (iii) the explicit `step()`-never-raises liveness invariant with per-repo try/except and the `errors` counter (T-LIVENESS) operationalizes §4.8's "loop starved, not broken" as a tested guarantee on the orchestrator, where B states it only as prose on the Source contract.

So neither dominates: B owns the cut, A owns the tick. The synthesis is forced and clean because the two contributions are orthogonal — B's purity boundary sits UNDER A's orchestration. The winning design is B's `OutcomeSource` (sealed I/O) + `OutcomeJoiner` (pure policy) cut, driven by A's `OutcomeProducer.step(now) -> ProducerTick` as the in-process single-writer tick: a thin orchestrator that owns the fixed hop order, the typed ProducerTick audit record, the per-repo liveness/error containment, and the Fix-#1 drain, while delegating every git fact to `source.poll()` and every credit decision to `joiner.ingest()/settle_sweep()`. This keeps the swap axis = test axis = pure-policy axis (B's win) AND gives the orchestrator a deep, narrow, enforced-contract surface with a tested ordering invariant and a gate-signal audit record (A's win), at no cost to either.
- **Rejected because:** REJECTED — A as a standalone design: its single `OutcomeProducer.step()` conflates orchestration with the §11 credit policy, co-locating the system's highest-risk code (settlement state machine + blame-overlap clawback, the §6.1.6 mutation-test target) with the per-deployment-variable git iteration. This is the Special-General Mixture / Information-Leakage failure: the blame OVERLAP DECISION (pure policy that MUST be mutation-tested) ends up inside the same method that drives `git blame` subprocess calls, so the mutation unit is larger and the §9 trust boundary (untrusted porcelain) sits closer to the credit math than necessary. A's own honest weaknesses #1 (blocking tick stalls the single writer) and #2 (subprocess parser is the largest new bug surface) are real, but the synthesis inherits A's mitigations (poll_interval=300, off-hot-path, subprocess timeout -> errors+1) unchanged; what the synthesis FIXES is that in A those risks are entangled with the credit policy, whereas the synthesis seals them behind `poll()`. A's later-sidecar path also requires reworking `step()` into an RPC'd WriteBatch producer (a refactor), versus the synthesis just dropping in a new `OutcomeSource` adapter.

REJECTED — B as a standalone design: B nails the cut but under-specifies the orchestrator and has two named soft spots the synthesis closes by importing A. (1) B's `ProducerTick` driver "owns no logic, only orchestration" and returns bare `int`s — it never makes the within-tick hop ORDER an asserted safety property (the settle-then-clawback netting that A's T-ORDER + mutation M3 pin down), and B explicitly hand-waves ordering ("settle_sweep assumes monotone arrival; out-of-order tolerated but logged"). For a self-modifying credit loop where a stale `+` surviving a revert is exactly the gameable-positive leak the spec is built to prevent (§6.6 anti-gaming), an UNasserted order is a latent correctness hole. The synthesis adopts A's fixed, tested order. (2) B surfaces no typed audit record, so the §12 Phase-2 readiness gate signal (stamp-hit-rate, credit density) and the §4.8/global structured-failure-logging mandate have no enforced contract to ride on; the synthesis adopts A's `ProducerTick` dataclass. (3) B's genuinely awkward seam — blame COMPUTATION is git (Source) but the overlap DECISION is policy (Joiner) — is handled honestly by B (named `blame_spans` port method, or pre-attach `touched_blame`), and the synthesis keeps B's resolution (pre-attach the original commit's introduced-lines as `touched_blame` on the BUGFIX fact so the Joiner stays pure data, with `blame_spans` as the explicit fallback port method), so the dependency is named and testable rather than smuggled. B's git-shaped `GitFacts` vocabulary (weakness #2) is correctly judged YAGNI-acceptable for the MVP (the only verifiable signal IS git) and the synthesis does not prematurely generalize it.

### Chosen design
# C10 Outcome Producer + Trace<->Outcome Join — Chosen Design (Synthesis: B's cut, A's tick)

## Thesis
The producer is one in-process tick that owns ORCHESTRATION and delegates everything else across a single information-hiding boundary. Two seams, one driver:
- **`OutcomeSource` (PORT)** — the ONLY component that touches a VCS. All impurity (subprocess, porcelain parsing, squash resolution, blame computation) is sealed here. This is the named swap axis.
- **`OutcomeJoiner` (PURE module)** — the ENTIRE §11 credit policy (window/stamp association, the provisional->settled_pos|clawed_back state machine, blame-OVERLAP decision, family derivation, squash survival). Clock-injected, git-free, exhaustively unit-testable in milliseconds. This is the test axis = the swap axis = the policy axis, all one cut (B's win).
- **`OutcomeProducer.step(now) -> ProducerTick` (DRIVER)** — the thin in-process tick the single-writer loop calls on `producer.poll_interval_s`. Owns the fixed, asserted hop ORDER, the typed audit record, per-repo liveness/error containment, and the Fix-#1 drain (A's win).

Direction of dependency: `OutcomeProducer -> OutcomeSource` (facts) and `OutcomeProducer -> OutcomeJoiner` (policy); `OutcomeJoiner -> {ExposureLedger, TaskOutcomeStore, TelemetrySink}` (existing C9 surfaces). The Joiner NEVER imports a VCS; the Source NEVER imports the Joiner.

```
 git repos ─▶ OutcomeSource (PORT, all I/O) ─poll()─▶ [GitFacts: pure frozen data]
                                                          │
                       OutcomeProducer.step(now) ─────────┤  (driver: order + audit + liveness + drain)
                                                          ▼
                                          OutcomeJoiner (PURE policy, clock-injected)
                                          ingest()/settle_sweep()/_clawback()
                                                          │ Reward rows
                          existing C9: TelemetrySink.record -> apply_outcomes_from_sink -> Beta-Bernoulli posterior
```

## 1. The port (swap axis) — `OutcomeSource`

```python
# hive/outcome/source.py
from typing import Protocol, runtime_checkable, Iterator

@runtime_checkable
class OutcomeSource(Protocol):
    """Yields normalized GitFacts. The ONLY component permitted to touch a VCS.
    Mirrors embedder.TextEmbedder (@runtime_checkable Protocol + auto factory).

    Behavioral contract (enforced by tests/test_outcome_source_contract.py,
    parametrized over EVERY adapter):
      - poll(since_ts) is idempotent: re-poll same window -> facts with identical
        (repo, sha, kind) identity; the Joiner dedups on those (no double-credit).
      - NEVER raises into the caller: a git/network failure yields () + logs WARN
        ('loop starved, not broken' — §4.8 watch_repos-empty rule).
      - resolves squash: a squash-merge MUST be emitted as a MERGE fact whose
        squashed_from lists the folded SHAs, so trailer + blame target survive.
      - pre-attaches blame: a BUGFIX/REVERT fact carries touched_blame (the lines
        the ORIGINAL commit introduced ∩ this fix's changed lines) so the Joiner's
        overlap DECISION stays pure data. blame_spans() is the explicit fallback.
    """
    def poll(self, since_ts: int) -> Iterator["GitFacts"]: ...
    def blame_spans(self, sha: str, paths: tuple[str, ...]) -> tuple["BlameSpan", ...]: ...
```

### Normalized vocabulary crossing the port (the contract — carries NO git-library types)

```python
# hive/outcome/facts.py
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum

class FactKind(str, Enum):
    COMMIT = "commit"   # landed on a watched ref
    MERGE  = "merge"    # reached integration branch (incl. squash)
    REVERT = "revert"   # reverts an earlier SHA
    BUGFIX = "bugfix"   # subject matches producer.bugfix_pattern

@dataclass(frozen=True, slots=True)
class BlameSpan:
    path: str
    sha: str                       # commit blamed as INTRODUCING these lines
    lines: frozenset[int]          # 1-based; Decision B enforced on this, never path equality

@dataclass(frozen=True, slots=True)
class GitFacts:
    repo: str                       # producer.watch_repos entry (family: git-remote)
    sha: str                        # the §3 task_outcomes.task_ref join key
    kind: FactKind
    subject: str                    # first line (bugfix_pattern matched here)
    author_ts: int                  # epoch s
    merged_ts: int | None           # epoch s when it reached integration; None until merged
    files_touched: tuple[str, ...]  # coarse same-file PREFILTER only, never the clawback decision
    languages: tuple[str, ...]      # dominant exts (family: language)
    stamp_trace_ids: tuple[str, ...]     # parsed Hive-Trace trailer (override set); () if absent
    reverts_sha: str | None         # for REVERT: the SHA being reverted
    touched_blame: tuple[BlameSpan, ...] # PRE-ATTACHED: original-introduced ∩ this-fix-changed lines
    squashed_from: tuple[str, ...]  # source SHAs folded into a squash-merge (join survives squash)
```

### Adapters (config-selected, like auto_embedder)
```python
# hive/outcome/factory.py
def make_outcome_source(cfg) -> OutcomeSource:
    return {
        "local_git": LocalGitOutcomeSource,   # MVP default — git CLI, read-only verb allowlist
        "fake":      FakeOutcomeSource,        # scripted GitFacts, the test substrate
        # "webhook": WebhookOutcomeSource,     # LATER sidecar swap — compose file staged for it
    }[cfg.producer.source_kind](cfg)
```
- **`LocalGitOutcomeSource`** — the ONLY file that runs subprocess / touches `.git`. Uses git PLUMBING not porcelain (`git rev-list`, `git log --format=%H%x00…` NUL-delimited, `git blame -L --line-porcelain`), each via `subprocess.run(..., timeout=…)` (the proven llm.py:73 pattern), parsed into frozen dataclasses, read-only verb allowlist (the §9 threat boundary, enforced in the adapter not by trusting the repo). Imports `facts.py` and nothing of the Joiner.
- **`FakeOutcomeSource(scripted_facts: list[GitFacts])`** — in-memory, deterministic; no repo, no disk, no clock.

## 2. The pure policy module — `OutcomeJoiner`

```python
# hive/outcome/joiner.py
@dataclass(frozen=True, slots=True)
class Reward:
    trace_id: str          # sink key, joins exposure
    task_ref: str          # commit SHA
    family_scope: str      # git-remote × language × workflow (one denormalized string)
    utility: float         # +0.2 provisional->settled, or −1.0 clawback (§4.7)
    ts: float

@dataclass(frozen=True, slots=True)
class JoinerCounts:        # structured per-call counts -> driver aggregates into ProducerTick
    associated: int; settled: int; clawed_back: int
    stamp_hits: int; window_assoc: int   # Phase-2 gate signal originates HERE

class OutcomeJoiner:
    def __init__(self, *, exposure: ExposureLedger, outcomes: TaskOutcomeStore,
                 sink: TelemetrySink, cfg: ProducerConfig,
                 clock: Callable[[], float], log=...): ...

    def ingest(self, facts: GitFacts) -> JoinerCounts:
        """HOP 2 (+ inline HOP 3b for REVERT/BUGFIX). Pure. Writes provisional
        task_outcomes rows for COMMIT/MERGE; routes REVERT/BUGFIX to _clawback.
        Idempotent on (repo, sha, kind)."""

    def settle_sweep(self, now: float) -> JoinerCounts:
        """HOP 3a. provisional + -> settled_pos once settle_at<=now AND not clawed;
        emits Reward(utility=+0.2*margin*(1-ε)) to the sink. CAS on row.version."""

    def _clawback(self, facts: GitFacts) -> JoinerCounts:
        """HOP 3b. REVERT: immediate −1.0 on reverts_sha's rows. BUGFIX: −1.0 ONLY
        when facts.touched_blame is non-empty (the pre-attached changed∩introduced
        intersection) — a same-file disjoint-line edit yields empty touched_blame
        and does NOT clawback. THIS is the §6.1.6 mutation target (pure side)."""
```

### Family derivation (resolves the §2/§11 hand-wave; pure, O(1), one tested function)
`family_scope = f"{facts.repo}:{dominant_lang(facts.languages)}:{workflow(facts)}"` where `workflow = bugfix` if `bugfix_pattern.match(facts.subject)`, `dep-upgrade` if files are manifest-only (`package.json`/`requirements.txt`/`go.mod`), else `general`. One denormalized string, on the credit event, never on the episode.

### Association policy (Decision A: window-primary, stamp override)
For each COMMIT/MERGE fact: if `stamp_trace_ids` -> credit exactly that set at `STAMP_WEIGHT` (skip the margin discount, it is the precise set); elif `cfg.require_stamp` -> drop (0 rows); else -> `exposure.traces_in_window(end=author_ts, window_s=assoc_window_s)` at `WINDOW_WEIGHT`, each row `reward = provisional_plus * recall_margin * (1-ε)`. `task_ref` canonicalized to the LANDED SHA (via `squashed_from`) at link time so the join survives squash.

## 3. The driver — `OutcomeProducer.step()` (A's tick on B's cut)

```python
# hive/outcome/producer.py
@dataclass(frozen=True, slots=True)
class ProducerConfig:           # §4.8 producer.* + §4.7 reward constants
    watch_repos: tuple[str, ...] = ()
    source_kind: str = "local_git"
    poll_interval_s: int = 300
    assoc_window_s: int = 1800
    stamp_trailer: str = "Hive-Trace"
    bugfix_pattern: str = r"^(fix|bug|hotfix|patch):"   # + BUG-NN/regression/crash/race (compiled)
    require_stamp: bool = False
    settle_days: float = 7.0
    provisional_plus: float = 0.2
    clawback: float = -1.0          # fixed, §4.7
    epsilon: float = 0.1

@dataclass(frozen=True, slots=True)
class ProducerTick:             # the ENFORCED audit contract — feeds §12 Phase-2 gate + structured logging
    associated: int = 0; settled: int = 0; clawed_back: int = 0; drained: int = 0
    stamp_hits: int = 0; window_assoc: int = 0; errors: int = 0
    def __add__(self, c: JoinerCounts) -> "ProducerTick": ...   # aggregates Joiner counts

class OutcomeProducer:
    def __init__(self, source: OutcomeSource, joiner: OutcomeJoiner,
                 controller: Controller, meta: MetaStore, agent_ids: Callable[[], list[str]],
                 cfg: ProducerConfig, clock: Callable[[], float] = time.time, log=...): ...

    def step(self, now: float | None = None) -> ProducerTick:
        """ONE producer tick. Called by the server single-writer loop on
        poll_interval_s. NEVER raises (per-repo containment). Idempotent
        (Source dedups on (repo,sha,kind); Joiner CAS on row.version).
        O(new facts + open provisional rows). FIXED hop order is a safety property."""
        now = now if now is not None else self.clock()
        t = ProducerTick()
        if not self.cfg.watch_repos:
            self.log.warning("producer idle: watch_repos empty"); return t     # idle != broken
        since = int(self.meta.get("producer_poll_watermark") or 0)
        try:
            for facts in self.source.poll(since):        # I/O happens here, OUTSIDE the write critical section
                t += self.joiner.ingest(facts)           # HOP 2 (+ inline 3b clawback)
        except Exception as e:                            # Source contract says it shouldn't, belt-and-suspenders
            self.log.warning("producer poll failed: %s", e); t = replace(t, errors=t.errors+1)
        t += self.joiner.settle_sweep(now)               # HOP 3a — settle BEFORE drain
        t = self._drain(now, t)                          # §4.4 Fix #1: apply_outcomes_from_sink HERE, not in consolidate()
        self.meta.set("producer_poll_watermark", str(int(now)))
        self.log.info("producer tick", extra={"tick": asdict(t)})  # JSON structured (global CLAUDE §6)
        return t

    def _drain(self, now, t) -> ProducerTick:            # verbatim from service.py:942, relocated
        floor = float(self.meta.get("controller_drain_watermark") or 0.0)
        n = 0
        for aid in self.agent_ids():
            n += self.controller.apply_outcomes_from_sink(
                full_agent_identity(self.cfg.tenant_id, aid), since=floor, advance_watermark=False)
        self.meta.set("controller_drain_watermark", repr(float(self.controller.peek_max_seen_ts(floor))))
        return replace(t, drained=t.drained + n)
```

**Fixed hop order is load-bearing and asserted:** poll/ingest -> settle_sweep -> drain. settle BEFORE drain so the sink contains this tick's rewards before the credit path reads it (else credit lags a full poll_interval — the silent-staleness bug Fix #1 guards). REVERT/BUGFIX clawback runs INSIDE ingest, so a provisional `+` reverted in the same tick is superseded to `clawed_back` before settle_sweep can promote it — the netting that prevents a stale gameable-positive (§6.6 anti-gaming).

## 4. Process / placement (§12)
In-process, one OS process, one SQLite (WAL). `step()` shares the single-writer CAS/version discipline — NO second writer, NO `BEGIN IMMEDIATE`. CRITICAL sequencing constraint (the one cost of in-process): `source.poll()` + `blame_spans()` do all git I/O OUTSIDE the write lock; only the `task_outcomes`/sink writes take the short critical section. The sidecar swap (compose-staged) dissolves this by re-homing `OutcomeSource` into its own container — the Joiner and driver do not move.

## 5. Failure-mode logging points (global CLAUDE §6, structured JSON)
- Source: every `subprocess` git failure -> WARN with repo+verb+timeout, yields () (boundary failure).
- Source: porcelain parse anomaly (detached HEAD, shallow clone, malformed trailer) -> WARN, skip that fact (edge case, recoverable).
- Driver: `watch_repos` empty -> WARN once (idle, not error); `poll()` raised -> WARN + `errors+1` (liveness).
- Driver: every tick -> INFO `ProducerTick` JSON (success checkpoint + Phase-2 gate signal: stamp_hits/window_assoc/credit density).
- Joiner: CAS conflict on `transition` -> DEBUG retry; secret-bearing text NEVER reaches here (scanned pre-stage upstream). No secrets/PII in any log.

## 6. Test-first verification (TDD mandate — written BEFORE implementation)

### Pure Joiner — `tests/test_outcome_joiner.py` (NO git, NO clock, NO subprocess)
| test | asserts | catches |
|---|---|---|
| test_window_association_writes_provisional_rows | N in-window traces -> N rows, reward=0.2*margin*(1-ε), settle_at=merged_ts+settle_days | hop-2 window join / off-by-one / dropped discount |
| test_stamp_trailer_overrides_window | stamp_trace_ids=(T1,T2) -> only {T1,T2} at STAMP_WEIGHT | override inverted/ignored |
| test_require_stamp_drops_unstamped | require_stamp=True + no stamp -> 0 rows | tightening knob a no-op |
| test_provisional_settles_after_settle_days | settle_sweep(settle_at+1) -> settled_pos +0.2; settle_sweep(settle_at-1) -> nothing | gameable positive counts before reality confirms |
| test_revert_claws_back_immediately | REVERT(reverts_sha=C) -> C's rows clawed_back −1.0, no settle wait | discriminative signal lost |
| test_bugfix_overlapping_blame_claws_back | BUGFIX, touched_blame non-empty -> −1.0 on culprit rows | delayed clawback never fires |
| **test_bugfix_same_file_no_blame_overlap_does_NOT_clawback** | same file, touched_blame empty -> 0 reward, 0 state change | §6.1.6 guard (a), the expensive false-positive — **MUTATION TARGET** |
| **test_squash_merge_resolves_trace_and_blame** | squashed_from=(C,) -> C's traces + blame resolved; later revert of landed SHA finds rows | §6.1.6 guard (b), silent attribution break |
| **test_settle_then_clawback_nets_to_clawback (T-ORDER)** | provisional + reverted same tick -> final clawed_back, sink has −, no stale + | within-tick ordering hole (A's contribution) |
| test_family_scope_derivation | one tested function: bugfix-pattern->bugfix, manifest-only->dep-upgrade, else general | family hand-wave / cross-family credit leak |
| test_credit_split_by_recall_margin | multi-memory trace -> reward splits by margin, no flat +1 | co-occurrence over-credit |
| test_double_poll_idempotent | same (repo,sha,kind) twice -> rows once, reward once | double-credit via re-poll |

### Source contract — `tests/test_outcome_source_contract.py` (PARAMETRIZED over LocalGit + Fake)
test_poll_never_raises_on_bad_repo (yields (), WARN); test_poll_idempotent_identity; test_squash_emitted_with_squashed_from; test_bugfix_pre_attaches_touched_blame; test_blame_spans_are_pure_data (no live handles).

### LocalGit normalization — `tests/test_local_git_source.py` (ONLY git-touching tests, tmp-repo fixture)
planted `fix:` -> BUGFIX; `git revert` -> REVERT+reverts_sha; `Hive-Trace: T1 T2` -> stamp_trace_ids; `git blame -L` of changed lines -> correct BlameSpan.sha.

### Driver — `tests/test_producer_tick.py`
test_drain_fires_on_producer_tick_consolidation_disabled (§6.1.6 guard (c) — loop not dead); test_tick_shares_single_writer_cas; test_git_failure_one_repo_does_not_kill_tick (errors==1, no raise); test_empty_watch_repos_logs_warn_returns_zero; test_tick_aggregates_joiner_counts_into_audit_record (Phase-2 gate signal is one enforced contract).

### Mutation tests (RULE 2 — named, on the PURE side)
- **M1 (blame guard, §6.1.6's named mutation):** in `_clawback`, replace `touched_blame non-empty` with same-file `set(files_touched) & set(introduced_files)`. -> **test_bugfix_same_file_no_blame_overlap_does_NOT_clawback RED.** Restore -> green.
- **M2 (settle clock):** `settle_at<=now` -> `<0` (never settles). -> **test_provisional_settles_after_settle_days RED.** Restore -> green.
- **M3 (ordering):** emit `+0.2` at ingest time instead of after settle_at. -> **test_provisional_settles_after_settle_days (now<settle_at half) + T-ORDER RED.** Restore -> green.
- **M4 (watermark/idempotency):** driver does not advance producer_poll_watermark. -> **test_double_poll_idempotent RED.** Restore -> green.
- **M5 (squash):** Source returns empty squashed_from. -> **test_squash_merge_resolves_trace_and_blame RED.** Restore -> green.

Each names fault + catching test + green-after-restore. The §6.6 keystone ("lift traces to the ungameable clawback, not to rewarding CI-green") is protected by test_provisional_settles_after_settle_days + M2 + M3 specifically.

## 7. Why this beats each pure approach (rejected-alternatives log)
- vs A: A conflates orchestration with §11 policy — the §6.1.6 mutation target (blame overlap) and the §9 trust boundary (porcelain parsing) sit inside the same `step()`. Synthesis moves policy to a pure git-free Joiner and seals I/O behind `poll()`, so the mutation unit is small and the parser can never touch the credit math. A's sidecar path needs a `step()`->WriteBatch RPC refactor; synthesis just swaps an adapter.
- vs B: B nails the cut but leaves the driver thin-and-untyped — no asserted hop order (settle-vs-clawback netting), no typed audit record for the Phase-2 gate, ordering hand-waved as 'monotone arrival'. Synthesis imports A's fixed-order `step()`, the `ProducerTick` audit dataclass, and the tested liveness invariant, while keeping B's pre-attached-`touched_blame` resolution of the one awkward blame seam.

## 8. Grounding
GitFacts/poll port idiom: `embedder.py:37` (@runtime_checkable TextEmbedder) + `embedder.py:399` (auto_embedder). Downstream: `RecallOutcome`/`TelemetrySink.record/read/update_outcome` (`ops/telemetry.py:71/101/145/242`), `apply_outcomes_from_sink(identity, since, advance_watermark)` (`federation/controller.py:250`), the multi-agent fixed-floor drain + peek_max_seen_ts verbatim at `service.py:942`. task_outcomes DDL §3; reward schedule §4.7; producer knobs §4.8; three hops + family derivation + squash/blame guards §11; three mandatory guard tests + blame mutation §6.1.6; in-process single-writer + Fix-#1 drain §12/§4.4. subprocess-with-timeout pattern `llm.py:73` / `diagnostics/runner.py:101`. Confirmed: no existing git plumbing in the tree — C10's reader is genuinely BUILD-NEW.

**Open questions:**
- Blame round-trip resolution: pin whether the Source PRE-ATTACHES the original commit's introduced-lines as touched_blame on each BUGFIX fact (keeps Joiner pure data, but pushes a 'which original SHA?' lookback into the Source) vs. exposes blame_spans(sha, paths) as a second port method the Joiner calls (one Joiner->Source call, slightly muddies one-way fact flow). The synthesis prefers pre-attach with blame_spans as a named fallback; confirm the Source can always determine the culprit SHA at emit time without a Joiner round-trip in the squash case.
- Within-tick ordering vs. monotone-arrival: confirm the fixed hop order (associate->settle->clawback->emit->drain) fully subsumes B's 'monotone author_ts' assumption, i.e. that out-of-order facts within one poll() cannot produce a stale settled-positive that survives a same-tick revert. Add the explicit T-ORDER assertion to the synthesized Joiner, not just the driver.
- Blocking-tick budget: both designs run git blame inside the in-process tick under the single-writer discipline. Decide whether source.poll()+blame_spans() must complete OUTSIDE the write lock (B's sequencing note) with only the task_outcomes/sink writes in the critical section — and whether that sequencing is a tested invariant or just a convention. This is the one place the in-process choice (§12) imposes a real constraint the sidecar swap would dissolve.
- Phase-2 gate signal location: the ProducerTick audit record (stamp_hits/window_assoc/credit-density) is produced by the driver but the counts originate in the Joiner. Fix whether the Joiner returns structured per-ingest counts that the driver aggregates into ProducerTick, vs. the driver re-deriving them — to keep the gate signal a single enforced contract and avoid Scattered-Truth between Joiner return values and the audit record.
- require_stamp tightening + family granularity are pure config/derivation knobs on the Joiner side; confirm both A's and B's test matrices (T-REQSTAMP, test_family_scope_derivation) are merged so the synthesized Joiner has exactly one tested family-derivation function (git-remote x dominant-ext x workflow) and one tested stamp-override path.

---

## D7. Admission / native-chat approval surface (hive_pending / hive_approve / hive_reject + the hive_write scan→stage change): how to structure the pending→approved state machine, the secret-scan gate, and the approved-only recall boundary, scored against the design rubric (interface depth, cognitive load, information hiding, special-vs-general, future bolt-on, agent-navigability, enforced-not-prose contracts) plus the swappability / never-hallucinate / TDD-first mandates.

- **Option A:** Approach A — Status-Column Trio. A single deep `AdmissionService` module (3 calls + the `write()` change) mutating an `episodes.status` column directly. Two-state machine (pending/approved; rejected = deletion, with a `keep_rejected` flag). Reuses the `update_cas` `WHERE eid=? AND version=?` idiom as `WHERE eid=? AND status='pending'` for idempotent, race-safe approval. Two defense-in-depth recall enforcement points: (1) index-absence (only approve() calls index.add), (2) a belt-and-suspenders `ep.status=='approved'` re-check during candidate hydration. Two internal seams: a `SecretScanner` port and an `ApprovalPolicy` port. 11 named tests + 6 mutation faults. Honestly names: thin audit trail (status is a field not an event log), recall guarantee is discipline-not-type, rejected-as-deletion loses negative signal, no batch transactionality.
- **Option B:** Approach B — Admission Ledger. Three-module decomposition (scanner.py / ledger.py / models.py + episode_store + write_path + admission_tools) where the dangerous knowledge is partitioned three ways: 'what is a secret' (scanner), 'what is recallable' (a single named `_RECALL_PREDICATE = "status='approved'"` constant inside episode_store), 'how status changes' (ledger). Three-state terminal+idempotent machine (pending → {approved, rejected}, both terminal) via CAS `compare_and_set_status(... WHERE id=? AND status='pending')`. Frozen dataclasses with `__post_init__` assertions (ScanVerdict/AdmissionResult contracts that cannot be constructed malformed). The keystone: the approved-only predicate lives at the candidate-set SELECT that *births* the ranker's input (replacing the reference `WHERE tombstoned=0`), so no downstream stage can resurrect a pending row — enforcement is upstream of all scoring/gating/fusion. EpisodeStore/VectorIndex/SecretScanner all ports. ~25 named tests across 4 files + 5 mutation faults including a startup index-reconcile test. Names: agent-relayed approval (accepted §9 seam), two derived indexes to keep consistent (crash-window reconcile), REDACT semantic degradation, approved-is-terminal cliff.
- **Winner:** synthesis — The contrast exposed a decisive fact that neither approach alone fully exploits, and that I verified against the live reference tree. Approach A's own weakness #2 ("the recall guarantee is a discipline, not a type — a future contributor could add a second index.add() or forget a status filter") is not hypothetical: `grep tombstoned=0` in persistence.py returns the predicate duplicated across FOUR live query sites (iter_for_agent:518, scan_tenant:654, lookup_live_by_subject:543, count_live:626). The MVP swap to `status='approved'` would replicate that exact predicate-scatter — which is precisely the leakage Approach B eliminates by naming ONE `_RECALL_PREDICATE` constant as the single source of truth for 'what is recallable'. That is the highest-information delta between the two designs and it favors B on the two rubric axes that matter most for a security boundary: information hiding (B partitions the three dangerous facts into three modules with zero leakage; A lets 'approved' live as a literal scattered across recall sites) and contracts-that-cannot-lie (B's frozen `ScanVerdict.__post_init__` makes a PASS-with-matches or REDACT-without-text verdict UNCONSTRUCTABLE — a type-level guarantee, vs A's prose+test discipline). However, B as written has two genuine costs A solves more cleanly: (1) B models rejected as a retained terminal state requiring a 3-state machine and a `compare_and_set_status` that re-reads on race, where A's reuse of the EXACT proven `update_cas` rowcount idiom (verified at persistence.py:581 — real `UPDATE...WHERE...returning cur.rowcount`) is the lower-risk, already-tested concurrency primitive; (2) B's two-derived-indexes consistency problem (crash between status-commit and index.add) is real and forces a startup-reconcile, whereas A's framing of index-absence as the PRIMARY teeth is the cleaner mental model. The synthesis takes B's structural spine (single `_RECALL_PREDICATE` constant; three-module knowledge partition; frozen self-asserting value types; enforcement-at-candidate-birth; the SecretScanner/EpisodeStore/VectorIndex/ApprovalPolicy ports satisfying the swappability mandate without making the recall boundary itself swappable) and grafts on A's lower-risk mechanics (the literal `update_cas` rowcount CAS idiom rather than a bespoke compare-and-set; index-absence named as the primary guarantee with the query-predicate as belt-and-suspenders; rejected-as-deletion default with a `keep_rejected` flag for audit, avoiding B's 3-state terminal cliff while keeping the option). It keeps the union of both test contracts — A's per-invariant decomposition and B's keystone `test_pending_never_in_candidates` / `test_recall_returns_nothing_when_only_pending` plus B's startup-reconcile test — and the union of mutation faults (6 from A + B's 'drop the predicate' and 'index a pending row in stage()' faults, which are the two that directly prove the single-constant enforcement is load-bearing). On never-hallucinate: both satisfy abstain-no-resurrect because pending rows are absent from candidates; the synthesis enforces it at the SELECT (B) AND at index-absence (A) — two independent fail-closed defenses, each broken by an independent mutation test. On TDD-first: every module ships failing tests before implementation with file::test + exact assertion + the failure caught, and every gate/state-machine/recall path has a named mutation fault. The synthesis is not a split-the-difference compromise — it is B's information-hiding architecture made lower-risk by A's reuse of the one proven concurrency primitive already in the tree.
- **Rejected because:** Rejected pure Approach A because its central structural weakness is real and verified in the reference tree: 'only approve() indexes' and 'recall filters status=approved' are enforced by code review + a test, not by the type system or a single source of truth. The `tombstoned=0` predicate is ALREADY scattered across four query sites in persistence.py; A's `status='approved'` swap inherits that scatter, so a future fusion/BM25 stage (explicitly coming per spec §8.3) is one forgotten filter away from leaking a pending row — the exact bug class A admits it cannot make unrepresentable. A's 2-state 'rejected = deletion' default also silently discards the negative write-quality signal a later loop wants, and its audit trail is a single mutable field with no transition history. Rejected pure Approach B because, while its architecture is stronger, two of its specific mechanics are higher-risk than necessary: (1) its bespoke `compare_and_set_status` re-implements the optimistic-CAS pattern that ALREADY exists, proven and tested, as `update_cas` (persistence.py:581) — reinventing a concurrency primitive is gratuitous risk on the one path where a race is catastrophic; reuse the existing rowcount idiom. (2) B's three-state machine with BOTH approved and rejected as retained terminal states creates the 'approved-is-terminal cliff' B itself names as a future-scope hazard, and forces the two-derived-indexes consistency burden (crash-window reconcile) to be load-bearing rather than a hygiene nicety. The synthesis keeps B's single-`_RECALL_PREDICATE` constant and three-module knowledge partition (the actual wins) while dropping B's bespoke CAS (use update_cas's idiom) and softening rejected to deletion-by-default-with-flag (avoiding the 3-state cliff). Also rejected from BOTH: making the recall `status='approved'` boundary itself swappable/configurable — both correctly fuse it as a hard literal; the synthesis preserves that non-swappability as a feature (an allowlist, so any future status is non-recallable by default — the fail-safe direction).

### Chosen design
# Admission / Native-Chat Approval Surface — Chosen Design (Synthesis of A's mechanics + B's architecture)

## 0. The one hard guarantee (the boundary being designed)

**A `pending` row is NEVER recallable; the only `pending→approved` edge is a human "yes" relayed via native chat; a raw secret never reaches storage.** This is spec §1 [SEC], §8.2, §9 substrate-layer "hard" guarantees, and the §6.1 #5 acceptance test. Everything else (the loop C9, the producer C10, hooks) is downstream of and trusts this boundary.

The synthesis decision: take **Approach B's information-hiding architecture** (three dangerous facts partitioned into three modules; a SINGLE `_RECALL_PREDICATE` constant as the one source of truth for "what is recallable"; frozen self-asserting value types) and graft on **Approach A's lower-risk mechanics** (reuse the proven `update_cas` rowcount CAS idiom verbatim rather than a bespoke compare-and-set; name index-absence as the PRIMARY guarantee; rejected-as-deletion-by-default with a flag, avoiding B's 3-terminal-state cliff).

**Why the synthesis, in one line:** the reference tree shows `tombstoned=0` ALREADY scattered across four query sites (`persistence.py` iter_for_agent:518, scan_tenant:654, lookup_live_by_subject:543, count_live:626). A's `status='approved'` swap would inherit that scatter — exactly the leakage B's single constant eliminates — while B's bespoke CAS would gratuitously reinvent the proven `update_cas` (persistence.py:581). Take B's spine, A's primitive.

---

## 1. Module map (B's three-way knowledge partition)

```
hive/
  admission/
    models.py          # frozen value types w/ __post_init__ contracts that cannot lie
    scanner.py         # SecretScanner port + DeterministicScanner — "what is a SECRET"
    ledger.py          # AdmissionLedger — the ONLY status mutator — "how status CHANGES"
  storage/
    episode_store.py   # owns _RECALL_PREDICATE — the ONE place "what is RECALLABLE" lives
  serving/
    write_path.py      # hive_write handler: scan -> stage -> render-block
    admission_tools.py # hive_pending/hive_approve/hive_reject (thin; delegate to ledger)
```

Three dangerous facts, three modules, zero leakage:
- **what is a secret** → `scanner.py` (only the scanner knows credential shapes)
- **what is recallable** → `episode_store._RECALL_PREDICATE` (one named constant, B's keystone)
- **how status changes** → `ledger.py` (only the ledger issues an `UPDATE status`)

---

## 2. Value types (`admission/models.py`) — contracts that cannot lie

```python
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
from typing import Optional

class AdmissionState(str, Enum):
    PENDING  = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"           # only materialized when admission.keep_rejected=True

_TERMINAL: frozenset[AdmissionState] = frozenset({AdmissionState.APPROVED,
                                                  AdmissionState.REJECTED})

class ScanAction(str, Enum):
    PASS   = "pass"                 # no secret -> stage verbatim
    REDACT = "redact"               # secret found, masked text staged
    REFUSE = "refuse"               # secret found, unredactable -> NEVER staged

@dataclass(frozen=True)
class ScanVerdict:
    action: ScanAction
    redacted_text: Optional[str]            # set iff REDACT
    matched_rules: tuple[str, ...]          # e.g. ("aws_akia","high_entropy_b64")
    def __post_init__(self) -> None:
        # B's keystone: a malformed verdict is UNCONSTRUCTABLE (type-level, not prose)
        if self.action is ScanAction.PASS and self.matched_rules:
            raise ValueError("PASS verdict cannot carry matched rules")
        if self.action is ScanAction.REDACT and self.redacted_text is None:
            raise ValueError("REDACT verdict must carry redacted_text")

@dataclass(frozen=True)
class WriteResult:                          # hive_write return
    id: Optional[int]                       # pending id; None iff REFUSED
    status: AdmissionState                  # PENDING, or sentinel for refused (see note)
    refused: bool
    scan: ScanVerdict
    redacted: bool
    render_block: str                       # verbatim native-chat string the agent surfaces

@dataclass(frozen=True)
class PendingRow:
    id: int
    text_preview: str                       # first ~160 chars; full via hive_fetch
    proposed_by: str
    ts: int
    scan_action: ScanAction

@dataclass(frozen=True)
class AdmissionResult:                       # one per id in approve()/reject()
    id: int
    prior: Optional[AdmissionState]          # None == unknown id
    now: Optional[AdmissionState]
    changed: bool                            # True iff prior==PENDING and now in _TERMINAL
    indexed: bool                            # True iff this call inserted the vector
```

---

## 3. Secret scanner (`admission/scanner.py`) — port + v-min impl

```python
from typing import Protocol

class SecretScanner(Protocol):
    def scan(self, text: str) -> ScanVerdict: ...   # pure, deterministic, no I/O

class DeterministicScanner:
    """§9 'one always-on floor'. Regex set + Shannon-entropy backstop.
    Returns a verdict; NEVER mutates store state (no info leak across the boundary)."""
    # rules: sk-, AKIA[0-9A-Z]{16}, ghp_, xox[bap]-, eyJ...JWT, -----BEGIN...PEM-----,
    #        postgres://user:pass@, mysql://..., + base64 high-entropy backstop
    def scan(self, text: str) -> ScanVerdict: ...
```

SWAPPABILITY: `write_path` depends on the `SecretScanner` Protocol, never the concrete. An ML/vendor-DLP scanner later is constructor injection, zero change to the state machine.

---

## 4. The ONE recall predicate (`storage/episode_store.py`) — B's keystone, the load-bearing win

```python
# storage/episode_store.py — the ONE place "recallable" is defined.
# This replaces the reference's scattered `WHERE tombstoned=0` (verified at
# persistence.py:518/543/626/654 — FOUR sites). A single named constant kills the scatter.
_RECALL_PREDICATE = "status = 'approved'"   # single source of truth

EPISODE_COLS = [...]  # port from row_codec.EPISODE_COLS, drop bi-temporal/supersession cols

def recall_candidates(self, tenant_id: str) -> Iterator[Episode]:
    """The candidate set the ranker scores. By construction contains ONLY approved
    rows — a pending row is not 'filtered out', it is never SELECTed. There is no
    parameter to disable the predicate. O(N_approved) exhaustive scan; the exact
    kNN index is the authoritative fast path and is likewise approved-only
    (rows enter it only via AdmissionLedger.approve). The exact index is
    AUTHORITATIVE and must never silently flip to ANN (project geometry lock)."""
    cols = ",".join(EPISODE_COLS)
    sql = f"SELECT {cols} FROM episodes WHERE tenant_id=? AND {_RECALL_PREDICATE}"
    for row in self._conn.execute(sql, (tenant_id,)):
        yield row_to_episode(row)

# the CAS primitive — REUSED from update_cas (persistence.py:581), A's lower-risk choice.
def compare_and_set_status(self, eid: int, *, expect: str, new: str,
                           approved_by: Optional[str], approved_ts: Optional[int]) -> bool:
    """A's update_cas idiom verbatim: UPDATE ... WHERE eid=? AND status=? ; the
    rowcount IS the truth. Returns rowcount>0. This is the SAME proven optimistic-
    concurrency pattern already tested in the tree — not a reinvented primitive."""
    cur = self._conn.execute(
        "UPDATE episodes SET status=?, approved_by=?, approved_ts=? "
        "WHERE eid=? AND status=?", (new, approved_by, approved_ts, eid, expect))
    return cur.rowcount > 0

def insert_pending(self, *, text, value, weight, source, proposed_by,
                   content_hash, tags="") -> int: ...   # status defaults 'pending' (DDL)
def status_of(self, eid: int) -> Optional[str]: ...
def value_of(self, eid: int) -> bytes: ...
def list_by_status(self, status: str, since_ts: Optional[int], limit: int=100) -> list[PendingRow]: ...
def delete(self, eid: int) -> bool: ...                 # rejected-as-deletion (A's default)
```

### DDL (spec §3 columns; B's predicate, A's safe-default)

```sql
ALTER TABLE episodes ADD COLUMN status      TEXT    NOT NULL DEFAULT 'pending';  -- safe default
ALTER TABLE episodes ADD COLUMN proposed_by TEXT    NOT NULL DEFAULT 'unknown';
ALTER TABLE episodes ADD COLUMN approved_by TEXT;        -- NULL until approved
ALTER TABLE episodes ADD COLUMN approved_ts INTEGER;     -- NULL until approved

-- recall hot path filters status='approved' every query -> partial index, O(log n) seek
CREATE INDEX IF NOT EXISTS ix_episodes_approved ON episodes(status) WHERE status='approved';
CREATE INDEX IF NOT EXISTS ix_episodes_pending_ts ON episodes(ts)   WHERE status='pending';

-- CHECK: status IN ('pending','approved','rejected'); approved => approved_by/ts NOT NULL
-- (table-level CHECK at CREATE; trigger on ALTER path) — un-lie-able state set at storage.
```

`DEFAULT 'pending'` is a safety property: an INSERT that forgets status lands non-recallable, not silently live (allowlist, not denylist — a future `quarantined` state is non-recallable by default, the fail-safe direction). Non-swappability of `_RECALL_PREDICATE` is a FEATURE — it is not behind a port; making the recall boundary configurable would make the one hard guarantee soft.

---

## 5. The state machine (`admission/ledger.py`) — the only status mutator

```python
import time
class AdmissionLedger:
    """Single authority over episodes.status. No other module UPDATEs status.
    Transitions terminal + idempotent + replay-safe. Legal edges (the ENTIRE table):
        pending --approve--> approved   (stamps approved_by/ts; indexes the vector)
        pending --reject --> {deleted | rejected}  (A's default: delete; flag: terminal)
    Any edge out of a terminal/absent state is a NO-OP changed=False — never an error,
    never a re-transition."""

    def __init__(self, store, index, *, keep_rejected: bool = False, clock=time.time):
        self._store = store          # EpisodeStore port
        self._index = index          # VectorIndex port — pending rows are NOT in it
        self._keep_rejected = keep_rejected
        self._clock = clock

    def stage(self, *, text, value, weight, source, proposed_by,
              content_hash, scan: ScanVerdict, tags="") -> int:
        # REFUSE must never reach stage — asserted; a secret would otherwise persist.
        assert scan.action is not ScanAction.REFUSE, "REFUSE must be rejected before stage()"
        eid = self._store.insert_pending(text=text, value=value, weight=weight,
                source=source, proposed_by=proposed_by, content_hash=content_hash, tags=tags)
        log.info("admission.stage id=%d action=%s proposed_by=%s",
                 eid, scan.action.value, proposed_by)   # log identifiers, never the secret
        return eid                                       # NOT indexed

    def approve(self, ids: list[int], approver: str) -> list[AdmissionResult]:
        if not approver:                                 # fail-closed human-relay requirement
            log.warning("admission.approve denied: empty approver ids=%s", ids)
            raise ApprovalDenied("approver must be non-empty (human-relay)")
        ts = int(self._clock()); out = []
        for eid in ids:
            prior = self._store.status_of(eid)
            if prior is None:
                out.append(AdmissionResult(eid, None, None, False, False)); continue
            if prior in {s.value for s in _TERMINAL}:    # idempotent replay (already approved/rejected)
                out.append(AdmissionResult(eid, AdmissionState(prior),
                                           AdmissionState(prior), False, False)); continue
            ok = self._store.compare_and_set_status(eid, expect="pending", new="approved",
                                                    approved_by=approver, approved_ts=ts)
            if not ok:                                   # lost the race — re-read truth, no-op
                cur = self._store.status_of(eid)
                out.append(AdmissionResult(eid, AdmissionState.PENDING,
                           AdmissionState(cur) if cur else None, False, False)); continue
            self._index.add(eid, self._store.value_of(eid))     # NOW recallable (primary guarantee)
            log.info("admission.approve id=%d approver=%s indexed", eid, approver)
            out.append(AdmissionResult(eid, AdmissionState.PENDING,
                                       AdmissionState.APPROVED, True, True))
        return out

    def reject(self, ids: list[int]) -> list[AdmissionResult]:
        out = []
        for eid in ids:
            prior = self._store.status_of(eid)
            if prior is None or prior in {s.value for s in _TERMINAL}:
                out.append(AdmissionResult(eid, AdmissionState(prior) if prior else None,
                           AdmissionState(prior) if prior else None, False, False)); continue
            if self._keep_rejected:                      # flag: retain terminal rejected (audit)
                self._store.compare_and_set_status(eid, expect="pending", new="rejected",
                                                   approved_by=None, approved_ts=None)
                now = AdmissionState.REJECTED
            else:                                        # A's default: delete row + blob (hygiene)
                self._store.delete(eid); now = AdmissionState.REJECTED
            log.info("admission.reject id=%d keep_rejected=%s", eid, self._keep_rejected)
            out.append(AdmissionResult(eid, AdmissionState.PENDING, now, True, False))
        return out   # reject NEVER touches the index (pending was never in it)

    def pending(self, since: Optional[int] = None, limit: int = 100) -> list[PendingRow]:
        return self._store.list_by_status("pending", since, limit)
```

**Why CAS, not blind UPDATE:** two agents relaying the same human "yes" cannot both win — `compare_and_set_status(... WHERE eid=? AND status='pending')` makes the second see `changed=False`. Idempotency is enforced at storage, not just Python (spec §8.2: "the *only* pending→approved path"). This is A's verified `update_cas` rowcount idiom, not a bespoke primitive.

**Two recall defenses, both fail-closed (A's primary + B's keystone):**
1. **Index-absence (PRIMARY, A):** only `approve()` calls `index.add`. A pending row has no vector; the exhaustive cosine scan AND the exact kNN literally have nothing to score.
2. **Query-predicate (KEYSTONE, B):** `recall_candidates` SELECTs `status='approved'` at the candidate set's BIRTH, upstream of all scoring/gating/fusion — so the never-hallucinate gate, entropy abstention, and any future §8.3 BM25/RRF stage inherit approved-only for free, with no per-stage filter to forget.

Both must hold; the test suite breaks each independently.

---

## 6. Write path (`serving/write_path.py`) — scan → stage → render-block

```python
def handle_hive_write(self, *, text, weight=1.0, proposed_by="unknown",
                      source="hook") -> WriteResult:
    verdict = self._scanner.scan(text)                  # HARD GATE, before embed/store
    if verdict.action is ScanAction.REFUSE:
        log.warning("hive_write refused: rules=%s proposed_by=%s",
                    verdict.matched_rules, proposed_by)   # identifiers only, never the secret
        return WriteResult(id=None, status=AdmissionState.PENDING, refused=True,
                           scan=verdict, redacted=False,
                           render_block="(refused: credential detected — nothing staged)")
    staged_text = verdict.redacted_text if verdict.action is ScanAction.REDACT else text
    value = np_to_bytes(self._embedder.embed(staged_text))
    chash = hashlib.sha256(staged_text.encode("utf-8")).digest()
    eid = self._ledger.stage(text=staged_text, value=value, weight=weight,
            source=source, proposed_by=proposed_by, content_hash=chash, scan=verdict)
    block = render_pending_block(eid, staged_text, verdict, proposed_by)
    return WriteResult(id=eid, status=AdmissionState.PENDING, refused=False, scan=verdict,
                       redacted=(verdict.action is ScanAction.REDACT), render_block=block)
```

The **render-block** is the §8.2 "honest seam": the server hands the agent a ready-to-surface native-chat string; the agent relays it. The agent has NO status-mutating power — only `hive_approve` flips status, and that is the human's "yes" by construction of the surface.

---

## 7. MCP trio (`serving/admission_tools.py`) — thin handlers, extend TOOL_DEFINITIONS

```python
def hive_pending(self, since=None) -> dict:
    return {"pending": [r.__dict__ for r in self._ledger.pending(since)]}
def hive_approve(self, ids, approver) -> dict:
    return {"results": [r.__dict__ for r in self._ledger.approve(ids, approver)]}  # changed flags expose idempotency
def hive_reject(self, ids) -> dict:
    return {"results": [r.__dict__ for r in self._ledger.reject(ids)]}
```

Schema additions extend the ported `mcp_tools.py:TOOL_DEFINITIONS` (static-constant style); `hive_write`'s description changes to "Stages the text for approval (secret-scanned first). Returns the pending id; call hive_pending/hive_approve to admit it." — the contract now tells the truth about staging. Per §10 row 736: drop consolidate/schemas/recall_cold/restore_cold.

---

## 8. Swap seams (SWAPPABILITY MANDATE — all four ports)

- **`SecretScanner`** — `scan(text)->ScanVerdict`, pure. Only thing that knows what a secret looks like. Swap deterministic → ML, write_path unchanged.
- **`EpisodeStore`** — ledger + write_path depend on the protocol (`insert_pending`, `compare_and_set_status`, `status_of`, `value_of`, `recall_candidates`, `list_by_status`, `delete`). `_RECALL_PREDICATE` is part of the *contract of recall_candidates* ("yields approved-only"), so a Postgres/DuckDB swap preserves the guarantee.
- **`VectorIndex`** — `approve()` calls `index.add`; `stage()`/`reject()` never do. The exact-kNN-approved-only property survives an index swap because it is enforced by *who calls add*, not the index. Exact-authoritative by contract (the "never flip to ANN" lock holds).
- **`ApprovalPolicy`** (reserved) — today the MVP `TrustingRelayPolicy` (approver non-empty). The §9 fleet hardening swaps in `SignedApprovalPolicy` (verifies an out-of-band human signature) — only `hive_approve`'s parameter changes; the state machine, DDL, trio, and every test are unchanged. The §9 honest-seam weakness is isolated to one injected object.
- **Outcome producer (C10)** — completely decoupled. Admission is UPSTREAM of the loop (§8.2: the utility loop only credits already-admitted memories); the producer never reads `status`. Correct minimal coupling: no seam crosses here.

The recall `status='approved'` boundary is deliberately NOT a port — making it swappable would make the one hard guarantee soft.

---

## 9. Failure-mode logging points (CLAUDE.md §6)

| Point | Level | Fields (never the secret) |
|---|---|---|
| `hive_write` REFUSE | warn | `matched_rules`, `proposed_by` — boundary failure (credential blocked) |
| `stage` success | info | `id`, `scan.action`, `proposed_by` — checkpoint (staged) |
| `approve` empty approver | warn | `ids` — edge case (relay requirement violated), raises `ApprovalDenied` |
| `approve` CAS lost race | debug | `eid`, re-read status — recoverable concurrency edge |
| `approve` success+index | info | `id`, `approver` — milestone (now recallable) |
| `reject` | info | `id`, `keep_rejected` — checkpoint (terminal drop) |
| startup index-reconcile re-add | warn | `eid` count — recovered from crash-window (approved-but-unindexed) |

All logs carry identifiers/hashes only — never raw secrets or recalled text (CLAUDE.md §6 Performance & Context).

---

## 10. Test-first verification (the Test Contract — failing tests BEFORE implementation)

Format: `file::test` — exact assertion — failure it catches. Fakes: real SQLite in tmpdir (the CAS/atomicity SQL IS the contract — faking the store tests nothing); `FakeEmbedder` (hash→unit vec), `FakeScanner` (scripted verdicts by substring), `CountingIndex` (records every `add()` — ground truth for index-absence).

### 10.1 `tests/admission/test_state_machine.py`
```
::test_write_stages_pending          — write(text).id status=='pending' AND id NOT in counting_index.added
                                       CATCHES: write that auto-approves/indexes (the write_text immediate-index bug, service.py:1422)
::test_approve_makes_recallable      — approve([id],"derek"); recall returns id; id in counting_index.added
                                       CATCHES: approve that flips status but forgets to index (silent data loss)
::test_approve_is_idempotent         — approve x2; r2.changed==False; approved_ts UNCHANGED; counting_index.added.count(id)==1
                                       CATCHES: re-stamp / double-index on replay
::test_approve_requires_nonempty_approver — approve([id],"") raises ApprovalDenied; status still 'pending'
                                       CATCHES: unattributed approval slipping the human-relay requirement
::test_reject_is_terminal            — reject([id]); approve([id],"derek").changed==False; recall does NOT return id
                                       CATCHES: rejected row resurrectable by later approve (abstain-no-resurrect sibling)
::test_default_status_is_pending     — raw INSERT without status; row.status=='pending'
                                       CATCHES: insert/migration path that lands a row LIVE
::test_concurrent_approve_indexes_once — two threads approve([id]); exactly one .changed; counting_index.added.count(id)==1
                                       CATCHES: lost-update race / double-index (CAS WHERE status='pending' not holding)
::test_stage_refuse_asserts          — stage(scan=REFUSE) raises AssertionError; store row-count unchanged
                                       CATCHES: a REFUSE secret reaching stage() and persisting
```
### 10.2 `tests/admission/test_recall_enforcement.py` (B's keystone)
```
::test_pending_never_in_candidates   — stage 3, approve 1; list(recall_candidates(TENANT)) len==1, only approved id
                                       CATCHES: the candidate SELECT leaking pending rows (the _RECALL_PREDICATE win)
::test_pending_row_is_not_recalled   — write("secret sauce is X"); recall("secret sauce") is EMPTY   [§6.1 #5(b)]
                                       CATCHES: recall scoring pending rows — the core security guarantee
::test_recall_returns_nothing_when_only_pending — full service.recall on only-pending store -> EMPTY, never a weak guess
                                       CATCHES: never-hallucinate + approved-only failing together
::test_recall_index_never_contains_pending — write 5, approve 2; set(counting_index.added)==the 2 approved ids
                                       CATCHES: indexing on stage instead of approve
::test_startup_reconcile_readds_unindexed_approved — approved row with empty index; startup reconcile re-adds it
                                       CATCHES: crash-window between status-commit and index.add (recall-invisible, never a LEAK)
::test_approved_is_recallable_end_to_end — stage->approve->recall of own text returns it above gate
                                       CATCHES: over-tight enforcement dropping APPROVED rows (false-negative)
```
### 10.3 `tests/admission/test_secret_scanner.py`
```
::test_planted_credential_refused_before_stage — write("AKIA"+...).refused; id is None; NO row contains "AKIA"  [§6.1 #5(a)]
::test_refuses_pem_jwt_xoxb_sk      — each family -> REFUSE
::test_redact_scrubs_then_stages    — write("ghp_..."): redacted; "ghp_" NOT in stored text; content_hash is of masked text
::test_high_entropy_b64_refused     — 40-char high-Shannon token -> REFUSE (novel-format backstop)
::test_clean_text_passes_verbatim   — scan("Redis policy note").action==PASS, matched_rules==()   [no false-positive emptying the store]
::test_verdict_invariants           — ScanVerdict(PASS, matched_rules=("x",)) raises; ScanVerdict(REDACT, redacted_text=None) raises
::test_secret_never_in_logs         — scan a key under caplog; raw key substring absent from every record
```

### 10.4 Mutation tests (RULE 2) — name fault → named test goes RED → restore → green
| Fault introduced | Test that MUST go red |
|---|---|
| `recall_candidates` drops `AND status='approved'` (→ `WHERE 1=1`) | `test_pending_never_in_candidates`, `test_recall_returns_nothing_when_only_pending` — **proves the single `_RECALL_PREDICATE` is load-bearing, not incidentally true** |
| `approve` CAS predicate `status='pending'` → unconditional `WHERE eid=?` | `test_concurrent_approve_indexes_once`, `test_approve_is_idempotent`, `test_reject_is_terminal` |
| `approve` indexes on the `already_approved`/terminal branch too | `test_approve_is_idempotent` (count(id)==1 fails) |
| `stage()` adds `self._index.add(eid,value)` | `test_recall_index_never_contains_pending`, `test_pending_never_in_candidates` — **proves index-absence is owned by approve, not stage** |
| delete `if prior in _TERMINAL` branch in approve | `test_reject_is_terminal` (rejected row gets approved+indexed — the worst bug) |
| secret scan moved AFTER insert | `test_planted_credential_refused_before_stage` (row exists) |
| `status` default `'pending'`→`'approved'` | `test_default_status_is_pending`, `test_pending_row_is_not_recalled` |
| remove empty-approver check | `test_approve_requires_nonempty_approver` |
| remove `AKIA` rule from scanner | `test_planted_credential_refused_before_stage`, `test_refuses_pem_jwt_xoxb_sk` |

A gate whose test still passes when broken is not tested — every row names the firing assertion.

---

## 11. Future bolt-ons (clean by construction)
- **Fleet-tier signed approval** — swap `TrustingRelayPolicy`→`SignedApprovalPolicy` (ApprovalPolicy port); state machine/DDL/trio/tests untouched.
- **§8.3 hybrid recall (BM25/RRF)** — `approve()` gains one `self._fts.add(eid, text)` call exactly where it already calls `index.add`; the FTS5 index inherits approved-only for free (populated only by approve). Fusion scores an already-approved pool; entropy gate stays after fusion. No new enforcement.
- **§8.2 reversible-ledger digest + one-click veto** — `approved_by`/`approved_ts` are the audit columns the digest reads; a new *read* over the same rows, no schema change; grows `cortex-report.timer`. Rollback lives in the versioned utility layer (`utility.version` bump), so veto never mutates `status` — append-only episodes preserved.
- **Batch approval** — `approve(ids[])` already lists; "approve all since yesterday" = `approve([r.id for r in pending(since)], approver)`.
- **`quarantined`/triage states** — allowlist recall means a new status is non-recallable by default; needs an explicit edge to approved, never accidentally live.
- **C9 loop** — trusts this boundary for free: it only credits `approved` episodes because recall only surfaces approved episodes. Admission is strictly upstream; no coupling to add.

---

## 12. Honest weaknesses (carried from both, named not hidden)
1. **Approval relay is agent-mediated** — the server cannot verify the human truly said yes (§9 accepted seam). The synthesis LOCALIZES this to one argument of one tool (`approver`) behind the `ApprovalPolicy` port; v-min leaves it unsolved by design, fleet tier hardens by addition.
2. **Two derived indexes vs one transaction** — the vector index (and later FTS5) live outside the SQLite status-commit. A crash between CAS-commit and `index.add` leaves an approved-but-unindexed row (recall-invisible, NEVER a leak — fails safe). Mitigated by the startup `index-reconcile` (idempotent, exact, O(N_approved)) with its named test. A real operational seam, not a correctness hole. **OPEN QUESTION:** can status-flip + index.add be wrapped in one SQLite transaction to eliminate the window entirely?
3. **REDACT can degrade meaning** — a masked connection string may leave an ambiguous insight. Scanner errs toward REFUSE on doubt; the render-block surfaces the masked text so the approving human can reject a now-meaningless one. Server can't judge semantic degradation. Accepted.
4. **Approved-is-terminal cliff** — the small state space optimizes hard for "admit once, never un-admit." A future "approved-then-re-review" need is a NEW terminal-state exit (a state-machine change), not an additive bolt-on. The clean bolt-ons in §11 are all additive-policy-before-the-CAS; a new exit edge is not. Honest trade for v-min simplicity.
5. **Idempotency is per-call, not content-deduped** — two different pending rows with identical text can both be approved; dedup is the write-path's job (§3 `content_hash`), not admission's. Named so it is not surprising. **OPEN QUESTION:** confirm the write path dedups pre-stage.

None of these touches the one hard guarantee — a pending row is never recallable — enforced TWICE (index-absence + the single `_RECALL_PREDICATE`), defaulting to the safe state, broken by nine independent mutation tests.

**Open questions:**
- keep_rejected default: ship rejected-as-deletion (store hygiene, A's default) or rejected-as-retained-terminal (B's default, preserves negative write-quality signal for a later loop)? Recommend deletion-default + flag for MVP; revisit when the write-quality loop is scoped.
- Index-reconcile-on-startup: A treats index-absence as primary and does not require reconcile; B requires it because approve() does index.add outside the status-commit transaction. Decision: can the status-flip and index.add be wrapped in one SQLite transaction (index is in-process, exact, SQLite-backed) to make reconcile unnecessary, or is the vector index out-of-transaction by construction (forcing B's reconcile + its test)? This determines whether the crash-window is a correctness seam or eliminated.
- Content-hash dedup at admission: both note dedup is the write-path's job (§3 content_hash), not admission's, so two identical-text pending rows can both be approved. Confirm whether the write path dedups pre-stage (spec §3 lists content_hash for 'fetch + dedup') so the admission trio never needs to.
- approver attribution under §9 honest seam: confirm the MVP TrustingRelayPolicy (approver must be non-empty) is the agreed floor and that SignedApprovalPolicy is strictly the reserved fleet-tier port — i.e. no signature verification is built now.

---

## D9. Hybrid lexical recall channel (2026-06-11 — `docs/PLANS/HYBRID-RECALL-PLAN.md` v2, landed gated-off)

Adds the lexical (BM25/FTS5) channel fused with the dense channel by RRF **inside the CONFIDENT
branch only**, OFF by default (`recall.hybrid`, reload tier C), plus `hive/research/channel_eval.py` —
the labels-only instrument whose paired-bootstrap `lo > 0` on per-query recall@k deltas alone can
justify flipping it. Never-hallucinate is untouched: the entropy gate evaluates the dense cosine
distribution before any lexical I/O (mutation-pinned). Four design-twice calls:

**D-V1 — store-owned, in-transaction FTS vs a separate `Fts5LexicalIndex` adapter.**
*Rejected:* a Python-side adapter synced best-effort from the store, like the dense warm cache — two
modules know `episodes_fts` (the adapter reads, the store triggers), a second handle threads through
container/store, sync is post-commit best-effort though the table is durable, and a `content=''`
contentless FTS5 table cannot per-row DELETE before SQLite 3.43 (supersede/demote/sweep need deletes).
*Chosen:* the store owns every byte of FTS SQL in the same transactions that move trust states
(`complete`/`set_trust`/`supersede`/`sweep_decayed` + `rebuild_fts` boot self-heal — atomic, cannot
diverge), exposing one read-only port method (`LexicalIndex.search_text`; `lexical_index=store`, the
exact `reader=store`/`ledger=store` idiom). *Trade accepted:* the `EpisodeStore` god-port grows by two
methods (resolution B5 already accepts this shape; the method group is cohesive).

**D-V2 — cross-encoder rerank deferred wholesale** (the prior draft shipped its production scaffolding
off-by-default). Sequential evidence: rerank gains stack on a hybrid baseline, so its eval is only
meaningful after hybrid is measured; a port + two adapters + a `hive[rerank]` extra + three config
fields ahead of any evidence is scaffolding-before-measurement (~40% of the surface, cut). *Add-back:*
a `Reranker` port + `NoopReranker`/`CrossEncoderReranker` behind `hive[rerank]`, capped at a
fused-shortlist `rerank_top_k`, when `channel_eval` (extended with a rerank arm) justifies it.

**D-V3 (D-H7) — exposure margins under fusion reordering.** *Rejected:* RRF-score-based margins
(uncalibrated, breaks the D1 "same masses the gate computed" invariant); skipping exposure for
lexical-resurfaced hits (a serve must refresh liveness — the exposure-resurrection rule). *Chosen:*
per-hit margins over the resolved set's **dense masses in mass-descending order** — same helper, same
gate masses, non-negative by construction, byte-identical reduction when hybrid is off. A
lexical-resurrected hit gets a small (own-mass-floored) credit weight: conservative, and currently zero
live impact because the surfacer is observed-not-applied (`enabled=False`).

**D-G1 — knowledge-graph layer formally dropped** from the recall hot path: vector beats GraphRAG on
local-factual recall at ~3× less cost (the /socratic-5 adversarial review's falsification; reference
branches already stripped). *Add-back:* a separate global-sensemaking tool, never the recall hot path.

Sibling calls recorded in the plan: **D-V4** (FTS maintained always when available + boot rebuild, so
`recall.hybrid` is a pure read-path switch, never a data migration; a stripped SQLite degrades to
`fts_enabled=False` — silent with hybrid off, boot fail-fast with hybrid on) and **D-V5** (no registry
seam, no `lexical_backend`/`rrf_k` config — one conceivable backend is not a swap axis; `rrf_k=60` is
the canonical zero-tune code constant; lexical depth = `recall_top_n`, symmetric with dense).
