# Hivemind — Fleet Convergence: the true-MVP for a self-optimizing agent hivemind

**Status:** LANDED IN FULL — CV1–CV5 + CV7 on 2026-06-11; **CV6 on 2026-06-12** (commits
`convergence CV6.0–CV6.3`, after ADMIN-CLI landed 2026-06-11), implemented per the §8.2
amendment: single mirror-scanner deployment, mirror-aware main-ref ladder, squash-survival
layers + aged-unsettled alarm, and the task_outcomes v2 replacement (the §8.1 gate fired —
the Phase-0 clawback shape lacked `commit_sha`; replaced clean-store, boot-guarded;
`settled_exposures_since` rewritten onto the credit shape, win→+1/loss→−1). Recorded
deviations: CV2 store methods stayed duck-typed rather than Protocol-widened (D-C4,
built-decision-wins); `record_outcome` uses a PK-scoped `ON CONFLICT … DO NOTHING` (a
blanket `OR IGNORE` would swallow the CHECK). Operator guide: `HOWTO.md`.
**Date:** 2026-06-11 · CV6 2026-06-12
**Scope question answered:** "Starting from what exists, what is the minimum system that (a) every
dev-team agent — Claude Code, Cursor, Codex, custom LangChain/LangGraph/anything — reads and writes
without ceremony, (b) manages its own memory (noise, supersession, pruning) with maximum mechanical
autonomy, (c) keeps retrieval excellent as N grows, and (d) **provably** converges — more tasks
informed, fewer repeated failures, fewer tokens — improving on each margin over time?"
**Builds on (no change to):** AUTONOMY-PLAN (LANDED — quarantine → demand-promotion → decay →
human supersession), AUTH-PLAN (LANDED — token identity), REMOTE-ACCESS-PLAN (LANDED — HTTP+belts).
**Companions (approve + land first, as written; this plan re-specifies neither):**
- `HYBRID-RECALL-PLAN.md` — retrieval quality at scale (FTS5+RRF → capped rerank, CI-gated, off-default).
- `ADMIN-CLI-PLAN.md` — the `hive` operator CLI (fleet provisioning front door; CV6 adds one verb).

---

## 0. The position (read this first)

The hard core of the vision is **already landed**. The store is autonomous (capture → quarantine →
demand-promotion with structural anti-gaming → TTL decay), retrieval never hallucinates (entropy
gate), writes are secret-scanned, identity is unforgeable (token label), and the eval substrate
measures on labels with a CI ship rule — no judges, no load-bearing humans. What separates *today*
from *the fleet hivemind* is exactly four gaps, and the true MVP is closing them and nothing else:

| Gap | Today | This plan |
|---|---|---|
| G1 **Reach** | 6 harness profiles, no Codex, no story for custom frameworks | CV1: Codex profile + a vendorable stdlib client + framework recipes; token-per-agent contract documented |
| G2 **Trust ladder + self-correction** | Promotion stops at `provisional`; supersession 100% human; coexisting versions both serve | CV2: survival-establish (2nd mechanical rung); CV3: serve-time version shadowing + contested-memory report (mechanical staleness detection) |
| G3 **Convergence proof** | Counters exist; no trend, no KPI | CV4: windowed trends + the demand-entropy convergence KPI in `hive_health` |
| G4 **Outcome ground truth** | `Hive-Trace` trailer documented but "crediting not enabled"; `task_outcomes`/keystone dormant | CV6: host-side git-outcome credit scan (operator-cadence, never load-bearing) → feeds the existing readiness→keystone gate |

Plus CV5 (entropy-gate self-calibration, dev-time, CI-gated) — the requested **entropy-system
expansion**, §7.

**The graceful-degradation thesis (why instructions-only clients are acceptable).** The lifecycle
makes client integration failure *non-fatal by design*: an agent that forgets to capture merely
leaves recall misses; misses are recorded, clustered, and surfaced as demand; the next agent that
solves the problem captures it, and demand promotes it. Integration is best-effort everywhere
except the server. This is why the MVP does **not** need per-harness hook engineering parity —
tier-1 (rules-block) harnesses degrade to "slightly slower convergence," not "broken."

**What this plan refuses (deliberately, with add-back paths in §10):** server-side LLM anywhere
(consolidation/distillation stays a client-side write), autonomous retirement of `established`
(trust asymmetry stands), a graph layer (falsified), LLM-judge metrics, schedulers/daemons
(lazy + sweep piggyback only), per-harness bespoke servers (one server, thin projections).

---

## 0b. Acceptance criteria

