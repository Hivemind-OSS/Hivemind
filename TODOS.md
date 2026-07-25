# TODOs

Deferred work — not yet built — that still fills a real gap in the project. Each entry names the files it touches and the mutations that would prove it.

---

## TODO 7 — Qwen3 asymmetric query-instruction (deferred from the embedding swap)

**File:** `hive/adapters/embedding/local_st.py`, `hive/domain/ports.py`, `hive/domain/recall.py`, `hive/domain/admission.py`, `tests/fakes/_fakes.py`

Once the embedding model is `Qwen/Qwen3-Embedding-0.6B`, recall quality can be lifted by using the
model's intended **asymmetric** encoding: a document is embedded as raw text; a query is embedded
with an instruction prefix (`"Instruct: <task>\nQuery: <text>"`). The shipped `EmbeddingProvider.encode`
is a single symmetric chain used by both capture and recall, so this is deferred — it is NOT a free
swap:

- It **widens the port** (a query path distinct from the document path) and must update every adapter
  + the fake + both call sites (`recall.py` query side, `admission.py` document side), each conformance-
  tested (a port change that goes green on fakes alone is the smell).
- It **breaks "same text → same vector"**, which the trust lifecycle depends on: the recall miss-query
  vector is matched against candidate/servable *document* vectors by cosine (`demand_tau` = miss↔candidate,
  `competitor_tau` = candidate↔servable). Enabling asymmetry therefore requires **recalibrating
  `autonomy.demand_tau`/`competitor_tau`** (and re-checking the recall gate) on the asymmetric geometry —
  not just ranking.
- Land it OFF by default and byte-inert when off (a fixed/empty prompt ⇒ identical to symmetric); flip
  on only after the τ recalibration is measured.

**Mutations to verify:** with asymmetry ON, the same text as query vs document must differ; with it OFF
(default), query and document encodings must be byte-identical (the byte-inert guarantee); the
recalibrated `demand_tau` must still promote a genuine cross-identity demand and still withhold a
self-demanded one.

---

## TODO 8 — Non-Claude-Code permission auto-approve recipes (per-IDE allowlist)

**File:** `hive/app/onboard_ref.py` (`ONBOARDING_PROCEDURE`, `render_onboarding_payload`),
`tests/app/test_onboard_ref.py`, `tests/app/test_contract_fit.py`

Deferred from the contract-versioning work (resolved Claude-Code-first). The
contract-versioning feature serves an auto-approve step so the non-human-in-the-loop verbs
(`recall`/`capture`/`health`/`outcome`/`flag` — the schema-derived set whose `required[]` lacks
`approved_by`) don't prompt per call. But the *mechanism* is IDE-specific: v1 ships only Claude
Code's form (`.claude/settings.json` `permissions.allow`, `mcp__hive__*`). Other runtimes have
different (or no) permission-allowlist formats.

Extend `ONBOARDING_PROCEDURE` (served via `render_onboarding_payload()`'s `procedure` key) with a
per-runtime permission recipe **alongside** the existing per-runtime rules-file table (Cursor /
Windsurf / Cline / generic), each writing to the **project** scope (never global) and each keeping
the human-gated 3 (`write`/`supersede`/`prune`) prompted.

**Mutations / verification:** each IDE's permission schema is real-runtime-only (the BUG-007/011/012
class — an offline suite can't see whether the written allowlist actually suppresses that IDE's
prompt). Verify each recipe against the live IDE before serving it; a runtime with no allowlist
mechanism gets the rules block but keeps prompting (degrade-safe).

---

## TODO 9 — Full AGI-mode client-side contract + switching mechanism

**File:** `hive/app/onboard_ref.py`, `hive/app/mcp_server.py` (beacon), a new claude-code `SessionStart`
hook, `tests/app/test_onboard_ref.py`

