# U4-THIN-AGENT — single store, repo-partitioned memory, fully mechanical + agent-adjudicated trust

**Target state, one line:** a single store, partitioned by repo at the memory level; agents are
thin, repo-agnostic MCP clients that recall and store; the server owns mint, staleness, poison,
outcome, and promotion; trust is fully mechanical + agent-adjudicated — no humans, no AGI sentinel.

Status: **DRAFT — awaiting human confirmation. No code until confirmed (hard gate).**

---

## 0. Scope summary

| # | Subsystem | Change |
|---|-----------|--------|
| 1 | Onboarding / contract | delete the install/contract-version apparatus; keep a trimmed served `initialize.instructions` |
| 2 | Edge CLI + client hooks | delete agent-side; engines stay vendored server-side (`vendor/wheels/`) |
| 3 | Anchors / repo scope | anchor becomes a SET of `(repo, anchor)` rows; repo scope is per-memory; recall is scope-filtered pre-gate |
| 4 | Sync | N-repo registry (store table + `hive repo` verbs); per-repo mirror/graph/watermark; PR candidate leg **deleted** |
| 5 | Mint | server-authoritative + eager (backfill sweep is the path); per anchor, at that repo's canonical tip |
| 6 | Staleness | server-side drift at recall from a materialized per-`(repo, tip)` verdict cache; BUG-037 version-gate honored |
| 7 | Outcome / census | per repo, canonical line only; verified riders now land on the canonical `post_merge` ingest |
| 8 | AGI_MODE | deleted entirely |
| 9 | Human vouch | deleted entirely; `provenance` + `approved_by`/`approved_ts` columns deleted |
| 10 | Retirement | `hive_prune`/`hive_supersede`/`hive_write(replaces=)` gated on a qualifying MACHINE signal (server-verified) |
| 11 | Recall result | each hit gains `repos`, `anchors`, `drift` |

Confirmed decisions (from the intent author): retirement **must** cite a qualifying machine
signal; recall with no repo scope = **global search**; the top tier **reuses `established`**;
the stored corpus is **disposable** — rollout is a full reset, nothing carried over; an
UNQUALIFIED retirement attempt is a benign **no-op, never an error** (for `hive_write(replaces=)`
the write itself still lands — only the retirement rider no-ops); contract tests must exercise
**edge cases, not just the happy path** — explicitly including the `replaces=` gate-fails vs
gate-passes branches.

---

## 1. Grounding (what exists — files read, laws honored)

Grounded in full reads of: `hive/app/{sync,mcp_server,config,tool_defs,container,http_server}.py`,
`hive/domain/{lifecycle,admission,recall,models,conflict,change_evidence,agi,provenance,evidence_kinds,ports}.py`,
`hive/adapters/store_sqlite.py`, `hive/app/onboard_ref.py` (structure), `CONTEXT/THEORY.md`,
`CONTEXT/INTERACTIONS.md`, `CONTEXT/BUGS.md` (all 48 entries reviewed for regression risk).

Law revisions this plan **owns deliberately** (THEORY.md updated in the same change — §10 below):
- **Law 3** — trust is still measured, never asserted; the human vouch is REMOVED as an evidence
  source and canonical-line outcome evidence becomes the top rung (`established` = outcome-verified).
- **Write side** — goes serve-as-provisional-then-heal: `hive_write` is servable immediately,
  labeled `provisional`, actively worked by the heal loop.
- **O7 boundary** — moves from "human-vouched resolution" to "machine-evidence-gated agent
  resolution". Automatic/unbidden resolution stays refused: retirement still requires an agent
  CALL, and the call is refused without qualifying machine evidence.
- The read side is untouched: recall still abstains rather than serve a weak match (Law 1).

---

## 2. Design decisions (design-it-twice results)

Each major decision was sketched at least two ways; winners below with the losing option and why.

**D1 — Where repo scope + anchors live.**
*Winner:* a new relational table `episode_anchors(episode_id, repo, anchor, fp_meta)`;
`repos(episode)` = `SELECT DISTINCT repo`; scope-only membership = a row with `anchor=''`;
general = zero rows. `episodes.anchor` column is dropped.
*Loser A (meta-envelope encoding):* violates THEORY §5 — the server must never interpret `meta`
bodies (only the version prefix); a scope filter would have to. Disqualified.
*Loser B (JSON column on episodes):* prose-ish contract, per-row JSON parsing on every recall,
unqueryable census join. The table gives an enforced contract (SQL), a queryable per-repo join for
census, and one owner for per-anchor fingerprints (principles: *make illegal states
unconstructable*, narrow surface + enforced contract).

**D2 — Repo registry.**
*Winner:* a durable `repos` store table + `hive repo add/remove` / `hive repos` CLI verbs shelling
to a new in-container `hive.tools.repoctl` (the exact `authctl` seat pattern — the registry is
OPERATIONAL data like seats, not boot config, so THEORY §9.14 "config frozen at boot" is
untouched). The sync daemon re-reads the registry each tick — registering a repo needs no restart.
Per-repo git credentials stay in env via indirection: the row stores `token_env` (the NAME of an
env var; default `HIVE_SYNC__TOKEN`), never a secret byte (global secrets rule).
*Loser (env list `HIVE_SYNC__REPO_URLS`):* an N-repo × (url, ref, token) mini-grammar inside one
env string — a "Hard to Describe" red flag; restart per registration; no first-class repo NAMES,
which recall scope needs.

**D3 — Mint/drift engine.**
*Winner:* keep vendoring all three wheels and keep the subprocess seam: the server shells the
in-image `hive-edge mint` (fingerprints) and `hive-edge verify` (drift verdicts) against mirror
worktrees. `hive-edge verify` already implements the exact verdict logic the drift classifier
needs — version envelopes (BUG-037), prose-anchor routing to `unverifiable` (BUG-022),
single-source-root offset bridging (BUG-035/036) — and mint stays byte-identical, so the
"one mint owner fleet-wide" invariant survives with zero fingerprint-format fork risk.
*Loser (absorb the mint/verify core into hivemind now):* the right final shape, but it re-ports
the most regression-prone code in the system (the BUG-035/038/039 dialect family) inside an
already-large change. Named as a follow-on; the wheel is a server-internal engine like
matrix/comb-drift. Agent-side hive-edge (installs, hooks, docs) still dies in THIS change.