| AC | Criterion | Owner |
|---|---|---|
| AC1 | A custom agent (no MCP runtime) integrates with **one vendorable stdlib file**: `HiveClient(url, token).recall/capture/write/fetch/health` over HTTP+bearer; tested in-repo against the real `run_http` server in-process. Codex onboards via a `codex` harness profile (AGENTS.md, tier 1). | CV1 |
| AC2 | **Survival-establish**: a `provisional` row exposed to ≥ `survival_e` distinct identities (writer excluded) across ≥ `survival_days`, with ≥ `survival_min_exposures` total exposures, auto-promotes to `established` with an audit row. Writer-only exposure never establishes (anti-gaming, same key as AC3-autonomy). Default ON; `autonomy.enabled=false` keeps today's byte-stability umbrella. | CV2 |
| AC3 | **Version shadowing** (`recall.shadow=true`, default **OFF**): within one confident shortlist, of two hits with pairwise cosine ≥ `shadow_tau`, only the winner serves (higher trust; tie → newer `ts`). Filter runs at RESOLVE, **before** exposure recording — a shadowed row's liveness is never refreshed by the query that hid it. OFF ⇒ byte-identical recall (golden test). | CV3 |
| AC4 | **Contested-memory report**: `hive_health(include_gaps=true)` additionally returns servable rows that recent misses cluster against (cosine ≥ `contested_tau`) — the mechanical "this memory is being re-asked / abstained around" supersession-review queue. Report-time compute only; no write-path change. | CV3 |
| AC5 | **Convergence trends**: `hive_health(include_trends=true)` returns current-vs-previous-window aggregates (confident/abstain/no_match rates, miss-cluster demand entropy, promotions, median days-to-promotion, dead-capture ratio, est. tokens served). Lazy SQL over existing tables; no new table, no scheduler. | CV4 |
| AC6 | **Gate self-calibration is CI-gated and dev-time**: `hive/research/gate_eval.py` sweeps (`h_frac_max`, `softmax_beta`) over a replayed labeled miss set, scores with the existing `abstention_auroc` + `bootstrap_ci`, and recommends a change only on `lo > 0`. Runtime never imports it; config change stays operator-applied. | CV5 |
| AC7 | **Outcome credit without a load-bearing step**: `hive credit <repo>` (CLI verb) scans host-side git history for `Hive-Trace` trailers, derives settled outcomes mechanically (merged-to-main = win, reverted = loss; ancestry checks, **no diff parsing**), and idempotently ingests `task_outcomes` rows via an in-container admin tool. Never running it changes nothing; running it feeds the existing readiness→keystone gate. `utility_rerank` still flips only on a keystone WIN. | CV6 |
| AC8 | **Zero**: new runtime dependencies; server-side LLM; scheduler; repo filesystem access by the server; new MCP tools (surface stays exactly 6); load-bearing human or cooperative-agent step. | all |
| AC9 | **Solo mode**: with `autonomy.solo_mode=true` (operator-set env, default OFF), the demand rule's distinct-identity clause relaxes to **elapsed-span demand** (`max(ts) − min(ts)` over matched misses ≥ `solo_min_span_days`, default 1 — the SurvivalRule span idiom) — a sub-24h burst NEVER promotes, even straddling UTC midnight; survival-establish is deliberately untouched, so in solo mode `established` is reachable **only** via human `hive_write` (HITL held structurally). Single-seat traffic with wasted demand surfaces a `solo_hint` in `hive_health`. | CV1 §3.5 |

---

## 1. Principles (locked, inherited + extended)

1. **Every load-bearing signal is server-observable** (AUTONOMY-PLAN v2 rule). New signals here:
   exposure-identity spread (CV2), shortlist self-similarity (CV3), miss↔servable proximity (CV3),
   windowed counts (CV4). The one host-side signal (git outcomes, CV6) is *accelerant, never fuel*.
2. **Trust asymmetry stands.** Mechanical paths can only *raise* trust through the audited ladder
   (quarantined→provisional→established) or let it lapse by TTL. Retirement of `established`
   remains human-only (`hive_write(replaces=)`). Shadowing (CV3) is serve-time suppression, not
   state change — it grants no retirement power.
3. **Default-preserving.** Anything that changes serve output of an existing store ships OFF
   (`recall.shadow`) or dev-time-only (gate_eval); lifecycle additions ride the existing
   `autonomy.enabled` umbrella. Replay (`eval_membrane`) is the regression instrument.
4. **Labels, never judges; CI, never point estimates.** Gate recalibration and channel flips ride
   `bootstrap_ci.lo > 0`. Outcome labels are git facts (merge/revert), not model opinions.
5. **One server, thin projections.** Harness differences live in `onboard.py` profiles and client
   recipes; the trust boundary and tool surface are byte-identical for every client.
6. **Identity is the currency.** Demand-promotion, survival-establish, and outcome credit all key
   on token identity. Operational contract, now documented loudly: **one token per agent seat** —
   a fleet sharing one token structurally cannot promote its own captures (writer == every
   identity ⇒ `self_demand`) and cannot survival-establish. `hive token <seat>` per agent.
   **Solo escape hatch (§3.5):** a genuinely single-context dev (one repo, one machine) can never
   have identity diversity; `autonomy.solo_mode` substitutes *time* persistence (elapsed-span
   demand, ≥24h first-to-last) for *identity* diversity in the demand rule only — a named,
   operator-consented weakening that still defeats the realistic solo failure (a runaway
   single-burst loop), while `established` stays human-only at solo scale.

---

## 2. Architecture (delta view; unchanged parts elided)

```
 claude-code (tier2 hooks) ┐
 cursor/windsurf/cline/    │ MCP over HTTP (existing)
 opencode/codex (tier1)    ├──────────────────────────────► ┌─ hive server (6 tools, unchanged surface) ─┐
 custom frameworks ────────┘ hive/client.py (CV1, stdlib)   │ recall: …gate→resolve ──► shadow filter     │
   (LangChain/LangGraph/any)   recall/capture/write/fetch   │   (CV3, default OFF) ──► exposure (after!)  │
                                                            │ lifecycle: DemandRule (landed)              │
 operator host                                              │          + SurvivalRule (CV2, sweep-time)   │
   hive credit <repo> ── git log trailers ──► creditctl ────┼──► task_outcomes (existing, dormant→fed)    │
   (CV6: merged=win / reverted=loss, ancestry only)         │ health: +trends (CV4) +contested (CV3)      │
                                                            │ research/: gate_eval (CV5, AST-fenced)      │
                                                            └─────────────────────────────────────────────┘
```

Trust ladder after CV2 (the complete mechanical ladder; human vouch unchanged):

```
            hive_capture            DemandRule (landed)             SurvivalRule (CV2)
  agent ──► QUARANTINED ──demand──► PROVISIONAL ──survival-spread──► ESTABLISHED
                 │ TTL 14d               │ TTL 45d (exposure-refreshed)    │ never decays
                 ▼                       ▼                                 ▼ human supersession only
             DEPRECATED ◄────────────────┘                  hive_write(replaces=) ──► DEPRECATED
```

---

## 3. CV1 — Reach: Codex profile + vendorable client + the seat-token contract

### 3.1 `codex` harness profile (`hive/app/onboard.py`, additive)

- `HARNESSES += "codex"` (tool enum in `tool_defs.py` `hive_init.harness` likewise).
- Profile: tier 1 (`TIER_RULES`), rules file default `AGENTS.md`, `rules_addendum` notes Codex's
  MCP registration lives in `~/.codex/config.toml` (`mcp_servers` entry with the bearer header) —
  emitted as *reference text* in the recipe, exactly like the other tier-1 profiles.