Deferred from the contract-versioning work (open question #4, AGI descoped). The **domain**
half of AGI mode already exists (AGI-MODE landed: `HIVE_AGI__MODE` makes the boundary honor
`approved_by="AGI_OVERRIDE"`). The missing half is the **client-side** prompt-bypass + the switching
that propagates an off↔on flip into the installed contract (rules + allowlist + possibly hooks).

The hard constraint (why it is its own feature): AGI_MODE is a *server-side vouch* switch; the
*client-side* permission prompt lives on the client, and **a remote server cannot — and must not —
reach a client's `permissions.allow`** (a remote flag silently switching off local human control is a
footgun). So propagation is **local convergence on a server signal**, never server→client reach, and
the actuator must be an **operator-enabled** hook (conscious client-side opt-in).

Mechanism sketch (build on the contract-versioning beacon substrate):
- Beacon carries `agi_mode` as a **second axis** orthogonal to `contract_version`; the installed
  marker records the `(version, posture)` pair, so an off→on flip (text unchanged) reads as drift.
- Re-onboard on posture drift rewrites **only** the project allowlist 5↔8 verbs (under AGI-on the
  privileged 3 self-authorize, so the prompt is the only thing left blocking autonomy) and restamps
  the marker. The rules-block text is unchanged (it already states the AGI_OVERRIDE conditional).
- Needs the **hook-enforced** enforcement variant (the teeth) — model-driven re-onboard is too weak
  for an autonomy promise. Likely depends on **TODO 8** (per-IDE permission recipes), since the
  bypass is also per-IDE.