**D4 — Retirement gate shape.**
*Winner:* the server SEARCHES for qualifying evidence itself on every `hive_prune` /
`hive_supersede` / `hive_write(replaces=)`; no qualifying signal ⇒ a `refused` envelope; the audit
row stamps exactly which signal(s) qualified. Surface stays narrow (`prune(episode_id)`), and the
"wrong citation" error is defined out of existence (*pull complexity downward*).
*Loser (agent passes `{signal, evidence_id}`):* wider surface, a new failure mode, and the server
must verify the citation anyway — strictly more machinery for the same guarantee.
*Refinement (intent author):* an unqualified attempt is a benign **no-op**, never an error — see
§3.2 outcome semantics; for `hive_write(replaces=)` only the retirement rider no-ops, the write
still lands.
*Hardening within the confirmed decision:* an agent-reported `outcome_hurt` row qualifies only
when its recorded actor ≠ the retiring caller's identity (the DemandRule identity-diversity
clause, applied to retirement) — without it, `hive_outcome(hurt=[id])` + `hive_prune(id)` is a
two-call self-authorized destruction of any healthy memory. Server-derived signals
(`verify_stale`, `stale_suspect`, `outcome_verified_hurt`, live drift, mechanical contradiction)
have no such requirement. Advisory `hive_flag` rows do NOT qualify (agent-asserted).

**D5 — Drift at recall.**
*Winner:* recall reads ONLY a materialized `anchor_drift` cache keyed `(repo, tip_sha, anchor)`;
the sync tick materializes verdicts (worktree at tip + `hive-edge verify` per anchor, capped and
carried over). An un-materialized `(repo, tip)` reads `unverifiable` (fail-safe — never
false-fresh, never false-stale). A `ref_requests` table (touched on recall) drives on-demand
materialization of non-canonical branch tips.
*Loser (compute at recall):* a subprocess in the serve path under the global lock — violates the
"a slow engine can never stall the serve path" invariant and recall latency.

**D6 — Scope-filter placement.**
*Winner:* one shared exhaustive index (unchanged, id→vector); recall filters the candidate list
between search and gate through a new narrow read `servable_scopes()` → `{eid: (repos, anchors)}`,
then the gate evaluates ONLY the scoped sims. The absolute-relevance gate is partition-safe by
construction (no distribution-shape dependence), so `tau_serve` is preserved — the intent's
argument, verified against `AbsoluteRelevanceGate.evaluate`.
*Losers:* per-repo indexes (N caches to invalidate, no gain at this N); repo labels inside the
index (widens the index port beyond id→vector — Law 5 cache purity).

**D7 — Schema migration posture.**
*Winner:* schema v3, clean start, NO in-place migration — exactly the shipped posture
("this build has no migration — start from a clean store/volume", `store_sqlite.py:117`).
Operational consequence in §9.