- No server behavior change; `hive_init(repo_path, harness="codex")` round-trips phases 1–2.

### 3.2 `hive/client.py` (NEW — stdlib-only, single-file, vendorable)

```python
class HiveError(Exception): ...          # one exception type; carries http_status + rpc_error

class HiveClient:
    """Minimal Hivemind client for agents without an MCP runtime. stdlib-only
    (urllib.request + json); copy this one file into any agent codebase, or import it.
    All methods raise HiveError on transport/auth/rpc failure — never return partial junk."""
    def __init__(self, url: str, token: str, *, timeout_s: float = 10.0) -> None: ...
    def recall(self, query: str, *, k: int | None = None) -> list[dict]: ...   # [] on abstain
    def capture(self, text: str, *, tags: list[str] | None = None,
                source: str | None = None) -> dict: ...
    def write(self, text: str, *, approved_by: str,
              replaces: int | None = None, tags: list[str] | None = None) -> dict: ...
    def fetch(self, content_hash: str) -> dict: ...
    def health(self) -> dict: ...
```

- Speaks the existing JSON-RPC `tools/call` envelope over POST + `Authorization: Bearer`.
  `recall()` returns the server envelope's `reference_context` list **verbatim** (no client-side
  reshaping) — the hit schema stays single-sourced in the server.
- Purity-fence test, **transitive**: importing `hive.client` must pull in nothing outside stdlib
  — including via `hive/__init__.py` (subprocess `sys.modules` assertion: no torch /
  sentence_transformers / numpy after import). A second test imports a *copied* `client.py` from
  a temp dir (the vendoring contract, enforced not prose).
- Tested **against the real server**: `run_http` on an ephemeral port with a real token store,
  in-process (the AUTH-PLAN test idiom) — not against a fake.

### 3.3 Framework recipes (`docs/CLIENTS.md`, NEW — docs, not code)

Fenced, untested-by-design recipes (they require deps this repo doesn't carry): LangChain
callback handler (recall on chain start → inject as context; capture on chain end), LangGraph
pre/post node, plain-Python agent loop. Each is ~20 lines around `HiveClient`. The repo ships
**no** framework imports (keeps groundcheck + the dependency invariant clean).

### 3.4 The seat-token contract (docs: README + `docs/CLIENTS.md` + `hive connect` hint)

One paragraph, stated as a hard operational requirement (§1.6): identity diversity is the
promotion fuel; shared tokens silently stall the autonomy loop. `hive status` already shows token
count; the contested/trends reports (CV3/CV4) make a stalled loop visible (`n_promotions = 0`).

**Per-seat is the default outcome of onboarding, not tribal knowledge:** the stdio registration
example every tier-1 recipe/playbook emits carries `--agent <repo-name>` (identity-per-project
for local sessions — the exec line is operator-controlled config, so INV-2 is untouched), and
the HTTP examples in `docs/CLIENTS.md`/`hive connect` say "mint one token per seat
(`hive token <seat>`)" inline, not in a footnote.

### 3.5 Solo mode (single-seat fleets) — the simplest implementation that achieves it

**Problem.** Per-seat identities solve multi-project solos (cross-project demand promotes as
designed), but a one-repo/one-machine dev structurally cannot produce identity diversity: every
miss carries the writer's own label ⇒ `self_demand` forever ⇒ the autonomy loop is inert and
captures silently TTL out.

**Mechanism (one flag, ~10 pure lines, zero new tables/tools/manifest changes):**

- `AutonomyConfig` gains `solo_mode: bool = False` and `solo_min_span_days: int = 1`
  (validated ≥ 1; both tier "B"). Set via the existing layered config — `.env`/compose:
  `HIVE_AUTONOMY__SOLO_MODE=true`. Server-side and operator-owned ⇒ **not client-gameable**.
- `DemandRule` (ctor gains the two values): when `solo_mode`, the diversity clause swaps —
  *distinct identity* → **elapsed-span demand**: `max(m.ts) − min(m.ts)` over the matched misses
  must be ≥ `solo_min_span_days · 86400` (`MissRow.ts` already exists — zero new data; the SAME
  span idiom SurvivalRule uses, so "demand persisted over time" means one thing in both rules).
  Two consecutive days therefore trigger it **iff ≥ 24h elapsed between first and last matched
  miss** — Mon 09:00 + Tue 09:00 promotes; Mon 23:00 + Tue 01:00 does not. Failure reason:
  `solo_span`. All other clauses (`demand_m`, `demand_tau`, competitor veto, fail-closed
  non-finite) unchanged; `n_other_identities` still computed into the audit payload.
- **Survival-establish (CV2) is deliberately NOT relaxed** — it keeps the distinct-identity key.
  Consequence, stated as the design's HITL property: in solo mode, mechanical promotion tops out
  at `provisional` (served, labeled, exposure-refreshed); **`established` is reachable only
  through `hive_write(approved_by=…)`** — the human stays the establishment authority by
  structure, not by directive wording. (No manifest change needed or made; the landed v2
  manifest already routes user-confirmed knowledge through `hive_write`.)
- **`solo_hint` in `hive_health`** (~6 lines): when `autonomy.enabled` AND NOT `solo_mode` AND
  the last-14d misses have ≤ 1 distinct identity AND ≥ `demand_m` misses exist (demand is being
  wasted, not just an empty store), the snapshot gains
  `solo_hint: "single-seat traffic: demand-promotion is inert under the anti-gaming rule — set
  HIVE_AUTONOMY__SOLO_MODE=true or provision per-seat identities (§3.4)"`. Converts the silent
  stall into a self-describing one, in the same spirit as the first-touch onboarding hint.

**What it deliberately trades (named):** elapsed-span is weaker than identity diversity — a
*persistent multi-day* injected attacker defeats it; at solo scale that adversary defeats nearly
everything, and the realistic failure (a runaway hook/loop bursting misses, even one alive
across UTC midnight) is still structurally blocked. Damage stays bounded by the provisional
label, the entropy gate, decay, and one-write supersession.

**Rejected simpler/heavier alternatives (design-twice):** a *bare waiver* of the diversity
clause — one line cheaper, but deletes the only structural anti-gaming defense for no gain over
the span clause; *calendar-day buckets* (`ts // 86400` distinct-day counting) — same cost but a
midnight-straddling burst counts as two "days" (the guard's one realistic bypass), and it
diverges from SurvivalRule's span idiom; *client-claimed sub-identities* (suffixing the token
label) — would let one agent forge diversity, breaking INV-2's point; *solo manifest variant* —
config-threaded wording churn that the structural established-stays-human property makes
unnecessary.

---

## 4. CV2 — Survival-establish (the second mechanical rung; Appendix-A rung adopted)

### 4.1 Pure rule (`hive/domain/lifecycle.py`, additive)

```python
@dataclass(frozen=True, slots=True)
class ExposureRow:                       # carrier the store returns for a candidate's window
    agent_id: str; ts: int

@dataclass(frozen=True, slots=True)
class SurvivalDecision:
    establish: bool; n_exposures: int; n_other_identities: int; span_days: float; reason: str

class SurvivalRule:
    def __init__(self, *, survival_e: int, survival_days: int, survival_min_exposures: int) -> None: ...
    def decide(self, *, writer: str, exposures: Sequence[ExposureRow], now: int) -> SurvivalDecision:
        """establish IFF, among exposures:
             identities = {e.agent_id} − {writer}        # writer's own reads never count (anti-gaming)
             len(identities) >= survival_e
         AND len(exposures) >= survival_min_exposures
         AND (max(ts) − min(ts)) >= survival_days        # spread over time, not one burst
        Pure, total, never raises; empty ⇒ establish=False. // O(|exposures|)."""
```

### 4.2 Trigger: sweep-time, not exposure-time

Evaluated inside the existing `LifecycleService.sweep(now)` (boot + post-capture piggyback):
**one aggregate query** prefilters candidates (never an N+1 per-row COUNT loop), then
`store.exposures_for(eid, since_ts)` runs only for those: → `SurvivalRule.decide` →
`set_trust(eid, ESTABLISHED)` + `establish` audit row (decision payload). *Rejected:* an
exposure-time trigger — adds hot-path work to every recall for a decision where hours of latency
is irrelevant. Sweep cost: one GROUP-BY over the window + O(candidates).

### 4.3 Risk, named (design-twice)

Auto-established content is wrong and now immune to TTL. *Rejected mitigation:* a fourth servable
trust state (`established_survival`) — splits the serving predicate and the consumer contract for
an audit-level distinction. **Chosen:** same `established` state; the audit row records
`rule=survival`; the contested report (CV3) keeps watching it; supersession stays the cheap
correction. Defaults (human-set 2026-06-11): `survival_e=2`, `survival_days=14`,
`survival_min_exposures=5` — establishment needs a 3-seat fleet minimum (writer + 2 distinct
readers); 2-seat fleets keep content provisional (still served, labeled).

### 4.4 Store additions (`store_sqlite.py` + ports; conformance-tested on fake AND real — the
protocol-widening rule)