**Honest limits to design around (carried from the plan's AGI discussion):**
- Behavioral unless hook-enforced; absent teeth it degrades to *more* prompting, never a silent
  unauthorized write (the domain gate is the real authority — Law 6).
- One-session latency: `permissions.allow` is read at session start, so a mid-session rewrite applies
  next session. Operator fast-path: write the 8-verb allowlist at config-flip time, with
  beacon/re-onboard as the convergence backstop.

**Mutations to verify:** with AGI on, the beacon's `agi_mode` flips and an `agi=off`-marked install
reads as drift → re-onboard; with AGI off, the allowlist contracts to 5 and the privileged 3
re-prompt + require a human `approved_by`; the server never writes a client permission file (the
actuator is always local).

---

## TODO 10 — Continuous integration (run the suite on every push / PR)

**Where:** a new `.github/workflows/ci.yml` (plus a CI status badge in `README.md`).

Deferred only because GitHub Actions is not available on the current private-org plan — add this
once the repo is public or Actions is enabled. Today the test suite runs only on a contributor's
machine, so a regression or a broken build can land unnoticed and a fresh clone has no signal that
the default branch is green. This is the last standard open-source convention still missing.

Add a workflow that, on `push` and `pull_request`:
1. Sets up Python 3.11 and 3.12 (matrix).
2. Installs the package: `pip install -e ".[dev]"`.
3. Runs the fast tier: `pytest -m "not embed"` — the default, no model download.
4. Builds and validates the wheel (`python -m build` + `twine check dist/*`) so packaging
   metadata (`license` / `license-files` / `readme`) cannot silently regress.
5. Runs the `embed` tier (`pip install -e ".[embed,dev]"` then `pytest -m embed`) on a **schedule
   or manual dispatch only** — it pulls the ~1.2 GB Qwen3 model, too heavy for the per-PR path.

Keep it provider-portable: the steps only shell `pip` / `pytest` / `build`, so the workflow maps
onto any runner if the project ever moves off GitHub Actions.

**Verification:** open a trivial PR and confirm the check runs; then push a deliberately failing
test and confirm the PR check goes red — CI is only real once a red is proven to block.

---

## TODO 11 — Deterministic auto-re-onboard on contract_version skew (hook-enforced)

**File:** a new claude-code `SessionStart` hook under `.claude/hooks/`, `hive/app/mcp_server.py` (the
`_tool_result` beacon that stamps `contract_version`), `hive/app/onboard_ref.py` (`ONBOARDING_REFERENCE`),
`tests/app/test_onboard_ref.py`

The contract is **served, not shipped-frozen in the client**: the server is the single source of truth,
and each client holds only a version-keyed cache (the rules block + `.claude/` hooks + allowlist) that is
a *projection of server state*, not independently shipped software. So a contract update propagates by the
operator updating **one** artifact — the server image (`pull` + `hive up`) — and every client re-deriving
its cache on next connect. Nobody edits a client by hand.

The last mile is not yet deterministic. Today re-onboard is an **instruction to the agent** (the
HIVEMIND-RULES block says "RE-ONBOARD when a hive_* result's `contract_version` differs"), so it fires only
if the agent notices the skew and complies. Close it: a served `SessionStart` hook reads the beacon's
`contract_version`, compares it to the installed marker, and on drift **silently re-fetches and reinstalls**
the block + hooks + allowlist — the same hook-enforced pattern the Stop/capture and UserPromptSubmit/recall
hooks already use to make the discipline deterministic instead of advisory. That flips propagation from
"self-heals if the agent cooperates" to "self-heals, period." Shares the beacon/marker substrate with
TODO 9's AGI-posture axis (the marker becomes the `(version, posture)` pair).

**Mutations to verify:** with the installed marker behind the server's `contract_version`, the hook
re-onboards exactly once and restamps the marker (a second session is a no-op — no reinstall loop); with
the marker current, the hook is byte-inert (no rewrite); a server the hook cannot reach degrades safe
(keeps the last-good cache, never wipes it). The server must NEVER write a client file directly — the
actuator is always local (the no-server→client-reach law carried from TODO 9).

---

## TODO 14 — Ruby and Bash language support removed; re-add recipe if demand returns

**File:** `hive/combdrift/langs/`, `hive/matrix/extract/`,
`hive/verifier/registry.py`, and the doc-count claims across the engine reference docs (`docs/engines/`).

Ruby and Bash were removed entirely from all three language subsystems (matrix AST cone,
combdrift verdict, hive-verifier execution evidence — the latter now `hive/verifier/` in this repo) so nothing claims a capability the fleet does not
provide — the kept set is the six families / eight grammars (python, javascript, typescript, sql, go,
rust, c, cpp). This is a record, not open work: re-add only if real demand returns, via the now-uniform
seams — a combdrift `langs/<lang>.py` LangSpec + grammar dep, a matrix extractor + golden + fixture, a
hive-verifier `LangRecipe` row (+ `REGISTRY_VERSION` bump + `LOCKED_LANGUAGES`), and reverse the doc counts.

**Verification:** a re-add is complete when that language's row is green across the §6 functionality matrix
(combdrift found/missing/breaking/additive/parse_error + false-stale pin, matrix cone + `update()==build_graph()`,
hive-verifier conformance, edge E2E) and no "eight"/"six-language" count claim contradicts it.

## TODO 15 — C++ combdrift interface fidelity: full overload / template signature modeling

**File:** `hive/combdrift/langs/cpp.py`

combdrift C++ ships the plan's §8 CONSERVATIVE fidelity: existence (found/missing/indirect) always, but the
shape fingerprint only for an unambiguous single declaration — an overload set resolves to `ambiguous` →
`interface=None` (unverifiable), so an overload is never a wrong "breaking" verdict. Full overload-set and
template signature modeling is deferred: net-new combdrift work, Law-1-orthogonal (conservative fidelity is
already false-stale-safe; this only ADDS breaking-detection power it currently withholds).

**Verification:** with the deeper model, a C++ overload whose one member's signature changes reads
`breaking → stale` (today: `ambiguous`/unverifiable), while every existing false-stale pin stays green.

## TODO 16 — SQL Layer-B column-type fingerprint (combdrift)

**File:** `hive/combdrift/` (a SQL Layer-B extractor) and the `tests/edge` SQL E2E.

SQL combdrift staleness already works via EXISTENCE at table+column granularity (a dropped column →
`missing` → stale); SQL schema members render the `identity` shape because they carry no Layer-B interface.
A dedicated column-TYPE fingerprint (a type change → `breaking`, the SQL analog of a signature change) is
deferred — net-new combdrift work, Law-1-orthogonal (existence already covers the load-bearing case).

**Verification:** with SQL Layer-B, a column whose TYPE changes under an unchanged name reads
`breaking → stale` (today: `found`, unchanged), and the existing SQL existence + cone tests stay green.

## TODO 17 — Staleness-direct meta expansion (U4): validator tags beyond code symbols

**File:** `hive/edge/` (mint/verify, the anchor grammar), `hive/domain/meta_registry.py`,
`hive/app/sync.py` (backfill + change-time recompute), `hive/domain/change_evidence.py` (census
ingest seam), `hive/app/onboard_ref.py` (directive wording + contract bump), `CONTEXT/INTERACTIONS.md`,
upgrade-simulation tests in both repos.

The meta/tag system today verifies code-symbol anchors; whole classes of memory claims still have
no mechanical staleness check — library-version claims, config-key claims, doc/runbook claims,
runtime-conditional lessons. Extend the tagging system with a SMALL set of additional
machine-checkable tags that directly improve memory-staleness validation — nothing else qualifies.
Every tool computes and checks them: the edge CLI (mint/verify), hive-sync (backfill +
change-time recompute), and the census machinery where the tag's referent lives in the repo.

**Admission rubric (all five required):** deterministic to recompute from the consumer's world;
fail-open (unresolvable ⇒ omit the tag); directional compare where possible (breaking vs benign —
no naive-equality false stales); cheap at capture; and it must directly detect that a memory's
claim context went stale.

**Candidates (final cut happens in this update's own planning phase):**
1. `dep/<package>` — resolved version of packages the anchor's file actually imports; detects
   library-claim staleness via semver-directional compare (major ⇒ stale-advisory; patch ⇒ quiet).
2. Config-keypath anchors — extend the anchor grammar to declarative files
   (`file.toml:section.key` + a presence/type shape token), making config claims mechanically
   checkable. The largest single item: it changes load-bearing anchor parsing everywhere.
3. `doc/<file>#<heading>` — section content hash for documentation anchors, so prose/runbook
   claims get real staleness detection (today unverifiable).
4. `env/<runtime>` — runtime/toolchain version stamps for env-conditional lessons ("breaks on
   py3.12"), semver-directional; admit only if review agrees it is staleness-direct rather than
   general context.

**Rejected by rubric (recorded so they stay rejected):** live-network probes (non-hermetic ⇒ at
best a separate advisory-only tier, a different discussion), anything secret-bearing, machine-local
paths, memory-conclusion assertions, expensive-at-capture computation.

**Every admitted tag lands with the full slice:** its registry row + versioned token format,
mint-side computation in the edge CLI and the hive-sync backfill, verify-side directional check +
relay wording, census/change-time recompute where applicable, contract directive wording (a
contract version bump), and upgrade-simulation coverage proving pre-existing memories serve
unaffected.

**Sequencing:** deliberately last in the sync/tagging series — purely additive advisory channels
on settled semantics and the final tool layout. Start only after the hive-sync split's release
tail (move the `release` tag → pin flip → dogfood cutover → re-onboard) completes, since it edits the same
contract text, edge CLI, and doc surfaces. Two repos + a contract bump: this takes a full planning
phase (where the candidate cut is decided), not an ad-hoc add.

**Verification (per admitted tag):** one end-to-end scenario where a real staleness event
(dependency major bump / config key removed / doc section rewritten / runtime jump) is detected
and relayed as designed, plus the negative case (a benign change ⇒ silence); and the full
pre-existing corpus serves byte-identically before/after the update.

## TODO 18 — Recall concurrency: split the request lock, hoist the embed, partition the index by repo

**File:** `hive/app/http_server.py` (`_build_handler`, `run_http_dual`), `hive/app/container.py`,
`hive/adapters/sqlite_db.py`, `hive/domain/recall.py`, `hive/adapters/index_exhaustive.py`,
`hive/domain/ports.py`, `tests/app/test_http_server.py`, `tests/domain/test_recall.py`,
`tests/adapters/test_index_exhaustive.py`, a new concurrency suite

Three coupled bottlenecks on one path — the fleet's serving throughput. None is a storage-engine
problem: SQLite is not on the hot path for recall (the vectors live in a warm in-RAM matrix; the
store serves durability plus the resolve/partition reads) and is nowhere near its limits. What binds
is the serialization discipline and the shape of the index scan. Measured against the running
dogfood server, a warm `hive_recall` round-trip is ~170 ms (166 / 181 ms on consecutive calls,
near-empty store — so embed-dominated, not scan-dominated). Because that whole call is held under
one process-wide lock, the fleet ceiling is ~6 requests/second regardless of cores or store size,
and the 10th concurrent caller waits ~1.7 s. The binding limit is **concurrent agents (roughly a
dozen), not row count** — and it tightens if request rate is ever multiplied by a per-prompt
automatic recall path.

**(1) Split the request lock — read/write, not one mutex.** `_build_handler` wraps the entire
dispatch in `with lock: server.handle(req, identity=ident)`, so reads serialize behind writes and
behind each other. The invariant that lock actually protects is single-*writer* serialization
(THEORY §5) — the multi-hop mutation tick (`sqlite_db.tx()` = `BEGIN IMMEDIATE`: stamp trust →
record exposure → refresh the index row) must stay all-or-nothing, and the daemon shares ONE
connection across handler threads (`container.py`, `check_same_thread=False`), so writes must remain
mutually exclusive. Reads do not need that discipline: WAL already gives concurrent readers alongside
a writer, and `ExhaustiveCosineIndex` carries its own `threading.Lock`. Replace the single mutex with
a read/write lock, keeping the *same* lock object threaded into both listeners (the dual-door
property `run_http_dual` documents — no path may construct a per-listener lock). A shared connection
may still force a read connection (or pool) to realize the win; decide that in the update's own
planning phase, not here.

**(2) Hoist the embed out of the critical section.** `recall.py` calls `self.embedder.encode(query)`
inside the pipeline, hence inside the locked handler — so a Qwen3 forward pass, the dominant term in
that ~170 ms, is held against every other caller. The query vector is a pure function of the query
text and depends on no store state, so it can be computed before the lock is taken. This is the
largest single latency win and it is orthogonal to (1): even under a read/write lock, an embed inside
the critical section keeps readers behind each other.

**(3) Partition the index matrix by repo.** Today there is ONE shared exhaustive matrix and the
partition lives in the *candidate list*: a scoped recall runs the full global matmul and only then
filters through `servable_scopes()`. Since scoped recall is the common shape, splitting the matrix
per repo turns the scan from O(N_global) into O(N_repo) with no loss of exactness and no ANN. Scan
cost is linear and memory-bandwidth-bound — measured at d=1024 float32: 10k rows ≈ 41 MB / 3.3 ms,
50k ≈ 205 MB / 10.0 ms, 100k ≈ 410 MB / 21.1 ms, 250k ≈ 1.0 GB / 50.1 ms (one box, indicative). Under
a global lock that scan time is not merely latency, it is consumed throughput.

**Invariants that must survive (the reason this is a planned update, not a patch):**
- **No ANN, ever.** Partitioning must stay exact. Growing N may not flip the path, and a scoped
  recall may not become an approximation of the global one — that is the silent-recall-collapse trap
  `ExhaustiveCosineIndex` exists to make structurally impossible.
- **Gate equivalence.** A scoped recall over a per-repo sub-matrix must return byte-identical served
  results to the current scan-then-filter. The absolute-relevance gate has no distribution-shape
  dependence, so `tau_serve` is partition-safe by construction — but that must be *proven* by an
  equivalence test, not assumed.
- **Global path byte-stable.** Scope `None`/empty must keep its current exact global read.
- **Law 5 cache purity.** The index stays a warm cache over the store's approved rows; per-repo
  layout must still rebuild from `scan_approved()` and must not become a second source of truth.
- **Single-writer atomicity.** The promotion/exposure tick stays one transaction on one connection;
  no read path may observe a half-applied tick.

**Mutations to verify:** dropping the write-lock acquisition must red a concurrent-mutation atomicity
test (a half-applied promotion becomes observable); letting a read proceed *inside* a write tick must
red a torn-read test; making the pre-lock encode depend on store state must red a determinism test;
routing a scoped recall to the wrong sub-matrix — or silently dropping the global fallback for an
unpartitioned/unknown scope — must red the scoped≡global-filtered equivalence test; narrowing the
global scan to a subset of partitions must red an exactness test. Throughput is closed only when a
concurrency test shows N simultaneous recalls completing in materially less than N × single-call
latency; the current serialized path is the baseline to beat.