**Proposed-design scores** (reasoned per the review rubric, not measured):
complexity **3/10** ("mostly sound; a few shallow spots; no change-amplification" — every new
concern lands in one owner: scope in one table, gate in one function, drift in one cache);
cognitive load **3/10** ("small, learnable surface" — the agent surface SHRINKS to recall/store +
ids-only maintenance verbs; *modules should be deep*); information leakage **2/10** ("each design
decision lives in exactly one module" — repo identity normalization single-owned, verdict mapping
single-owned); extensibility **8/10** ("most likely changes are localized behind stable
interfaces" — a new repo is a row, a new drift class is one enum + classifier arm); agent-navigability
**8/10** ("mostly self-contained modules, enforced contracts" — schemas + frozen carriers + the
rewritten instruction layer stay single-source).

---

## 3. The redesigned contract (normative)

### 3.1 Trust lifecycle (zero human, zero AGI)

```
store:
  hive_write   → PROVISIONAL, servable now, server-minted fps    (preferred)
  hive_capture → QUARANTINED (unservable) → demand-promote       (unclear-value tail)

promote (mechanical only; every transition writes its audit row):
  quarantined → provisional : demand (m misses, ≥1 OTHER identity, competitor veto)  [unchanged]
  quarantined → provisional : verified-win rung                                      [unchanged]
  provisional → established : ≥1 SHA-bound outcome_verified_helped row (canonical line)  [NEW —
                              replaces the human vouch as the ONLY path to the top tier]

retire (agent-adjudicated, machine-GATED):
  servable → deprecated : hive_prune / hive_supersede / hive_write(replaces=) — the server
                          verifies a qualifying machine signal exists for the target; otherwise
                          the call is refused (nothing retired). No approver argument exists.

decay: quarantined/provisional TTL-decay unchanged; established never decays.        [unchanged]
```

`established` on the wire now means **outcome-verified on the canonical line** (was: human-vouched).

### 3.2 The retirement-evidence gate (single owner: `hive/domain/retirement.py`)

A retirement target is **eligible** iff at least ONE holds (checked in this order; the audit
records every satisfied clause):

1. **drift** — the newest materialized drift verdict for ANY of its anchors at that repo's
   canonical tip is `anchor_missing`, `anchor_changed`, or `blast_radius_changed`; or the newest
   `verify_stale` ledger row is newer than any `verify_current` (ledger form of the same fact).
2. **outcome-hurt** — a `outcome_verified_hurt` row exists (server-written, non-forgeable), OR an
   `outcome_hurt` row exists whose recorded actor ≠ the CALLING identity.
3. **contradiction** — a mechanical near-dup contradiction/redundancy is detected at gate time
   between the target and a co-servable row (for `supersede`, cos(loser, winner) ≥ `conflict.tau`
   also qualifies — the successor demonstrably answers the same need).
4. **general memories** (no repo, no anchor) — clauses 2–3 only (`drift` is `n/a`).

**Outcome semantics — unqualified means NO-OP, never an error** (defines the "invalid retire"
error out of existence; matches the existing unknown-id/self-supersede noop envelopes):
- `hive_prune` / `hive_supersede` on an unqualified target →
  `{"status":"noop", "reason":"no qualifying machine signal — nothing retired", …, "signals":[]}`;
  nothing retired, `isError=false`. A qualified call retires and stamps `signals` in the audit.
- `hive_write(replaces=X)` — the WRITE always proceeds (after secret scan/validation); the
  retirement rider applies only when X qualifies. Unqualified ⇒ the new memory IS stored, X is
  untouched, and the envelope reports `"superseded": null` +
  `"supersede_noop": "no qualifying machine signal"` (both rows coexist — the pre-supersession
  status quo, exactly today's benign-refused-supersede posture in `_apply_supersession`).
  An UNKNOWN `replaces` target keeps today's distinct behavior: the whole call fails loudly and
  nothing is stored (a caller bug, not an unqualified target).

The gate function is pure (`retirement_evidence(...) → Eligibility`), fed by ledger reads + the
drift cache + the conflict detector; deleting the gate CALL in a handler is a named mutation
(its test: a healthy, evidence-less target gets RETIRED — CT-7 reds). An agent thus still cannot
autonomously destroy a healthy, unflagged memory (Law 7 guard kept).

### 3.3 Tool surface v3 (8 verbs, same names)

- `hive_write {text, polarity?, kind?, anchors?: [{repo, anchor}], repos?: [string], meta?,
  replaces?: int}` → `{status: "approved"|"redacted"|"refused", id, trust: "provisional", …}`.
  No `approved_by`. `replaces`: an unknown target still fails the whole call (nothing stored —
  existing behavior); a known target is retired IFF the §3.2 gate qualifies, else the write lands
  and the retirement rider no-ops (envelope reports `supersede_noop`).
- `hive_capture {text, polarity?, kind?, anchors?, repos?, meta?}` → quarantined (unchanged flow).
- `hive_recall {query, repos?: [string], anchor_prefix?: string}` — each `repos` entry is
  `name` or `name@branch` (`@branch` names the tip drift is judged against for that repo; default
  = the repo's canonical ref). Omitted `repos` ⇒ **global** search. Served set = memories whose
  repo-set intersects the scope, ∪ general (`repos=[]`), full-K within that partition;
  `anchor_prefix` additionally keeps only episodes with ≥1 anchor whose path starts with the
  prefix (general memories are kept — prefix narrows the anchored population only).
- `hive_supersede {loser, winner}` / `hive_prune {episode_id}` — machine-gated (§3.2), no approver.
- `hive_outcome`, `hive_flag` — unchanged shape (flag stays advisory-only, never qualifying).
- `hive_health` — loses `include_onboarding`; `include_census_health` becomes per-repo blocks.

Anchors/repos are validated by a boundary gate `normalize_anchors(...)` (the `normalize_meta`
idiom): repo must be a registered name, anchor free text; a violation is a clean `refused`
envelope. `_validate_args` gains array-item object checking for `anchors`.

### 3.4 Served hit shape

```
{ episode_id, text, sim, trust: provisional|established, ts, polarity, kind, meta?,
  repos: ["A"] | ["A","B"] | [],            # [] = general
  anchors: [{repo, anchor}],                # [] for general / scope-only
  drift: { type: fresh | anchor_changed | anchor_missing | blast_radius_changed
                | branch_scoped | unverifiable | n/a,
           detail: {per_anchor: [{repo, anchor, verdict, tip_sha?}], ref?} } }
```

Aggregation across anchors (most-severe wins; fail-safe — a partially-unverifiable memory can
never read `fresh`): `anchor_missing > anchor_changed > blast_radius_changed > branch_scoped >
unverifiable > fresh`; general ⇒ `n/a`. Verdict source: the `anchor_drift` cache at the tip of the
queried ref (from `name@branch`) else the repo's canonical ref; cache miss ⇒ `unverifiable`.
`hive-edge verify` → wire mapping (single owner, `hive/app/drift.py`): `current→fresh`,
`stale/signature_changed→anchor_changed`, `stale/symbol_missing→anchor_missing`,
`radius changed→blast_radius_changed`, `branch_scoped→branch_scoped`, else `unverifiable`.
Drift attachment is a fail-open enrichment: a reader fault degrades that hit to `unverifiable`,
never breaks the read. `last_verified`/`remediation` riders are kept (remediation text rewritten
for the agent-adjudicated flow).

### 3.5 Schema v3 (clean start; migration refused as today)

```sql
-- episodes: DROP anchor, provenance, approved_by, approved_ts, tags (dead). Rest unchanged.
CREATE TABLE episode_anchors(              -- scope + code binding, one owner
  episode_id INTEGER NOT NULL, repo TEXT NOT NULL,
  anchor TEXT NOT NULL DEFAULT '',         -- '' = scope-only membership (repo w/o code anchor)
  fp_meta TEXT NOT NULL DEFAULT '',        -- per-anchor fingerprint tokens (meta-envelope law)
  PRIMARY KEY(episode_id, repo, anchor));
CREATE TABLE repos(                        -- the operational registry (authctl-seat pattern)
  name TEXT PRIMARY KEY,                   -- [a-z0-9._-]+ (slug-checked; used in paths/scopes)
  url TEXT NOT NULL, canonical_ref TEXT NOT NULL DEFAULT '',
  token_env TEXT NOT NULL DEFAULT '',      -- NAME of the env var holding the git token ('' = HIVE_SYNC__TOKEN)
  added_ts INTEGER NOT NULL);
CREATE TABLE anchor_drift(                 -- materialized drift verdicts (rebuildable cache, Law 5)
  repo TEXT NOT NULL, tip_sha TEXT NOT NULL, anchor TEXT NOT NULL,
  verdict TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '{}', ts INTEGER NOT NULL,
  PRIMARY KEY(repo, tip_sha, anchor));
CREATE TABLE ref_requests(                 -- recall-touched refs wanting materialization
  repo TEXT NOT NULL, ref TEXT NOT NULL, last_requested_ts INTEGER NOT NULL,
  PRIMARY KEY(repo, ref));
-- recall_misses: ADD COLUMN repos TEXT NOT NULL DEFAULT ''   -- JSON array; '' = global miss
```

Sync meta keys become per-repo: `sync:<name>:last_tip`, `sync:<name>:last_error`, etc.
`ingested_ranges`, `evidence_events`, `conflict_flags`, `exposure`, `blobs`, `meta` unchanged.

### 3.6 Repo-scoped demand & outcome

- A recorded miss carries the query's repo scope. Demand matching adds a scope clause before the
  cosine clause: miss scope S matches candidate scope R iff `S==[] or R==[] or S∩R≠∅`.
- The competitor veto is partition-scoped: the top servable sim among partition-compatible rows.
- Census join: receipt subjects join only `episode_anchors` rows of THAT repo (exact `repo` match
  on the registry name recorded at ingest), replacing the free-text `match_anchors` path gate with
  path equality + the existing symbol tier (anchors are now structured, so the §3.4 boundary-scan
  heuristics collapse to exact comparison — simpler and stricter).
- Verified riders (`outcome_verified_*`, `verify_*`, `stale_suspect`): the gate moves from
  `phase == "pre_merge"` to `version_stamp parses` (any phase) — the canonical `post_merge` ingest
  now writes them. The §6.2.5 canary rule is UNTOUCHED for the `change_outcome` tag (post-merge
  machine-checked still requires a canary signal); the sync ledger leg derives the post-merge
  verdict from decided execution lines when any exist (capped at `bounded-estimate`), else
  `pass` (landed-line parity). The PR candidate leg, `verify_candidates`, `sync:pr_heads`, the
  PR refspec, and env provisioning are DELETED.

---

## 4. Intents → contracts → tests (traceability; suite frozen at `tests/contract/`)

Entry points: contract tests drive the REAL surfaces — `HiveMCPServer.handle` (the one surface
both stdio and HTTP share) assembled by `build_container` over a temp SQLite store with the
deterministic fake embedder (existing convention), and `SyncService.tick` with the injectable
`Run` seam (existing `tests/sync` convention) plus real-git mirror fixtures where the scenario is
git behavior itself. `CT-x` names below are the frozen test modules.

**Edge-case bar (intent author's explicit requirement):** every CT module walks its FULL scenario
matrix — boundary, failure, and refusal paths, not just the nominal case. A contract test that
covers only the happy path does not satisfy its intent. The `replaces=` gate-fails vs gate-passes
branches are mandatory scenarios, not optional depth.

| # | Intent | Contract (given / when / then) | Scenarios covered | Contract test |
|---|--------|-------------------------------|-------------------|---------------|
| 1 | Write serves now as provisional | Given a connected agent, when `hive_write{text}` (no approver field exists), then the row is servable immediately with `trust=provisional` and a subsequent matching recall serves it labeled provisional | plain write; write w/ anchors in 1 and 2 repos; general write; dedup onto quarantined (lifts to provisional + audit); dedup onto provisional/established (idempotent); dedup onto deprecated (no revive); secret refuse (0 rows); redact; `replaces=` qualified target ⇒ stored + target retired; `replaces=` evidence-less target ⇒ stored + target UNTOUCHED + `supersede_noop` envelope; `replaces=` unknown target ⇒ whole call fails, nothing stored; unknown repo name in anchors/repos ⇒ clean refusal, nothing stored | `CT-1 test_write_provisional.py` |
| 2 | Capture quarantines then demand-promotes within its partition | Given a capture scoped to repo A, when `demand_m` scope-matching misses from ≥1 other identity arrive and no partition competitor answers, then it promotes to provisional; misses scoped to repo B never count | promote in-scope; B-scoped misses don't promote; global misses count for any scope; general candidate promoted by any miss; competitor in another repo does NOT veto; competitor in-partition vetoes; self-demand refused | `CT-2 test_demand_repo_scoped.py` |
| 3 | Recall partitions by repo ∪ general, full-K, gate-preserved | Given memories in repos A, B, and general, when recalling with `repos:["A"]`, then only A ∪ general candidates enter the gate and hits; omitted repos ⇒ global; unknown repo name ⇒ clean refusal; `anchor_prefix` narrows anchored hits | scope hit; cross-repo exclusion; general always in; global default; multi-repo memory served from either scope; abstain unchanged when scoped field is weak (τ preserved); anchor_prefix filter; empty partition ⇒ abstain/empty | `CT-3 test_recall_partition.py` |
| 4 | Every hit carries repos/anchors/drift | Given served hits, when recalled, then each hit carries `repos`, `anchors`, and a `drift` object; general ⇒ `n/a`; unmaterialized tip ⇒ `unverifiable`; a drift-reader fault degrades to `unverifiable` without breaking the read | fresh; anchor_changed; anchor_missing; blast_radius_changed; branch_scoped (off-set tag); unverifiable (no cache row); n/a (general); multi-anchor most-severe aggregation; `name@branch` ref routing | `CT-4 test_hit_drift.py` |
| 5 | Server mints eagerly; backfill catches up | Given a write with an anchor in repo A, when the next sync tick runs, then the absent fp keys are minted via the real mint seam against A's canonical tip into `episode_anchors.fp_meta` (absent-only; present keys never displaced), capped + carried over | fresh mint; absent-only merge; unresolvable anchor ⇒ silent skip; cap carry-over; per-repo baselining; provenance stamp | `CT-5 test_server_mint.py` |
| 6 | Drift is materialized per (repo, tip) and version-gated | Given stored fps and a moved tip, when the materializer runs, then `anchor_drift` rows appear for the canonical tip and any requested refs; an incomparable token version yields silence/`unverifiable`, NEVER false-stale (BUG-037) | verdict rows per class; version-gate silence; requested-ref materialization via `ref_requests`; tip move invalidates naturally; cache is rebuildable (wipe → repopulated) | `CT-6 test_drift_materializer.py` |
| 7 | Retirement requires a qualifying machine signal; unqualified = benign no-op | Given a healthy, evidence-less servable memory, when any agent calls `hive_prune`/`hive_supersede` on it (or `hive_write(replaces=)` names it), then NOTHING is retired — prune/supersede return the `noop` envelope (never `isError`), and the write lands with the rider no-opped; given a qualifying signal, the same call retires the target and the audit stamps exactly which signal(s) qualified | noop-on-healthy (all 3 verbs, envelopes asserted); drift-stale qualifies; verify_stale ledger row qualifies (and a NEWER verify_current disqualifies it); outcome_verified_hurt qualifies; outcome_hurt other-identity qualifies; outcome_hurt SAME identity does NOT (two-call self-destruction blocked); contradiction qualifies; supersede near-dup winner qualifies; hive_flag does NOT; general memory: outcome/contradiction only, drift `n/a`; deprecated/unknown target ⇒ existing noop/failure shapes preserved; gate-feed reader fault ⇒ fail-closed noop (never a retire) | `CT-7 test_retirement_gate.py` |
| 8 | Established = canonical-line outcome verification | Given a provisional memory whose anchor gains a SHA-bound `outcome_verified_helped` row from a canonical-line ingest, when the post-ingest promotion sweep runs, then trust becomes `established` with an audit row; self-reported `outcome_helped` alone never establishes | promote on verified win; no promotion on self-reported help; quarantined w/ win goes via provisional first; established never decays | `CT-8 test_established_rung.py` |
| 9 | N-repo sync feeds per-repo canonical evidence | Given two registered repos, when their canonical lines move, then each gets its own mirror, watermark, receipt build, and ingest (rows repo-keyed); one repo's fault never blocks the other; riders land from the canonical post_merge ingest under a full stamp; the PR-candidate machinery is GONE | two-repo tick isolation; per-repo watermark; force-push discontinuity per repo; rider rows post_merge; canary rule preserved on tags; empty registry ⇒ inert tick (no git, no engine import) | `CT-9 test_multirepo_sync.py` |
| 10 | Repo registry verbs | Given a running server, when `hive repo add <url> [--name --branch --token-env]` runs, then the row lands (slug-validated, no secret stored) and the next tick mirrors it; `remove` stops feeding and prunes the mirror; `repos` lists | add/list/remove; duplicate name refused; bad slug refused; token_env indirection honored; unset token_env var ⇒ named fail-open error surfaced in health, and boot fail-fast EX_CONFIG when set-but-missing | `CT-10 test_repo_registry.py` |
| 11 | Onboarding/contract apparatus is gone | Given a connected client, when `initialize` returns, then trimmed instructions ≤ `METADATA_FIELD_LIMIT` describe the v3 contract; tool results carry NO `contract_version`; `include_onboarding` is not a valid flag; no AGI/approver field anywhere in schemas | initialize fits cap (BUG-023 guard); no beacon key; schema sweep (no approved_by, no AGI mention); tools/list == enforced belt | `CT-11 test_served_contract.py` |
| 12 | AGI/vouch deletion is total | Given the built server, when grepping the runtime surface, then `AGI_OVERRIDE`/`HIVE_AGI__MODE`/`approved_by` are unreachable: env `HIVE_AGI__MODE=true` is an unknown-group WARN, tool args with `approved_by` are ignored-extra, provenance column absent | env ignored; extra-arg ignored; DDL sweep; purity gate still green | `CT-12 test_deletion_total.py` |
| 13 | Health worklists in the new shape | Given the maintenance flags, when `hive_health(include_*)` runs, then census_health is per-repo, conflicts bucket by (repo, anchor), suspect-consensus/stale-suspects/gaps/trends still serve; miss scope rides the gap report | each flag; per-repo census block; empty registry block; fail-open [] on probe fault | `CT-13 test_health_v3.py` |
| 14 | Legacy store refused | Given a v2 store file (with `anchor`/`provenance` columns), when the server boots, then it fails fast EX_SOFTWARE with the clear no-migration message (never serves mixed-schema) | v2 file refused; fresh store boots; restore of a v2 backup refused with the same message | `CT-14 test_schema_v3_boot.py` |

Unit-layer (component) tests accompany every changed module (§6 lists them per step). TDD order:
CT suite authored FIRST after plan approval, observed RED against the un-built system, then
frozen (`frozen_paths: tests/contract/**`); implementers never edit it (defect fixes = ESCALATE).

Property-based coverage (Hypothesis, already-in-idiom candidates): scope-matching algebra of §3.6
(`S∩R` clause — generated scope sets; invariants: global-matches-all, general-matched-by-all,
symmetry) in CT-2/CT-3's property module; drift aggregation total order (any verdict multiset →
exactly one aggregate; `fresh` only when all fresh) in CT-4.

---

## 5. External-interaction inventory (boundary → declared failure behavior → failure test)

| Boundary | Failure behavior | Test |
|---|---|---|
| git subprocess (per-repo mirror: clone/fetch/worktree/rev-parse) | fail-OPEN per repo per tick: logged + `sync:<name>:last_error`; other repos unaffected; serve path never touched; `clean_git_env` on every spawn (BUG-034) | CT-9 (fault isolation), sync units |
| `python -m hive.census.cli build` subprocess | fail-OPEN: leg skip + last_error; receipt refused ⇒ counted, never a crash | CT-9 |
| `hive-edge mint` subprocess | `{}`/nonzero/unparseable ⇒ silent skip, carry over (existing `_mint` contract) | CT-5 |
| `hive-edge verify` subprocess | nonzero/unparseable ⇒ verdict `unverifiable` (fail-safe), never false-stale/fresh | CT-6 |
| SQLite store (shared conn, one lock) | unchanged single-writer discipline; lock held only for store access, never across subprocess | CT-9 lock-scope assertion (existing sync-test idiom) |
| drift-cache read at recall | fail-OPEN enrichment: hit degrades to `unverifiable`; read never breaks | CT-4 |
| env: `HIVE_STORE__DB_PATH` | unchanged (ephemeral WARN + health key) | existing suite |
| env: per-repo `token_env` vars (`HIVE_SYNC__TOKEN` default) | registry row set but var ABSENT at boot ⇒ EX_CONFIG naming the var (fail-fast, no silent default); absent at tick (added post-boot) ⇒ that repo's leg fails open with a named error surfaced via health | CT-10 secrets fail-fast probe |
| env: deleted vars (`HIVE_AGI__MODE`, `HIVE_SYNC__REPO_URL/VERIFY_CANDIDATES`, `HIVE_CENSUS__CANONICAL_REF`) | unknown group/field WARN, ignored (existing `_apply_env` behavior) | CT-12 |
| webhook `POST /census-webhook` | unchanged (one nudge wakes the loop for all repos) | existing `tests/sync/test_contract_webhook.py` updated |
| MCP HTTP doors / auth / identity / rate limit | unchanged | existing suites |

---

## 6. Implementation plan (numbered; order is dependency-safe)

Every step names files + exact signatures; an implementer needs no re-derivation. "MM" = named
mutation marker + the test that reds (Law 7).

**Step 0 — contract suite.** Author `tests/contract/` (CT-1…CT-14) + `tests/contract/conftest.py`
(container fixture w/ fake embedder, sync fixture w/ scripted `Run`, real-git repo factory reused
from `tests/sync/conftest.py`). Observe RED. Freeze.

**Step 1 — domain vocabulary & carriers** (`hive/domain/`)
- `models.py`: `Episode` — drop `anchor`, `provenance`, `approved_by`, `approved_ts`, `tags`;
  add `repos: tuple[str, ...] = ()` and `anchors: tuple[AnchorRef, ...] = ()` (new frozen
  `AnchorRef(repo: str, anchor: str, fp_meta: str = "")`); invariants: `repos == sorted set of
  anchor repos ∪ declared scope`, servable-trust ⇒ approved (kept). `RecallHit` — drop
  `provenance`, `anchor`; add `repos`, `anchors`.
- delete `hive/domain/agi.py`, `hive/domain/provenance.py`.
- `lifecycle.py`: `MissRow` gains `repos: tuple[str, ...] = ()`; `DemandRule.decide(...,
  candidate_repos)` adds the scope clause (§3.6) before the cosine clause (MM: dropping the scope
  clause reds CT-2 cross-repo test); `LifecycleService` gains
  `promote_established(self) -> list[int]` (provisional ∩ `verified_wins` → `set_trust(ESTABLISHED)`
  + audit `{rule:"verified_established"}`; MM: promoting on `settled_wins` instead reds CT-8
  self-report test); `_competitor_top_sim(vec, scope)` becomes partition-aware (walk desc, first
  partition-compatible eid).
- new `hive/domain/retirement.py`: `retirement_evidence(*, episode, caller_identity, drift_verdicts,
  evidence_rows, conflict_pairs, winner_cosine=None) -> Eligibility` (frozen
  `Eligibility(eligible: bool, signals: tuple[str, ...])`), pure/total/fail-closed
  (undecidable ⇒ ineligible). MM: deleting the handler's gate call reds CT-7
  noop-on-healthy (a healthy, evidence-less target gets retired = the mutation).
- `admission.py`: drop `is_agi_override` import + provenance derivation; `write(...)` loses
  `approved_by`, lands `trust=PROVISIONAL` (via `complete`), dedup-onto-quarantined LIFTS to
  provisional (+ audit, MM: silent lift reds CT-1 audit assertion); `capture()` unchanged flow;
  both gain `anchors: Sequence[AnchorRef]` + `repos: Sequence[str]` threaded to `stage`.
- `change_evidence.py`: rider gate `stamp is not None` replaces the `phase == "pre_merge"`
  condition (MM: restoring the phase condition reds CT-9 rider test); `derive_post_merge(...)`
  helper (decided lines ⇒ verdict, tag capped ≤ bounded-estimate; nothing decided ⇒ caller
  verdict); join consumes `(episode_id, repo, anchor, polarity)` and filters `repo == receipt repo`
  with exact-path + symbol-tier matching (replaces the free-text span scan; MM: dropping the repo
  filter reds CT-9 cross-repo join test).

**Step 2 — store schema v3** (`hive/adapters/store_sqlite.py`)
- `_SCHEMA` v3 per §3.5; boot refusal now triggers on PRESENCE of legacy columns
  (`anchor`/`provenance` in `episodes`) or absence of `episode_anchors` (CT-14).
- `stage(..., anchors: Sequence[tuple[str, str]], repos: Sequence[str])` writes `episode_anchors`
  rows in the same tx; drop `provenance`/`tags` params. `complete`/`approve` collapse: `approve`
  deleted; `complete(..., trust)` no longer takes `approver/approved_ts`.
- new reads/writes: `servable_scopes(*, now, provisional_ttl_s) -> dict[int, tuple[frozenset[str],
  tuple[tuple[str, str], ...]]]` (RepoScopeReader); `anchors_lacking_fp(repo) -> list[(eid, anchor)]`;
  `fill_anchor_fp(eid, repo, anchor, additions) -> int` (absent-only, spread-order MM kept from
  `fill_absent_meta`); `drift_get(repo, tip, anchors) -> dict[anchor, (verdict, detail)]` /
  `drift_put(rows)` / `drift_prune(repo, keep_tips)`; `repo_registry() -> list[RepoRow]` /
  `repo_add/repo_remove`; `touch_ref_request(repo, ref, ts)` / `requested_refs(repo, since_ts)`;
  `evidence_rows_for(episode_id, kinds) -> …` (gate feed); `anchored_episodes()` returns
  `(eid, repo, anchor, polarity)`; misses read/write carry `repos` JSON.
- `record_miss(..., repos_json: str)`; `misses_window` returns scope-carrying `MissRow`s.

**Step 3 — recall pipeline** (`hive/domain/recall.py`)
- `RecallPipeline.recall(query, *, agent_id, scope: RecallScope | None)` — new frozen
  `RecallScope(repos: tuple[tuple[str, str], ...], anchor_prefix: str)` (repo, ref) pairs, ref ""
  = canonical. Pipeline order: encode → search FULL index → **scope-filter candidates** via
  `scopes = reader.servable_scopes(...)` (keep eid iff global, or repos∩scope≠∅, or repos==∅;
  then anchor_prefix clause) → gate on the FILTERED sims (MM: gating on unfiltered sims reds
  CT-3 τ-preservation test) → resolve/select/surface unchanged → miss recorded WITH scope.
- port `RepoScopeReader` added to `ports.py` (separate narrow protocol, conformance-tested).

**Step 4 — boundary** (`hive/app/`)
- `onboard_ref.py` → renamed `hive/app/contract.py`: keeps `METADATA_FIELD_LIMIT`, a rewritten
  `SERVER_INSTRUCTIONS` (the trimmed v3 usage contract: recall-first, scoped store/recall,
  capture-vs-write, outcome, machine-gated retirement; proven under the cap — CT-11),
  `REMEDIATION_NOTICE` (rewritten), `WRITE_VS_CAPTURE`, `BAD_VS_STALE`. DELETED: everything else
  (CONTRACT_VERSION, keystone/bundle, rules block, hooks, allowlist, procedure, edge CLI refs,
  MIN_EDGE_VERSION, identity/kind payload rendering, `render_onboarding_payload`).
- `tool_defs.py`: schemas per §3.3 (`_ANCHORS_PROPERTY` shared by write/capture); descriptions
  rewritten lean; every description asserted < cap (existing `test_contract_fit` extended).
- `mcp_server.py`: `_tool_result` beacon line deleted (with its test); `_override_refused`/
  `_agi_refused` deleted; `_handle_write`/`_handle_capture` accept anchors/repos via
  `normalize_anchors` (new, in `hive/domain/meta.py`-adjacent module `hive/app/anchors.py` —
  boundary grammar gate; registered-repo check against the registry); `_handle_recall` builds
  `RecallScope`, attaches `repos`/`anchors`/`drift` per hit via `hive/app/drift.py:attach_drift`
  (fail-open), records `ref_requests` touches; `_handle_prune`/`_handle_supersede` call the §3.2
  gate feed + `retirement_evidence` before any store mutation, audit stamps `signals`;
  `_handle_health` v3 (per-repo census blocks; flag removals).
- new `hive/app/drift.py`: the verify→wire verdict mapping + most-severe aggregation + cache
  lookup (single owner of both tables' semantics).
- `config.py`: delete `AgiConfig`, `CensusConfig`; `SyncConfig` → `{interval_s, webhook_secret,
  mirror_dir}` (repo_url/token/verify_candidates gone; partial-config guard reworded to
  webhook_secret-only? — webhook_secret is now standalone-valid (nudge is global), so the guard
  is deleted); `Config` group table updated.

**Step 5 — sync** (`hive/app/sync.py`)
- `SyncService` becomes the N-repo loop: `tick()` reads the registry; per repo (own fail-open
  guard): `ensure_mirror(repo)` → `mirrors/<name>/` (slug path), fetch ALL branches
  (`+refs/heads/*:refs/remotes/origin/*`), ledger leg per repo (watermark `sync:<name>:last_tip`,
  receipt `--repo-id <registry name>`, post_merge ingest w/ derived verdict per §3.6), mint
  backfill per repo (`anchors_lacking_fp`), drift materializer per repo (canonical tip + refs from
  `ref_requests` within 7d; worktree at tip → `hive-edge verify` per anchor with stored fps;
  capped `_DRIFT_PER_TICK = 50`, carried over; `drift_prune` old tips). Candidate leg + PR
  machinery deleted. `start_sync` always starts the thread; empty registry ⇒ inert tick (no git,
  no engine import — MM: cloning with empty registry reds CT-9 inert test).
- token resolution: `os.environ[row.token_env or "HIVE_SYNC__TOKEN"]`; absent ⇒ per-repo
  fail-open error. Entrypoint boot check: every registered `token_env` var present or EX_CONFIG.

**Step 6 — container / entrypoint / CLI / tools**
- `container.py`: wire `retirement` gate feed args, scope reader, drift attach; drop
  agi/canonical_ref/flag wiring changes; `_REQUIRED_TABLES` += `episode_anchors, repos,
  anchor_drift, ref_requests`.
- `entrypoint.py`: sync start unconditional (post serve-ready); token_env boot probe (EX_CONFIG).
- new `hive/tools/repoctl.py` (authctl twin: argparse `add/remove/list`, zero-config db_path fix
  from BUG-013 honored, slug validation, no secret bytes stored/printed); `hive/tools/cli.py`:
  verbs `repo add|remove` + `repos` (docker-exec shell-through like token verbs; BUG-020-style
  quoting discipline), `connect` breadcrumb rewritten (no edge install, no onboarding fetch).
- `healthcheck.py` untouched.

**Step 7 — deletions sweep** (grep-verified, suite green after)
`hive/domain/agi.py`, `hive/domain/provenance.py`, `scripts/contract_version_guard.py` (+ its
tests + the `.githooks/pre-commit` invocation), `tests/app/test_contract_conservation.py` +
`tests/app/fixtures/contract_corpus_v06.py`, keystone golden fixtures, `test_onboard_ref.py`
(replaced by CT-11 + a small `test_contract.py` for the trimmed module), AGI/approver tests
throughout, `tests/sync/test_contract_candidate.py`, candidate-leg code, `hive_health`
onboarding flag docs. Law-7 audit: every deleted guard's mutation marker either dies with its
code or is re-homed (list kept in the step's checklist).

**Step 8 — check gate (create-on-first-touch) + docs**
- `Makefile` with `check`: `ruff format --check .` + `ruff check .` + `mypy hive/ --strict`
  (pragmatic per-module overrides ONLY for missing third-party stubs, each with a comment) +
  `pytest` (full suite). New dev deps in `[project.optional-dependencies].dev`. Fix violations.
- Docs sweep in the same change: `README.md`, `HIVE-ADMIN.md`, `OPERATIONS.md`, `llms.txt`,
  `llms-full.txt`, `skills/*` (connect-repo/connect-team lose the onboarding/edge-install story;
  operate gains `hive repo` verbs), `CHANGELOG.md`, `CONTEXT/THEORY.md` (§10 below),
  `CONTEXT/INTERACTIONS.md` (rewrite: S-entries collapse, C-entries per-repo, T/M entries new
  lifecycle + gate), `CONTEXT/BUGS.md` untouched (history).
- `graphify update .` after code lands.

**Step 9 — verification.** Full suite + `make check` green; **/verify** (runtime surface): boot
the compose stack with 2 registered scratch repos, drive the real MCP flow over the loopback door
(write scoped → recall scoped → break an anchor on repo A's canonical line → observe drift +
retirement-gate accept/refuse → verified-win establish), confirm per-repo census rows; cold-start
check (fresh clone → README bootstrap → `make check`) since deps/bootstrap changed.

Order rationale: domain first (pure, fake-testable), store second (schema the domain rides),
pipeline/boundary third (consume both), sync/CLI fourth (drive the store), deletions after
replacements exist (nothing references them), gate+docs last (whole-tree facts).

---

## 7. Deletion inventory (what stops existing)

Agent-side: install procedure, HIVEMIND-RULES block + marker, claude hooks + allowlist,
`hive-edge` as an agent tool, re-onboard loop, contract-version beacon.
Server-side: `hive_init`-era onboarding payload, CONTRACT_VERSION + keystone hash + guard,
AGI_MODE (config/env/sentinel/refusals), human vouch (`approved_by` args + columns), provenance
taxonomy, PR-candidate verification leg, `HIVE_CENSUS__CANONICAL_REF` (per-repo registry column
replaces it), `HIVE_SYNC__REPO_URL`/`VERIFY_CANDIDATES`, `episodes.anchor/tags` columns.
BUG-043/046/047 family: moot (their subsystem is deleted).

---

## 8. What deliberately does NOT change

Abstain gate + τ_serve; select_served; conflict detection defaults; secret floor; two-door
auth/identity/rate-limit/webhook; single-writer lock discipline; exposure/miss side-channel
fail-open direction; index authority; census receipt format + range ledger + canary rule;
`hive_outcome`/`hive_flag` shapes; backup/restore/reset verbs; hermetic image + vendored wheels
(still three); demand rung's identity-diversity clause (now also guarding retirement).

---

## 9. Operational consequences (state plainly at rollout)

- **Schema v3 = clean store, and the corpus is disposable** (confirmed by the intent author:
  none of the stored information is needed). Rollout is simply `hive reset` → register repos.
  No export, no re-seed, no migration tooling of any kind is built.
- Consuming repos must strip the now-dead HIVEMIND-RULES block + hivemind hooks + allowlist from
  their rules files / `.claude/settings.json` (one-time; the block's re-onboard loop is dead —
  documented in CHANGELOG + README upgrade note).
- The served MCP tool descriptions/instructions change; sessions pick them up on reconnect.

## 10. THEORY.md revisions the plan owns (docs step)

Law 3 (“vouched by a human” clause → outcome-verified top rung; identity-diversity clause now
also gates hurt-based retirement); §3 lifecycle diagram + “retirement has two conscious owners”
(now machine-gated agent calls); §5 onboarding paragraph → trimmed served-instructions posture;
AGI paragraphs (§3/§5/§9.9/§10) deleted; §10 O7 boundary restated (“automatic resolution refused;
agent resolution requires machine evidence”); §2 map (edge CLI row → server engines; census
per-repo); §9 checklist item 6 rewritten mechanically-or-machine-gated.

## 11. Risks (stated, mitigated, accepted)

1. **No human short-circuit** (the intent's own headline risk): a wrong provisional serves until
   flagged + retired; a bad supersede installs a provisional winner until outcome catches it.
   Mitigations already in-plan: the evidence gate (§3.2), identity-diverse hurt clause, labels on
   every hit, TTL decay, worklists. Accepted by design.
2. **Drift-cache lag**: a verdict is only as fresh as the last materializer pass; bounded by
   `interval_s` + caps; un-materialized reads fail-safe (`unverifiable`). Accepted.
3. **mypy-strict adoption** (Step 8) may surface broad annotation debt; bounded by per-module
   stub overrides; runs last so it can't destabilize the build.
4. **Established-rung fuel is polarity-narrow** (`classify_verified` reaches only do/dont
   memories with test-reach); neutral memories cap at provisional until the classifier widens
   (explicitly out of scope here).

## 12. Handoff

Size: well over 300k context — **/daisy-chain-build**, with `frozen_paths: ["tests/contract/**"]`.
Definition of done: CT suite green (authored red-first) + unit sets green + `make check` green +
/verify pass + cold-start pass. After implementation, move this file to `docs/PLANS/IMPLEMENTED/`.