```python
def survival_candidates(self, *, since_ts: int, min_exposures: int) -> list[tuple[int, str]]:
    # (episode_id, proposed_by) of PROVISIONAL rows with >= min_exposures exposures in the
    # window — ONE aggregate (JOIN exposure GROUP BY episode_id HAVING COUNT(*) >= ?).
def exposures_for(self, episode_id: int, *, since_ts: int) -> list[ExposureRow]: ...
```

### 4.5 Config (`AutonomyConfig`, additive; all tier "B")

`survival_e: int = 2` · `survival_days: int = 14` · `survival_min_exposures: int = 5`
(validated ≥ 1 each). Rides `autonomy.enabled` — no new umbrella flag.

---

## 5. CV3 — Serve-time shadowing + the contested-memory report (mechanical self-correction)

### 5.1 Version shadowing (`hive/domain/recall.py`, pure post-resolve filter; default OFF)

```python
def shadow_filter(hits: Sequence["ResolvedHit"], *, shadow_tau: float) -> list["ResolvedHit"]:
    """Within one confident shortlist: for any pair with cosine(value_i, value_j) >= shadow_tau,
    keep the winner — higher trust rank (established > provisional); tie → newer ts; tie → lower
    episode_id (stable). Non-finite vectors never shadow (fail-open = serve both, status quo).
    Pure. // O(k²·d), k ≤ recall_top_n."""
```

- Wired between resolve and surface; **exposure records only the post-filter served set** — a
  shadowed row's `last_active_ts` is not refreshed by a query that hid it, so a permanently
  shadowed provisional row decays naturally (the lapse-resurrection ordering rule, applied
  forward: side effects after filtering, always).
- **Vector source pinned:** pairwise cosines use the value vectors already in hand at resolve
  (the resolved episode carrier). If the current carrier drops the vector before this point,
  widen the **carrier**, never the `VectorIndex` port — the index owes search, not per-id
  vector lookup.
- **Placement vs HYBRID-RECALL channels:** the filter runs on the **final resolved shortlist**
  — after RRF fusion and after the capped rerank when those channels are on — i.e. always the
  last transform before the surfacer, so the dedup applies to what is actually served
  regardless of which upstream channels produced it.
- Trust-rank-first is deliberate: a newer unverified capture must not hide a human-vouched row.
  `established` vs `established`: newer wins (both vouched; recency is the only differentiator).
- `recall.shadow: bool = False` (tier C) · `recall.shadow_tau: float = 0.95` (tier B, validated
  in (0,1]). OFF ⇒ byte-identical (golden replay test). Flip-on evidence: an `eval_membrane`
  replay on the live store showing only intended near-dup suppression (Jaccard delta inspection)
  — operator-applied, reversible.

### 5.2 Contested-memory report (`hive/app/gaps.py`, additive; report-time only)

Extends the existing miss clustering: misses are clustered **first** (existing machinery), then
one `index.search(representative_vec, 1)` per cluster — never per miss (caps the search count at
the cluster cap, ~50× cheaper, same signal). Clusters whose representative's `top_sim >=
contested_tau` (default 0.80, config `autonomy.contested_tau`, tier B) group by that servable
episode:

```json
"contested": [{"episode_id": 41, "trust": "established", "miss_count": 7,
               "miss_types": {"abstained": 6, "no_match": 1}, "last_seen": "…"}]
```

Interpretation (documented in the envelope, fixed strings): repeated **abstains near a servable
row** = mass-splitting near-dups or a contradiction inside the store; repeated **re-asks** = the
served content isn't satisfying. Either way it is the supersession-review queue — the human (or
any agent, in chat) resolves it with one `hive_write(replaces=…)`. Window-capped (reuses the gaps
cap); zero write-path changes; no new state.

---

## 6. CV4 — Convergence trends (`hive_health(include_trends=true)`)

`hive/app/trends.py` (NEW, app-side like `gaps.py`): lazy SQL over existing tables
(`recall_misses`, `exposure`, `episodes`, `evidence_events`), two fixed windows (current 14d vs
previous 14d), no new table, no scheduler:

```python
@dataclass(frozen=True)
class TrendWindow:
    recalls_confident: int; misses_abstained: int; misses_no_match: int
    confident_rate: float                 # confident / (confident + misses)
    demand_entropy: float                 # H over miss-cluster mass / ln(n_clusters) ∈ [0,1]; 0 if <2 clusters
    n_promotions: int; n_establishments: int; n_supersessions: int
    median_days_to_promotion: float | None
    dead_capture_ratio: float             # ttl_expired(quarantined) / captures, this window
    est_tokens_served: int                # Σ len(text)//4 over served hits (cost proxy)

def compute_trends(store, gaps_clusterer, *, now: int) -> dict   # {"current": …, "previous": …, "deltas": …}
```

- **The convergence KPI, defined:** `confident_rate` ↑ AND `demand_entropy` ↓ (demand is being
  answered and what remains is concentrated/fillable, not diffuse noise) with `dead_capture_ratio`
  bounded (the fleet isn't writing junk). These are *coverage* proxies and the doc says so —
  outcome ground truth is CV6's job.
- "Safer" margin, measured mechanically: `secret_refused` count, abstain rate (the
  never-hallucinate floor working), contested count (CV3) trending down, quarantine pile-up
  visible. "Cheaper": `est_tokens_served` per confident recall.
- `trust_counts` already exists; trends compose, never duplicate it.

---

## 7. CV5 — Entropy-system expansion (the requested exploration, mechanical parts only)

The entropy machinery generalizes from *per-query abstention* to *store-level information
accounting* — same math, three productionized uses (two land via CV3/CV4, one is new dev-time):

1. **Demand entropy as the convergence KPI** (CV4): `H/ln(C)` over miss-cluster mass. Falling =
   unmet demand is concentrating into fillable gaps; ~1.0 = diffuse noise. The fleet-health
   number the vision's "converges to informing most tasks" cashes out as.
2. **Abstention as diagnosis** (CV3): an abstain *with a strong servable competitor* is the gate
   telling us the store itself is disordered there (near-dups splitting softmax mass, or a
   contradiction). The contested report routes that diagnosis to the cheap human fix.
3. **Gate self-calibration** (`hive/research/gate_eval.py`, NEW — dev-time, AST-fenced):
   ```python
   @dataclass(frozen=True)
   class GateEvalResult:
       arms: dict                      # (h_frac_max, beta) → {auroc, false_abstain_rate, recall_at_k}
       best: tuple[float, float]
       best_vs_current_ci: tuple       # paired bootstrap_ci on per-query correctness delta
       recommend: bool                 # ci.lo > 0
   def run_gate_eval(spec, *, sweep, n_boot: int = 10_000, seed: int = 0) -> GateEvalResult: ...
   ```
   Labels are **reconstructable from stored data only** (no served-query vectors exist, so
   "a later query succeeded" is not recoverable): replay each stored miss **vector** against
   today's servable index *restricted to rows with `ts < miss.ts`* — a strong top-1
   (`sim ≥ label_tau`) means the answer existed when the gate abstained ⇒ false-abstain
   candidate; a weak field ⇒ true abstain. Plus the existing labeled eval sets. Scored with the
   **existing**
   `abstention_auroc` + `bootstrap_ci`; recommends a `recall.h_frac_max`/`softmax_beta` change
   only on `lo > 0`; the operator applies it (tier-B hot reload). Closes HYBRID-RECALL §10's
   "does the gate already cap recall?" question with standing instrumentation.
4. **Deferred (named, §10):** novelty-weighted TTL — store admission novelty
   (`1 − competitor_top_sim`, already computed at capture) and decay low-novelty quarantine
   faster. One column + one multiply, but a new knob with no demonstrated need while TTL+belts
   bound noise. Add back if `dead_capture_ratio` trends high with near-dup-dominated quarantine.

---

## 8. CV6 — Outcome credit (host-side, operator-cadence, never load-bearing)

The honest gap: CV4's proxies measure *coverage*, not whether memories make the team faster/safer.
Ground truth needs task outcomes. The repo already ships the full consumption chain — exposure
margins → `task_outcomes` (dormant) → `readiness` floors → `keystone` 4-arm causal gate →
`utility_rerank` flip — and the rules block already documents the `Hive-Trace: <trace_id>` commit
trailer ("crediting not enabled in this build"). CV6 enables collection, changing **no** gate:

### 8.1 Host-side scan (`hive/tools/creditctl.py` NEW + one CLI verb `hive credit [path]`)

```python
def scan_repo(repo_path: str, *, trailer_key: str = "Hive-Trace",
              main_ref: str | None = None,
              since: str | None = None, run: Run | None = None) -> list[OutcomeRow]:
    """git log --grep=<trailer_key>: --format=<NUL-delimited fields> (NEVER diff parsing —
    classify by format fields and rev-list ancestry, not content prefixes):
      merged  := commit is ancestor of the resolved main ref → outcome 'win'
      reverted:= a later commit ON MAIN names the sha via git's own revert format → 'loss'
      else    := unsettled — emitted with settled=False, never ingested
    Extracts trace_ids from the trailer line(s). DEFAULT = full-history scan every run —
    idempotent ingest makes re-scans free, and it sidesteps the unsettled-then-settled trap a
    high-water mark would create (a commit unsettled at scan N must be re-seen at scan N+1).
    `since` is an optional rev-range optimization the operator owns, not state we keep.
    Main-ref resolution (amended 2026-06-12, mirror-aware): an explicit `main_ref` wins;
    else the first of origin/main → origin/master → main → master that rev-parses — a bare
    --mirror clone has no origin/* remote-tracking refs, so the bare names must be in the
    ladder. trailer_key defaults to the producer.stamp_trailer contract value ("Hive-Trace",
    the same string the rules block and link records carry); the host CLI cannot read
    container config, so it is an overridable default, not injected config."""

def ingest(store, rows: Sequence[OutcomeRow], *, now: int) -> dict:
    """In-container (python -m hive.tools.creditctl ingest --ndjson -): trace_id → exposure rows
    → (episode_id, margin) credit set → task_outcomes upsert keyed (commit_sha, episode_id) —
    re-runs never double-credit. Unknown trace_ids count in the report, write nothing."""
```

- CLI verb shape (ADMIN-CLI idiom): `hive credit` runs `scan_repo` host-side (the repo lives on
  the host; **the server still never reads repos**), pipes NDJSON to
  `docker compose exec -T hive-server python -m hive.tools.creditctl ingest` — the authctl
  pattern exactly. No scan state is persisted (full scan + idempotent ingest, §8.1).
- **Implementation gate:** the dormant `task_outcomes` DDL must support the `(commit_sha,
  episode_id)` idempotency key — verify at CV6 start; if the legacy shape lacks `commit_sha`,
  extend additively (clean-store regime applies; no migration machinery).
- **Never load-bearing:** never run ⇒ today's system, byte-stable. Run weekly/by cron at the
  operator's choice ⇒ `readiness` floors (200 settled / 30 credited memories) fill; only a
  keystone WIN flips `utility_rerank` — that discipline is untouched.
- Crude-labels honesty: merge/revert is a coarse win/loss. That is exactly the v1 "ungameable
  clawback" design — the keystone's job is to decide whether this signal beats recency/frequency
  causally; if it can't, the flip never happens. No judge enters.

### 8.2 Deployment model (amended 2026-06-12): one mirror scanner; squash-proofing; aging alarm

**Everything CV6 ever ingests is visible from `main` alone** — wins are trailer commits
reachable from the main ref, losses are revert commits *also on main* naming them, and
unsettled rows are never ingested. Therefore the canonical deployment needs no seat clone
and no per-seat infrastructure:

```
# server host, once per tracked repo (a dedicated scan mirror, NOT a workspace;
# read-only deploy key):
git clone --mirror git@host:team/repo.git ~/hive-scan/repo.git
# cron (daily/weekly — operator cadence):
git -C ~/hive-scan/repo.git fetch && hive credit ~/hive-scan/repo.git
```

Seats only stamp trailers. Any working clone MAY also run `hive credit` (idempotent
`(commit_sha, episode_id)` ingest makes overlapping scans free); the mirror is simply the
deployment that makes "the server host is not where repos are worked on" irrelevant.

**Squash-merge trailer survival** (the only flow that destroys trailers — merge commits and
rebase-merges carry them onto main natively), three layers, strongest first:
1. *Mechanical:* set the repo's squash-message default to **"PR title and commit details"**
   — constituent commit messages (trailers included) land in the squash commit on main.
2. *Convention:* agents also put their `Hive-Trace:` lines in the **PR description** (the
   "title and description" squash default carries it). Manifest-v3 wording for this is
   DEFERRED — `HOWTO.md` carries it operator-side today.
3. *Alarm:* the scan report flags trailer commits aged beyond a threshold that never
   settled (a GitHub `--mirror` clone also fetches `refs/pull/*/head`, which outlive
   deleted branches). Report-only — never a gate ("accelerant, never fuel").
No double credit under squash: only the main-side sha settles; the original feature-branch
shas stay unsettled forever and are never ingested.

**`task_outcomes` DDL — the §8.1 implementation gate FIRED (2026-06-12):** the legacy table
was the Phase-0 clawback shape — `PRIMARY KEY(task_ref, trace_id)`, a
provisional/settled/clawed-back state machine, and `files_touched`/`introduced_lines`
diff-text columns this plan explicitly rejects. It is dormant (zero writers; readiness/
keystone consume assembled counts, not the table). Under the clean-store regime it is
REPLACED, not migrated: `PRIMARY KEY(commit_sha, episode_id)` with
`trace_id, repo, outcome CHECK(win|loss), recall_margin, commit_ts, ingested_ts`; the boot
guard (episodes-table precedent) extends to refuse a legacy-shaped `task_outcomes`.

**Per-seat HTTP ingest** (an authenticated `tools/call` submission path for repos the server
host cannot fetch — e.g. never-pushed local-only repos on another machine) is a DEFERRED
add-back (§10); the mirror model removes the need for every push-to-remote team.

---

## 9. Implementation chunks (TDD; each green + RULE-2'd before the next)

> Standing mutation hygiene: restore via Edit with unique anchors (never sed); mutation runs
> foreground under `timeout`; clear `__pycache__` after same-size restores. Test conns through
> the prod `connect()` factory. New/changed port methods conformance-tested on fake AND real.

**CV1 — Reach + solo mode** *(independent)*
Files: `hive/app/onboard.py` (+codex profile, +`--agent <repo-name>` in stdio recipe text),
`hive/app/tool_defs.py` (enum), `hive/client.py` (NEW), `docs/CLIENTS.md` (NEW), README
(seat-token contract + solo addendum), `hive/domain/lifecycle.py` (`DemandRule` solo clause),
`hive/app/config.py` (`solo_mode`/`solo_min_span_days` + RELOAD_TIER), `hive/app/mcp_server.py`
(`solo_hint`), `hive/app/container.py` (thread the two values), `tests/clients/test_hive_client.py`
(NEW, against in-process `run_http`), `tests/onboard/` (+codex), `tests/domain/test_demand_rule.py`
(+solo), `tests/mcp/` (+hint), `tests/test_purity.py` (+client stdlib fence).
| Test | Assertion |
|---|---|
| `test_codex_profile_round_trips` | phase-1 plan + phase-2 confirm for `harness="codex"`; tier 1; AGENTS.md default. |
| `test_client_recall_capture_write_fetch_health` ★ | against real `run_http`+real token: recall []→hits round-trip; capture lands quarantined; write lands established; 401 on bad token raises `HiveError`. |
| `test_client_never_partial_on_transport_error` | connection refused / non-JSON / rpc-error ⇒ `HiveError`, never a half-dict. |
| `test_client_is_stdlib_only` | import fence (vendorability contract). |
| `test_solo_sub_day_burst_never_promotes` ★ | solo_mode: `demand_m` matched misses within < 24h elapsed — including a fixture straddling UTC midnight (23:59 + 00:01) — ⇒ no promote, reason `solo_span` (AC9 — the runaway-loop guard, bucket-proof). |
| `test_solo_span_promotes_same_identity` | solo_mode: m matched misses from ONE identity with `max(ts)−min(ts) ≥ solo_min_span_days·86400` (boundary: exactly 24h passes) ⇒ promote (the solo unlock). |
| `test_solo_off_keeps_identity_clause` | solo_mode=False ⇒ writer-only demand still `self_demand` (landed behavior byte-stable). |
| `test_solo_established_stays_human_only` | solo_mode acceptance: demand reaches `provisional`; SurvivalRule (CV2) still refuses writer-only exposure ⇒ only `hive_write` lands `established`. (AC9) |
| `test_health_solo_hint_fires_precisely` | hint present iff enabled ∧ ¬solo_mode ∧ ≤1 distinct miss identity (14d) ∧ ≥ demand_m misses; absent on empty store and in solo_mode. |
RULE-2: client swallows rpc error → ★ red; codex profile returns tier 2 → profile red. Solo:
span computed as calendar-day buckets (`ts//86400` distinct-count) instead of elapsed
`max−min` → the midnight-straddle arm of burst ★ red; span compare `>=` flipped → both solo
tests red; solo clause applied when solo_mode=False → byte-stable test red; hint condition
drops the ≥`demand_m` floor → empty-store arm red.

**CV2 — Survival-establish** *(independent of CV1)*
Files: `hive/domain/lifecycle.py`, `hive/domain/ports.py`, `hive/adapters/store_sqlite.py`,
`hive/app/config.py`, `tests/domain/test_survival_rule.py`, `tests/store/` (+2), acceptance.
| Test | Assertion |
|---|---|
| `test_establishes_at_spread_thresholds` | ≥e identities (writer excluded) + ≥min exposures + ≥days span ⇒ establish. |
| `test_writer_only_exposure_never_establishes` ★ | the anti-gaming arm. |
| `test_burst_without_span_does_not_establish` | 10 exposures in one hour ⇒ no. |
| `test_sweep_establishes_and_audits` | sweep promotes an eligible provisional row; `establish` audit payload round-trips; idempotent. |
| `test_established_by_survival_never_decays` | post-establish TTL irrelevance (existing `decayed` truth table extended). |
RULE-2: drop the writer-exclusion → ★ red; span check inverted → burst test red.

**CV3 — Shadowing + contested** *(after CV2 — trust-rank ordering shared)*
Files: `hive/domain/recall.py` (+`shadow_filter` + wiring), `hive/app/gaps.py`,
`hive/app/config.py`, `tests/domain/test_shadow_filter.py`, `tests/app/test_gaps_contested.py`.
| Test | Assertion |
|---|---|
| `test_shadow_off_is_byte_identical` ★ | golden: `recall.shadow=False` ⇒ output equals today's. |
| `test_shadow_prefers_trust_then_ts` | established beats newer provisional; newer established beats older established. |
| `test_shadowed_row_gets_no_exposure` ★ | ledger sees only the post-filter set (the resurrection-ordering pin). |
| `test_nonfinite_never_shadows` | NaN vector ⇒ both serve (fail-open documented). |
| `test_contested_groups_misses_by_servable_neighbor` | fixture misses near an established row ⇒ one contested entry, correct counts/types. |
RULE-2: filter runs after exposure → ★ red (the headline ordering mutation); trust-rank dropped →
preference red; `contested_tau` applied `<` → grouping red.

**CV4 — Trends** *(after CV3 — composes gaps clustering)*
Files: `hive/app/trends.py` (NEW), `hive/app/mcp_server.py` (health param), `tests/app/test_trends.py`.
| Test | Assertion |
|---|---|
| `test_windows_partition_correctly` | events at window edges land in exactly one window. |
| `test_demand_entropy_bounds` | one cluster ⇒ 0.0; uniform clusters ⇒ ~1.0; [0,1] clamp; <2 clusters ⇒ 0.0 (no div-by-zero). |
| `test_rates_and_ratios_zero_safe` | empty store ⇒ all fields present, no ZeroDivisionError. |
| `test_health_trends_shape` | envelope contract: current/previous/deltas keys pinned. |
RULE-2: swap window boundaries → partition red; entropy normalized by N not C → bounds red.

**CV5 — gate_eval** *(independent; dev-time)*
Files: `hive/research/gate_eval.py` (NEW), `tests/research/test_gate_eval.py`, purity fence.
| Test | Assertion |
|---|---|
| `test_recommend_only_on_ci_lo_gt_0` ★ | point gain with `lo<=0` ⇒ recommend=False (the standing ship rule). |
| `test_false_abstain_labeling_from_replay` | the miss→later-confident-from-preexisting-row labeler on a fixture store. |
| `test_runtime_never_imports_research` | existing AST fence covers the new module. |
RULE-2: `recommend` reads point estimate → ★ red.

**CV6 — creditctl** *(after ADMIN-CLI lands; verb rides its dispatch table; §8.2 amendment applies)*
Files: `hive/tools/creditctl.py` (NEW), `hive/tools/cli.py` (+`credit`, + keyword-only `input=`
widening of `default_run`), `hive/adapters/store_sqlite.py` (task_outcomes v2 DDL + guard +
`record_outcome`/`exposures_by_trace`), `tests/tools/test_creditctl.py` (fixture repos built by
the test), `tests/store/` (+outcome tests), `tests/container/test_cli.py` (+verb argv).
| Test | Assertion |
|---|---|
| `test_scan_classifies_merge_revert_unsettled` ★ | fixture repo: merged trailer commit ⇒ win; reverted ⇒ loss; branch-only ⇒ unsettled. Ancestry via rev-list, asserted no diff text is ever parsed (no `--patch` in argv). |
| `test_ingest_idempotent_no_double_credit` ★ | same NDJSON twice ⇒ identical `task_outcomes` counts ((sha,eid) keyed). |
| `test_unknown_trace_ids_reported_not_written` | counts in report, zero rows. |
| `test_unsettled_then_settled_credits_once` | scan while branch-only ⇒ nothing ingested; merge; full re-scan ⇒ exactly one win row (the trap the full-scan default closes). |
| `test_mirror_bare_clone_resolves_main` (§8.2) | scan of a `--mirror` clone (no origin/*) resolves the bare main ref and classifies identically. |
| `test_squash_carried_trailer_settles_main_sha_only` (§8.2) | trailer present only in the squashed commit message on main ⇒ one win row keyed by the MAIN-side sha; the branch originals stay unsettled; no double credit. |
| `test_report_flags_aged_unsettled` (§8.2) | a trailer commit older than the aging threshold and not settled ⇒ counted in the report's aging field; never ingested. |
RULE-2: drop the upsert key → idempotency ★ red; ancestry check inverted → classification ★ red.

**CV7 — Docs** *(no code)*: 02-CONTRACTS (client envelope, new store methods, trust ladder
diagram), 06-DESIGN-DOC (decision rows), 01-DECISIONS (CV2 risk acceptance, CV3 OFF-default,
CV6 boundary), README (seat tokens, convergence KPI), this file → LANDED. `graphify update .`.

**Order safety.** CV1/CV2/CV5 are mutually independent (parallelizable). CV3 follows CV2
(trust-rank), CV4 follows CV3 (clustering reuse), CV6 follows ADMIN-CLI. Everything is additive;
the only serve-output change (shadowing) ships OFF behind a golden byte-stability test.

---

## 10. Dependencies, exclusions, deferrals

**New dependencies: none** (stdlib + existing numpy; client is stdlib-only by fence).
**Surface: still exactly 6 MCP tools** — health params and envelope fields are additive.

| Deferred | Add-back path |
|---|---|
| Librarian/consolidation as a client-side skill (LLM distills contested/near-dup clusters → one `hive_write(replaces=)`) | a documented agent skill once contested reports accumulate; server stays LLM-free |
| Cursor tier-2 native hooks | a `HarnessProfile` hook-file emitter when Cursor's hook API stabilizes |
| Novelty-weighted quarantine TTL (§7.4) | one column + `decayed` term, if dead-capture ratio trends high on near-dups |
| `hive_evidence` / client-reported corroboration kinds | AUTONOMY-PLAN Appendix A, unchanged |
| Capture-side `replaces` claims (autonomous supersession of `established`) | Appendix A; requires the evidence layer + keystone gate first |
| ANN index backend at RAM-scale N | the standing socratic condition: self-run load test, accepted <1.0 recall |
| Per-hit serve-text truncation (`max_chars`) | a recall config knob if `est_tokens_served` trends high |
| Session threading (`session_id` independence refinement) | Appendix A, unchanged |

## 11. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Survival-established wrong content becomes permanent | spread defaults (2 non-writer identities/14d/5 exposures); audit row; contested report watches it; supersession stays one cheap write (§4.3, accepted knowingly). |
| Shadowing hides the *correct* older version | trust-rank-first (vouched beats newer-unverified); OFF by default behind a golden; replay inspection before flip; pure filter ⇒ one-flag rollback. |
| Trends/contested compute cost on big windows | window caps reused from gaps; COUNT short-circuits; all report-time (no hot-path cost). |
| Credit scan misattributes (squash-merges, trailer typos) | trailer grep is exact-key; unknown traces reported-not-written; (sha,eid) idempotency; keystone is the final causal arbiter — a noisy signal that can't beat recency never ships. |
| Fleet shares one token → autonomy silently stalls | documented contract (§1.6, §3.4); visible in trends (`n_promotions=0`) + `hive status` token count; **self-describing via the `solo_hint`** (§3.5); single-context solos get `autonomy.solo_mode` (day-spread diversity, AC9). |
| Solo mode weakens anti-gaming (elapsed-span < identity diversity) | operator-consented env flag, default OFF; sub-24h bursts structurally blocked even across UTC midnight; `established` stays human-only at solo scale; provisional label + entropy gate + decay + one-write supersession bound the damage (§3.5, named trade). |
| Client lib drifts from server envelope | client tested in-repo against the real `run_http` server on every suite run. |

---

**Summary.** This plan closes the four gaps between the landed autonomous store and the fleet
vision: universal reach (Codex profile + a vendorable stdlib client + the seat-token contract),
a completed mechanical trust ladder (survival-establish) with serve-time version shadowing and a
contested-memory report as the self-correction surface, a measurable convergence KPI
(demand-entropy + confident-rate trends) in `hive_health`, CI-gated entropy-gate
self-calibration, and host-side git-outcome credit that feeds the existing keystone gate without
ever becoming load-bearing. Zero new dependencies, zero new tools, no server-side LLM, no
scheduler; every serve-output change ships OFF behind a golden test, and every activation
decision rides labels + bootstrap CI — never a judge, never a point estimate.
