# Changelog

All notable changes to this project are documented here.

## Added
- U4-THIN-AGENT (schema v3, served contract v3): ONE store, partitioned by repo at the
  memory level; agents are thin, repo-agnostic MCP clients that recall and store; the
  server owns mint, staleness, poison, outcome, and promotion; trust is fully mechanical
  + agent-adjudicated — no humans in the loop, no AGI sentinel. The pieces: a durable
  synced-repo REGISTRY (`repos` table; `hive repo add <url> [--name --branch --token-env]`
  / `hive repo remove <name>` / `hive repos`, shelling to the in-container
  `hive.tools.repoctl`; rows are operational data — the sync daemon re-reads them every
  tick, registering needs no restart; `--token-env` stores the NAME of the env var
  holding that repo's git token, never a secret byte, unset ⇒ the fleet-default
  `HIVE_SYNC__TOKEN`); the sync daemon rebuilt as a per-repo registry loop (one mirror,
  ledger receipt, candidate evaluation, and server-side mint backfill per registered
  repo; a deregistered repo's mirror is pruned next tick; per-repo census rows and a
  per-repo `hive_health(include_census_health=true)` block); repo-partitioned memory —
  structured `anchors=[{repo, anchor}]` + `repos=[...]` scope on the write verbs
  (an unregistered repo name refuses the call; the server mints fingerprints and judges
  drift per anchor, server-side — callers never supply fingerprint material) and scoped
  recall (`repos=["name"|"name@branch"]`, `anchor_prefix`; omit = global; general
  memories always remain candidates; partition filtering runs BEFORE the relevance
  gate); serve-now trust (`hive_write` lands `trust=provisional`, recallable
  immediately; `established` is reachable ONLY via a SHA-bound outcome-verified win on
  the repo's canonical line — the server-written census artifact, never a human vouch);
  the MACHINE-GATED retirement evidence gate (`hive_prune` / `hive_supersede` /
  `hive_write(replaces=)` retire only when the server itself verifies a qualifying
  machine signal for the target — anchor drift at the canonical tip, hurt evidence from
  another identity or a verified outcome, a mechanical contradiction, or a near-dup
  successor; an unqualified call is a benign no-op envelope, never an error); the
  served-only usage contract (the whole contract rides the MCP `initialize` result's
  `instructions` field, fresh at every connect — no install step, no versioned rules
  block, no client hooks, no re-onboard loop; sessions pick up a changed contract on
  reconnect); and a frozen v3 contract-test suite (`tests/contract/`) pinning the
  surface. Rollout is deliberately clean-store: schema v3 starts empty and the prior
  corpus is disposable — `hive reset`, then `hive repo add` each repo; no export, no
  re-seed, no migration tooling. Consuming repos must one-time strip the now-dead
  HIVEMIND-RULES block, hivemind hooks, and hive_* allowlist from their own rules files
  / `.claude/settings.json`; the served tool descriptions change and sessions pick them
  up on reconnect.
- `make check` — the repo's canonical mechanical gate (create-on-first-touch):
  `ruff format --check .` + `ruff check .` + `mypy hive/ --strict` +
  `pytest -m "not embed"` (the default suite — the heavy `embed` tier is opt-in),
  non-zero on the first failing leg, each leg runnable alone (`make format` /
  `lint` / `typecheck` / `test`). Legs run through `uv run --extra dev`, so a fresh
  clone needs nothing beyond uv; the dev extra gains `ruff`, `mypy`, and the
  `types-jsonschema` / `types-defusedxml` / `types-networkx` stubs. `[tool.mypy]` pins
  `python_version` 3.12 with per-module `ignore_missing_imports` ONLY for stub-less
  deps — the vendored `combdrift.*` / `matrix.*` engines and the embed-only
  `sentence_transformers.*`; the whole `hive/` tree now passes
  strict mypy, and the tree is ruff-formatted end to end.
- `hive-connect-repo` operator skill (`skills/hive-connect-repo/SKILL.md`): a guided runbook that
  arms and tests the server-side census feed against a GitHub repo — it checks the prerequisites
  (repo URL, a read-only token for a private remote, a sync-capable server image), auto-detects the
  tracked branch with `git ls-remote --symref`, writes `HIVE_SYNC__REPO_URL` / `HIVE_SYNC__TOKEN` /
  `HIVE_CENSUS__CANONICAL_REF` to `.env`, restarts to load it, and verifies the connection through
  the `sync` block of `hive_health(include_census_health=true)` (with a failure-mode table). Indexed
  in `skills/README.md`, `llms.txt`, and cross-referenced from `HIVE-ADMIN.md` §4.
- Server-automatic census (contract v.13, `MIN_EDGE_VERSION` 0.6.0 → 0.7.0 alongside the hive-edge
  0.7.0 release): the change-outcome evidence feed no longer needs per-repo/per-device git hooks —
  the server feeds itself. Armed by `HIVE_SYNC__REPO_URL` (unset ⇒ byte-inert: no thread, no
  clone, no census import; a token/webhook-secret without a repo URL fails boot EX_CONFIG), an
  in-process daemon thread (`hive/app/sync.py`, started by the entrypoint only after serve-ready,
  sharing THE one global write lock — held only for store access, never across git/subprocess
  work) keeps a local mirror (`/data/sync/mirror`, a rebuildable cache) and runs three fail-open
  legs per tick. (1) LEDGER: ONE unsigned receipt per contiguous watermark..tip range on the
  tracked branch (`HIVE_CENSUS__CANONICAL_REF`, else the origin default), built by
  `python -m hive.census.cli build` in a subprocess and ingested in-proc as
  post_merge/pass/none, the watermark (`sync:last_tip`) advanced in the same critical section;
  first connect baselines at the remote tip (no historical receipt); a force-push logs a
  discontinuity and records a defensive merge-base..tip receipt. (2) CANDIDATES
  (`HIVE_SYNC__VERIFY_CANDIDATES`, default on): changed PR heads (`refs/pull/*/head` fetched as
  `refs/sync/pr/*`, diffed against the `sync:pr_heads` baseline — first connect baselines without
  evaluating) get merge-base..head pre_merge receipts stamped `--ref refs/pull/<N>/head`, ≤5 per
  tick, built in a STRIPPED env (no HIVE_*, the token never rides env; `uv sync --frozen`
  provisioning iff the head carries uv.lock, else the verifier abstains) under a 600 s bound; a
  nothing-decided receipt is refused (logged + counted), and an exact already-ingested range skips
  before any build. (3) BACKFILL: approved code-anchored episodes lacking `combdrift/fp` are
  minted server-side through the real `hive-edge mint` subprocess against the tracked tip
  (absent-only merge — a present key cannot be displaced; provenance
  `hive-sync/minted: hive-sync-minted/1:server@<tip> <ref>`; ≤50 per tick). Alongside: a durable
  RANGE LEDGER — `ingested_ranges` PK `(repo, base, head, phase)`, the
  `already_ingested_range`/`record_ingested_range` port pair, `IngestReport.range_skipped`, and
  `ChangeOutcome.repo` — dedupes whole ranges across the daemon and a manual `hive ingest`
  (censusctl wires `ranges=store`; the report line gains `"range_skipped"`); a
  `POST /census-webhook` nudge on the TUNNEL door only (live iff `HIVE_SYNC__WEBHOOK_SECRET`;
  constant-time HMAC-SHA256 vs `X-Hub-Signature-256`, resolved pre-bearer after the
  body-drain + Origin guard; 204 + wake on match, 401 on mismatch; the payload is never parsed —
  a wake-up, never a data channel, the poll interval stays the correctness floor); and
  `hive_health(include_census_health=true)` gains a `sync` block (present iff `sync:*` meta
  exists: configured/tracked_ref/last_tip/last_sync_ts/last_error/candidates_evaluated/
  backfilled_total, with `status: "sync stalled"` only when configured yet dark). The census +
  verifier engines moved INTO the server (`hive/census/` — its CLI reduced to `build`, gaining
  `--ref`/`--repo-id` — and `hive/verifier/`; suites under `tests/census/` + `tests/verifier/`)
  behind a `sync` extra (currently a git pin to the hive-edge v0.7.0 tag with a dev-time uv
  source override to the local clone; it flips to the PyPI `hive-edge==0.7.0` pin after publish)
  and a Dockerfile that adds git + uv, installs `.[embed,sync]` (bringing the hive-edge CLI +
  engines into the image), and pre-creates `/data/sync/` — NO compose change (sync config rides
  `.env`; the mirror lives in the existing hive-data volume). The served contract rewrites
  `CENSUS_DIRECTIVE` server-automatic (`hive ingest` = the manual escape hatch; nothing to wire
  per repo or device), points `EDGE_CLI` at the PyPI install (`uv tool install hive-edge`), and
  drops the census-init wiring step from `ONBOARDING_PROCEDURE`. Operator docs and runbooks
  (README, HIVE-ADMIN §4/§5/§6/§8, OPERATIONS, llms.txt, the bringup/connect-team/operate skills)
  reconciled to the server-automatic feed + the reduced 0.7.0 edge CLI; OPERATIONS.md gains the
  coupled hivemind + hive-edge release runbook.
- Branch-scope tagging + the census ref stamp (contract v.12, `MIN_EDGE_VERSION` 0.5.0 → 0.6.0
  alongside the hive-edge 0.6.0 release). Four pieces, each byte-inert when unused:
  (1) the edge mints an OPT-IN, set-valued `git/branches` relevance tag (`hive-edge mint
  --branch-scope [NAME…]`, token `git-branches/1:<sorted space-joined names>`, registered in the
  hive-edge meta-key registry; never auto-attached — the pre-capture hook adds only the two
  fingerprint cores) and `hive-edge verify --branches <token>` routes by set membership: an
  off-set consumer whose anchor would read stale gets the advisory `branch_scoped` verdict —
  no remediation, no delta, radius suppressed — while member/untagged/unreadable-token/detached
  runs stay byte-identical; the post-recall hook relays it as ONE off-branch notice line.
  (2) `census build` stamps the measured checkout's branch as `provenance.ref` (resolved inside
  the build; detached ⇒ omitted; receipt schema v0 unchanged — the key is optional).
  (3) ingest threads that `ref` into every `change_outcome` / `verify_*` / `outcome_verified`
  payload as a conditional key, so legacy (ref-less) receipts render byte-identical payloads and
  their content-keyed dedup never moves.
  (4) a new `CensusConfig` group (`HIVE_CENSUS__CANONICAL_REF`, default "" = byte-identical
  unscoped build) scopes the recall rider: when set, `last_verified` + the stale `remediation`
  derive only from verify rows measured on that line — a newest foreign-line row is skipped and
  an older canonical row answers; legacy ref-less rows always count (the absence rule).
  The served contract teaches the flow: `MINT_DIRECTIVE` gains the opt-in clause (a relevance
  scope, never required), `VERIFY_DIRECTIVE` gains the membership semantics plus the human-gated
  supersession flow (an off-set hit verifying `current` on your branch ⇒ propose a superseding
  memory tagged with ALL relevant branches via `hive_write(replaces=…)`/`hive_supersede`).
  Non-goals held: no auto-minting, no serve-side branch filtering, no `git/head_sha`, promotion
  fuel (`verified_wins`) and the conflict worklist stay branch-unaware. Rides along: the
  single-source-root receipt join (BUG-038, fixed in hive-edge's matrix 0.3.4) is pinned on the
  ingest leg by a regression test + fixture (`tests/container/test_censusctl.py::
  test_single_source_root_receipt_joins_a_repo_relative_anchor`, `tests/data/receipt.singleroot.json`).
- `hive_health(include_meta_versions=true)` — the corpus token-version histogram, the meta
  envelope law's no-migration observability. Per episode-meta key over the LIVE corpus
  (`status='approved' AND trust != 'deprecated'` — servable + quarantined, retired tombstones
  excluded): counts per token-version prefix, an `absent` count (live rows carrying no value for
  that key), and a `malformed` bucket (values with no parseable version prefix, present only when
  nonzero) — so an operator can watch old token formats age out instead of migrating them. The
  fold is a pure domain function (`hive/domain/meta.py:meta_version_histogram` over
  `token_version`), the ONE sanctioned read of meta content server-side, and it reads only the
  `<engine>-<kind>/<N>:` version PREFIX — never the body, never per-episode behavior.
  `SqliteEpisodeStore.meta_version_counts` streams the rows; the flag joins its `include_*`
  siblings in the tool schema and description behind the same sole-request-flag gate (byte-inert
  when omitted). The meta-key catalog itself is committed in hive-edge
  (`hive_edge/meta_registry.py`, test-enforced coverage/agreement/retention ratchets); no contract
  bump, no `MIN_EDGE_VERSION` change.
- The served contract teaches the dependency-neighborhood fingerprint and the commit-lined-up
  census feed (contract v.10): `MINT_DIRECTIVE`'s printed map now names both fingerprint keys
  (`combdrift/fp` + `matrix/subgraph_fp` — passing the printed map verbatim as capture meta is
  unchanged); `VERIFY_DIRECTIVE` adds `--subgraph-fp <the hit's meta matrix/subgraph_fp>` beside
  `--fp` and teaches the advisory `radius` field (`changed` = re-verify against the current code
  before relying on the memory; never a retirement trigger by itself — the anchor verdict is
  untouched); `CENSUS_DIRECTIVE` + onboarding step 4 wire the post-merge + post-commit hook pair,
  so merges AND direct commits land as `change_outcome` evidence and keep the per-repo code graph
  current. `MIN_EDGE_VERSION` 0.3.0 → 0.4.0 alongside the hive-edge 0.4.0 release (matrix 0.3.0
  `out_dir` param; the `hive-edge graph update|radius|fp` verb group over a persistent per-checkout
  graph cache under `$HIVE_EDGE_HOME/state/matrix/`; `mint` printing the two-key union map;
  `verify --subgraph-fp`; both census hooks wired by `census init` and re-wired by `upgrade`).
  Operator docs (`HIVE-ADMIN.md` §8, `README.md` edge section, `OPERATIONS.md`, the connect-team +
  operate runbooks) reconciled with the two-hook + persistent-graph reality.
- `hive_health(include_census_health=true)` — a passive census dark-feed signal. Since the
  post-merge census hook is fail-open, a feed that has gone dark (building zero receipts) is
  otherwise invisible without per-repo CI. The new `hive/app/census_health.py` serves
  `days_since_last_change_outcome` (the days since the last SHA-bound `change_outcome` evidence row,
  `null` on an empty feed) behind the sole-request-flag gate, byte-inert when omitted. It serves the
  raw day-count with no invented staleness threshold; the flag joins its six `include_*` siblings in
  the tool schema and description.
- The served onboarding contract now wires the census evidence feed (BUG-030, contract v.08): a new
  `CENSUS_DIRECTIVE` in `hive/app/onboard_ref.py` rides `render_onboarding_payload()`'s `procedure`
  (the uncapped channel) and `ONBOARDING_PROCEDURE` gains step 4 — `hive-edge census init --repo
  <repo-root> --hive-url <registered URL>`, once per repo per device, idempotent, inert/fail-open on
  a device without the `hive` server CLI. Previously the one command that activates the post-merge
  `change_outcome` loop existed only in `HIVE-ADMIN.md` §8, so a repo onboarded purely over MCP left
  the corroborate/contradict loop silently dark. `MIN_EDGE_VERSION` 0.1.0 → 0.2.0 alongside the
  hive-edge 0.2.0 release (device-portable constant-bytes hook: run-time binary/config resolution,
  PATH-independent `census init`, `HIVE_EDGE_HOME` override, `census`/`hook` CLI discoverability,
  and matrix 0.2.0's import-closed incremental `update()`).
- `README.md` documents the companion `hive-edge` CLI (anchor mint/verify + the census post-merge
  evidence hook) in a new "Edge tooling" section: not required for the core recall/capture/write
  loop, installed automatically by a connecting agent during onboarding, or manually via
  `uv tool install hive-edge --from git+https://github.com/Hivemind-OSS/Hive-edge@release`.
- `hive upgrade [--ref release]` — move the server to a vetted release ref, backup-gated and
  health-verified: aborts on a dirty tree (no magic stash), snapshots the store to the host BEFORE
  any checkout, then checkout → rebuild → bounded health-wait → app status gate, and auto-reverts
  (code + store) on any post-checkout failure — printing the exact manual `git checkout` + `hive
  restore` recovery if the revert itself fails. Reuses reset/restore's snapshot + copy-into-volume
  blocks as shared helpers. `hive connect` now prints a one-line `hive-census init` edge-tools
  breadcrumb (transport boundary preserved — a cross-reference, not a merged verb). See
  HIVE-ADMIN.md §8.
- `hive ui` — a loopback-only operator dashboard verb (`hive/tools/ui.py` + `hive/tools/ui_page.py`):
  a stdlib `http.server` control plane bound to 127.0.0.1 that opens the native browser to ONE
  self-contained page (inline CSS/JS, zero build step, no external asset) plus a same-origin JSON
  API — live status (serialized from the single-owner `StatusSnapshot` a `cli._status` refactor now
  shares), seat list/mint/revoke over the in-container authctl, safe lifecycle (backup, loopback-only
  `up`, volume-preserving `down`), and a bounded logs tail. No reset exists — by construction,
  not merely hidden. The browser door INVERTS the MCP daemon's Origin doctrine: it admits the
  same-origin browser and enforces a Host-header allowlist + a same-origin Origin check on POST + a
  declared-length 413 body cap (each a named-mutation guard, fail-closed), while auth stays a property
  of the loopback socket (tokenless). Pinned by `tests/container/test_ui.py`, `test_ui_page.py`, and
  the `_probe_status` / `_exec_backup` tests in `test_cli.py`.
- `hive ui` dashboard controls — the lifecycle route is now NON-BLOCKING: `/api/lifecycle` validates
  synchronously, then runs the docker child on a detached worker behind a module-level single-flight
  lock (a second concurrent op → 409 `operation_in_progress`), and plain Start drops `--build` so it
  no longer rebuilds and bounces a healthy stack (this fixed a browser hang). Adds tunnel
  activate/deactivate (`tunnel-up` gates `NGROK_AUTHTOKEN` + `NGROK_DOMAIN` from the environment only
  and fails fast; `tunnel-down` stops only the sidecar) and restore-from-backup — `GET /api/backups`
  lists the in-volume snapshots and `POST /api/restore` replaces the live store from a chosen snapshot
  behind a bare-basename path-traversal whitelist and an automatic pre-restore safety snapshot (a
  failed snapshot aborts before any overwrite). The page gains a tunnel control and a restore picker
  behind a typed-"restore" confirm. Reset stays absent by construction; each new guard is a
  named-mutation test in `tests/container/test_ui.py`.
- `verified_promotion` GRADUATED to default-ON (`hive/app/config.py`): the L4 verified-outcome
  rung now ships enabled, ratified by the powered gate below (+0.243 success, FSR 0.0 at power,
  CI [0.108, 0.378] clears zero). A safety-loosening rung earns default-ON only as a graduation
  ratified by a pre-registered benchmark plus operator sign-off; the operator opts OUT with
  `HIVE_AUTONOMY__VERIFIED_PROMOTION=false` (byte-identical to the strict build when off). Residual
  OPEN gate carried forward: receipt authenticity (keyid pinning) — a fabricated receipt still
  promotes at the ingest door. Pinned by `test_autonomy_verified_promotion_defaults_on_and_env_opts_out`
  and `test_verified_promotion_wiring_follows_the_flag`.
- POWERED rerun of the L4 empirical gate: three independent-seed runs
  (`hive/research/bench/runs/l4_gate_powered_s{0,1,2}.json` + per-arm replay logs,
  seeds 0/1/2, n=30 each, haiku, the recorded run's tau/poison config) pooled via
  the new `--l4-pool` reducer into `l4_gate_powered_pooled.json`. Every C′ control
  promoted nothing (0/3 seeds); the rung promoted only verified-helped captures
  (225/194/212) and never a verified-hurt row. Pooled corrected slice n=37:
  success 0/37 (C′) vs 9/37 (E), FSR 0.0 in both arms, success delta +0.243 with
  paired bootstrap CI [0.108, 0.378] — the CI clears zero with FSR non-worsening,
  so the pre-registered `clean_win` criterion is MET — and on that evidence
  `verified_promotion` was subsequently flipped to default-ON (see the graduation
  entry above).
- Cross-seed pooling for the L4 gate (`poison_run.py --l4-pool <reports...>`):
  each arm summary now carries its per-task frozen `PoisonTaskObs` rows under
  `cases`, and `pool_l4_reports` rebuilds those carriers across >=2 independent-seed
  reports, concatenates the task-paired vectors, and rescores through the same
  `score_l4_arms` path (config-parity enforced; duplicate seeds, legacy
  reports without case rows, and mismatched configs are refused).
- The L4 bench plant phase is observable: `preload_captures` logs a
  rows-done/total + elapsed heartbeat every 500 rows plus a completion line, and
  `run_l4_gate` announces each arm's plant/inject/demand/agent-loop transitions,
  so a detached (nohup) run's console log always shows where the wall-clock goes.
- The universal capture/recall discipline floor (contract v.03): the served
  HIVEMIND-RULES block — and through it the initialize-instructions floor every
  MCP client receives at connect — now carries the cross-platform discipline as
  plain rules-file markdown (valid verbatim in CLAUDE.md / AGENTS.md /
  .cursorrules / .windsurfrules): recall-before-edit-by-anchor, the task-end
  capture shape (WHAT + WHERE as `path/file.py:symbol` in both body and anchor +
  WHY), capture-only-never-write at task end, the DO-NOT-CAPTURE noise floor
  (single-sourced with the taxonomy via the new `NOISE_FLOOR` constant), and the
  zero-capture escape — with the tiering documented in the text itself (advisory
  everywhere; deterministic where a platform has lifecycle hooks).
  `CONTRACT_VERSION` bumped v.02 → v.03 by the pre-commit guard, keystone golden
  regenerated; connected agents re-onboard on the beacon mismatch.
- Graph-propagated staleness, detection-only: the census ingest now honors the
  receipt's optional `propagation` block (built census-side under
  `hive-census build --propagate`) — the same atomic batch appends one advisory
  `stale_suspect` evidence row per episode whose anchor joins a breaking/removed
  seed's blast-radius neighbours (the same anchor rule as the point join;
  stamp-gated pre-merge like the verified/verify riders; a directly-matched
  episode's point row dominates and suppresses its suspect row).
  `hive_health(include_stale_suspects=true)` surfaces the worklist — per SERVABLE
  episode the newest suspect `{episode_id, anchor, seed, drift, head_sha, ts}`,
  anchor-bucketed, request-flag gated (byte-inert until asked), fail-open to `[]`;
  resolution stays human (re-verify, then `hive_supersede`/`hive_prune`). The
  censusctl report line gains the `stale_suspects` counter.
- Recorded the first REAL L4 empirical-gate run (`hive/research/bench/runs/l4_gate_s0.json`
  + per-arm replay logs): LongMemEval-S n=30 (seed 0), haiku, production gate/autonomy
  knobs. The mechanism behaved exactly as designed — the OFF control promoted nothing
  and served nothing; the ON arm promoted 225 of 302 verified-helped captures, never
  promoted or served any of the 12 verified-hurt poison rows (FSR 0.0 in both arms),
  and lifted corrected-slice success 0/12 → 3/12 (E − C′ success delta +0.25, paired
  bootstrap CI [0.0, 0.5]). The CI does not exclude zero at 12 paired points, so the
  pre-registered `clean_win` criterion is NOT met: **`verified_promotion` stays
  default-OFF**; a default flip would need a larger-n rerun whose CI clears zero, plus
  explicit ratification.
- L4 empirical-gate arm pair in the poison bench (`poison_run.py --l4`) — the run the
  `verified_promotion` default flip is conditioned on: arm E (`verified-outcome`) vs
  arm C′ (`l4-off`), both planting the pool (gold AND poison) as capture-only
  quarantined rows and injecting SHA-bound `outcome_verified_helped`/`_hurt` ledger
  rows through the store's idempotent `append_evidence` (payloads rendered by the same
  `render_verified_payload` the census ingest uses, full L7 version stamp), then
  driving one demand tick per evidence row — the flag is the sole difference, so the
  E − C′ delta is the rung's end-to-end contribution. Scored success-led with the
  existing paired-CI machinery: `clean_win` ⇔ success improves AND false-serve does
  not worsen. A C′ that promotes anything invalidates the pairing and the run refuses
  to emit a report; per-arm promoted counts and injected-evidence counts are required
  provenance.
- Resurrected the dev-time benchmark harness (`hive/research/bench/`, poison subset)
  from history: the four-arm poisoned-shared-store false-serve runner
  (`poison_run.py` + substrate/agent/backends/LLM seam and their offline tests),
  restored verbatim and re-fenced (wheel-excluded via `pyproject` `exclude`,
  `.dockerignore`d out of the image, `test_research_not_imported_by_runtime`
  re-added). Its scoring layer now sources the IR/significance primitives from
  `scripts/eval_metrics.py`, which gains the missing `mrr` and `bootstrap_ci`
  (with their locked tests) instead of a parallel metrics module.
- Verified-outcome rider on the census ingest (Flow A) — the same atomic
  `hive ingest` batch now also writes, per matched episode (pre-merge only, and only
  when the receipt carries the full `ModelVersion ⊕ VerifierVersion ⊕ SHA` provenance
  stamp), the mechanical `outcome_verified_helped` / `outcome_verified_hurt` rows
  (a `dont` memory corroborated by breaking/removed drift + a DECIDED failing test
  run with blast-radius reach; a `do` memory contradicted by a decided-failing run
  with reach; everything else abstains, fail-closed) and the `verify_current` /
  `verify_stale` anchor-verification rows. Four new registry kinds in
  `hive/domain/evidence_kinds.py`; the censusctl stdout report gains the four
  counters; `AnchoredEpisodeReader` carries `(id, anchor, polarity)`.
- `last_verified` recency stamp on recall hits — a hit whose episode carries a
  `verify_current`/`verify_stale` ledger row rides `{ts, sha, state}` from the newest
  row (new narrow `LastVerificationReader` port; fail-OPEN boundary side-channel;
  never-verified hits emit no key, so the envelope stays byte-identical). The kernel
  never judges churn — the edge compares the stamp to the code.
- Verified-win promotion rung, **default OFF** (`HIVE_AUTONOMY__VERIFIED_PROMOTION`) —
  when enabled, a quarantined memory carrying a SHA-bound `outcome_verified_helped`
  audit promotes at the next demand tick even when demand alone would not (competitor
  veto retained; the corroboration is non-forgeable by the memory's writer because
  `evidence_events` is server-written only). OFF wires no reader handle: the rung is
  structurally unreachable and every envelope byte-identical. `settled_wins` becomes
  the UNION of self-reported and verified wins, so the suspect-consensus martingale
  honors a verified win with zero consumer change.
- `meta` map carrier on `hive_capture` / `hive_write` — an optional namespaced map of
  opaque machine handles (`{"tool/attr": "value"}`, e.g. a capture-time `combdrift/fp`
  interface fingerprint) normalized once at the boundary (`hive/domain/meta.py`:
  canonical key-sorted serialization, loud `BadMeta` refusals), secret-scanned
  REFUSE-only (a redacted handle is garbage), stored on `episodes.meta`, and served
  back verbatim on recall hits — the `meta` key rides a hit only when non-empty, so
  the no-meta envelope is byte-identical to the pre-meta surface. A lifecycle-current
  store that predates the column gains it via one explicit additive
  `ALTER TABLE episodes ADD COLUMN meta` migration (loud, defaulted, lossless — so
  `hive restore` of pre-meta backups keeps working); pre-lifecycle stores still
  refuse. Dedup preserves the existing row's meta unmerged; recall stays meta-blind
  (the carrier is never embedded).
- `hive ingest` + `hive.tools.censusctl` — the change-outcome feed: a signed census
  receipt (stdin or path) lands as one append-only, SHA-bound `change_outcome` row on the
  `evidence_events` ledger, joined to the episodes whose anchors match the receipt's
  touched symbols. Fail-closed end to end: decided execution lines only, nothing-decided
  refuses (exit 65, no row), idempotent re-ingest, malformed payloads refused loudly, no
  trust mutation (O7) and recall byte-inert. Backed by the new
  `hive/domain/evidence_kinds.py` registry (the six existing ledger-kind literals
  retrofitted onto the single source) and two narrow ports
  (`AnchoredEpisodeReader`, `ChangeEvidenceAppender`).
- `LICENSE` — the project is released under the **Apache License 2.0**, declared in
  `pyproject.toml` via the PEP 639 `license` / `license-files` fields (build requires
  `setuptools>=77`).
- `scripts/contract_version_guard.py` + `.githooks/pre-commit` — a non-blocking pre-commit guard
  that keeps the agent-contract `CONTRACT_VERSION` monotonic: when a commit stages a change to the
  served-contract source (`hive/app/onboard_ref.py` / `hive/domain/kinds.py`) it bumps the version
  past HEAD if it is not already ahead, regenerates the keystone bundle-hash golden from the new
  single owner `onboard_ref.bundle_digest()`, and `git add`s both so the bump rides in the commit.
  Enable with `git config core.hooksPath .githooks`. Contributor tooling (wheel-excluded).
- `skills/` — operator runbook-skills (`hive-bringup`, `hive-connect-team`,
  `hive-backup-restore`, `hive-operate`), each a self-contained `SKILL.md` with trigger
  frontmatter, so an agent can run the load-bearing lifecycle / connect / backup / tuning
  procedures without rediscovering them. Indexed in `skills/README.md`; the long-form reference
  stays in `HIVE-ADMIN.md` + `OPERATIONS.md`.
- `HIVE_SECRET_SCAN__ENABLED` (config group `secret_scan`, default `true`) — an operator
  opt-out for the credential secret floor. On by default it is byte-identical to the prior
  always-on scan; `false` bypasses the pre-persist scan so raw text (secrets included) is
  stored unscanned. Disabling loosens a safety gate, so the default is the safe posture and a
  disabled floor is logged loudly at boot and surfaced as `secret_scan_disabled` in `hive_health`.
- `hive backup` — a manual durability verb (the in-container `hive.tools.backupctl`
  entry runs the existing `run_daily_backup` and prints the snapshot path).
- Self-serve onboarding is served-only: the full usage contract is delivered over MCP via the
  `initialize` instructions (every client surfaces them), with a secondary reference carried in
  the `hive_health` tool description. It installs nothing and writes nothing into a rules file —
  no server-driven handshake.
- `hive` operator CLI (`hive/tools/cli.py`, stdlib-only, `[project.scripts]` entry
  point; uninstalled: `python -m hive.tools.cli`): `up [--tunnel]` (bounded
  health-wait; tunnel secrets fail-fast), `down`, `logs`, `reset` (snapshot the store out
  of the volume, then destroy + recreate it empty — recoverable; aborts if the snapshot fails),
  `restore` (replace the live store from a snapshot — the inverse of reset),
  `status` (health + tunnel + seat count), `token`/`revoke`/`tokens` (shell to the
  in-container authctl — crypto + schema stay single-sourced), `connect` (teammate
  MCP registration line; transport only, no per-repo onboarding).
- `authctl list` subcommand + `SqliteTokenStore.labels()` — seat inventory, labels
  only, never hashes.
- Mechanical memory lifecycle: `hive_capture` lands insights
  quarantined (embedded, structurally unservable); measured recall-miss demand from
  ≥1 non-writer identity auto-promotes to `provisional`, served WITH its trust label;
  TTL decay retires unused quarantined/provisional rows; `hive_write(replaces=)` is
  the human supersession path (the only retirement of `established`).
- `recall_misses` + `evidence_events` tables; exposure rows now carry `agent_id` and
  refresh the served rows' liveness clock in the same transaction.
- Recall hits carry `trust` + `ts`; `hive_health` adds `trust_counts`, `n_misses_7d`,
  and `include_gaps` (clustered demand-gap report).
- `autonomy` config group (`enabled` + demand / TTL knobs); like all
  config it is resolved at boot — there is no live reload (tune via `.env` then `hive up`).

## Changed
- The three anchor/census engines are absorbed as **first-party subpackages of the one `hive`
  distribution** — `hive.matrix` (AST code-structure + blast radius), `hive.combdrift` (node-level
  contract-drift / call-shape fingerprint), and `hive.edge` (the in-image engine CLI front) —
  ported byte-preserving from the former `Hivemind-OSS/Hive-edge` workspace at source commit
  `f94e1a7`, which is thereby archived: the hivemind repository now self-contains every engine and
  builds the whole server from itself alone. The vendored-wheel channel dies with it —
  `vendor/wheels/`, `scripts/vendor_edge.py`, the `[tool.uv.sources]` engine pins, and the
  four-site version-lockstep + `KNOWN_HIVEMIND_MIN_EDGE_VERSION` floor machinery are all removed;
  the engines' third-party dependencies (tree-sitter `<0.26` + the eight grammars, networkx,
  sqlglot) move into the `sync` extra and resolve through `uv lock` / `uv sync` like any other
  dependency — no wheelhouse to refresh, no separate engine repository to release. Server behavior
  is byte-identical: the `hive-edge` console script name is kept (re-pointed to
  `hive.edge.cli:main`) so the sync mint/verify discovery + argv are untouched; all token formats,
  the four registered meta keys, and receipt/provenance shapes are unchanged; the frozen contract
  suite (CT-1…14) passes byte-untouched, joined by CT-15/16/17 pinning self-containment,
  engine-seam byte-parity, and the agent-side deletions. The U2 meta-key registry re-homes to
  `hive/domain/meta_registry.py` (owner strings drop their `hivemind:` prefix), strict
  `mypy hive/ --strict` extends over the absorbed engines with **no** first-party override, the
  engine version constants (`hive.matrix.__version__`, `hive.combdrift.version.COMBDRIFT_VERSION`)
  are frozen as provenance literals, and the dead agent-side vestiges (`hooks.py`, the `hook` and
  `worktree-delta` verbs) are not ported — they join the argparse-rejected exit-2 set.
- The shipped operator skills resolve the `hive` CLI mechanically: each runbook opens with a
  one-line probe (`command -v hive >/dev/null 2>&1 || hive() { python3 -m hive.tools.cli "$@"; }`)
  so every literal `hive …` step runs on an uninstalled checkout — the CLI is stdlib-only — and
  the install docs note the PEP-668 externally-managed-Python wrinkle (README, `HIVE-ADMIN.md`
  §1, `skills/hive-bringup`).
- PyPI is removed as an install/publish channel for our distributions (`hive-edge`, `matrix`,
  `comb-drift`) — system-wide, both repos (closes BUG-045). Agents install the edge CLI from the
  public git repository via uv: the served contract (now **v.14**) renders
  `uv tool install git+https://github.com/Hivemind-OSS/Hive-edge@release` (+ `uv tool
  update-shell` for PATH) from the single-owned `EDGE_REPO_URL`/`EDGE_REPO_REF` constants, states
  uv as REQUIRED with the reason (the CLI's workspace engines resolve from git subdirectories via
  uv sources — pip/pipx would resolve the squatted `matrix` name), updates via
  `uv tool upgrade hive-edge`, warns off the retired `hive-edge upgrade` verb (hive-edge 0.9.0
  removed it with the whole PyPI machinery), reframes rollback as reinstalling an explicit ref
  (deferring to a human pin), and preserves the degraded-mode clause (no CLI → mint/verify no-op;
  capture/recall unaffected). Onboarding's Claude-Code step 2 becomes a deterministic hook
  RECONCILE instead of a merge (closes BUG-043): `HIVEMIND_HOOK_COMMAND_MARKERS` +
  `is_hivemind_owned_hook_command()` single-own hook ownership; the procedure removes every
  hivemind-owned hook command wherever it sits (any event, any matcher group, pruning emptied
  containers), THEN inserts the served set verbatim — operator hooks untouched, the auto-approve
  allowlist installed as the EXACT served set (never merge-accumulate) — and ends with
  restart-to-activate (hooks load at session start only); the step-4 re-onboard repeats the
  reconcile and restart. `MIN_EDGE_VERSION` 0.7.0 → 0.9.0 alongside the hive-edge 0.9.0 lockstep;
  `vendor/wheels/` re-vendored to 0.9.0 (pyproject wheel pins + `uv.lock` in the same change);
  the conservation corpus's install probe re-pinned to the frozen∩live intersection (unit 40,
  BUG-041 precedent); doc mirrors rewritten to the git-install reality (README, HIVE-ADMIN §8,
  llms.txt, the hive-connect-team skill, the OPERATIONS release runbook — which gains the
  move-the-`release`-tag-first duty — and the TODOS release tail).
- The server ships self-contained from this repository alone: the `[sync]` extra's engines
  (comb-drift, matrix — now declared as the direct dependencies they are; `hive/census/join.py`,
  `receipt.py`, and `hive/app/sync.py` import them by name) and the in-image `hive-edge` mint CLI
  install from the committed `vendor/wheels/` wheelhouse. The image build pre-installs it before
  the extra resolves and dev/uv pins the exact wheel paths in `[tool.uv.sources]`, replacing the
  remote git pin a production host — which receives only this repo: no package index, no sibling
  checkout — could never resolve (the `matrix` name on PyPI belongs to an unrelated project).
  `scripts/vendor_edge.py` is the single refresh seam: it rebuilds the three wheels from a
  hive-edge checkout, refuses a half-bumped workspace (the hive-edge distributions ship one
  lockstep version, currently 0.8.0) or a wheel below the served `MIN_EDGE_VERSION` floor,
  rewrites the pyproject wheel pins, and re-locks — wheelhouse, `pyproject.toml`, and `uv.lock`
  land as one unit. The build-context secret scan
  (`tests/container/test_dockerignore_no_secret.py`) now covers `vendor/` as part of the layered
  COPY set, and the `OPERATIONS.md` release runbook drops its tag/publish/pin-flip steps for the
  wheelhouse refresh.
- `AgiConfig`'s docstring (`hive/app/config.py`) adds a scope note: `HIVE_AGI__MODE` today only
  gates the `AGI_OVERRIDE` sentinel check on `hive_write`/`hive_supersede`/`hive_prune` — it is not
  yet a fully autonomous mode. `HIVE-ADMIN.md` and `OPERATIONS.md` already documented this narrow
  scope; the code now says so explicitly.
- The operator docs (`HIVE-ADMIN.md` §8, `skills/hive-connect-team/SKILL.md`,
  `skills/hive-operate/SKILL.md`, `llms.txt`) and the `hive connect` edge-tools breadcrumb now
  reference the published **`hive-edge`** CLI (`git+https://github.com/Hivemind-OSS/Hive-edge`)
  instead of the retired `hive-census`/`comb-drift` names — `hive-edge census init` /
  `census build` / `upgrade` replace the old verbs throughout.
- `OPERATIONS.md` moved from the gitignored `docs/` tree to the repo root so it ships with the
  release; the long-form operations & tuning-evidence reference now travels with the repo, and the
  references in `llms.txt`, `skills/`, and the README layout point at the new location.
- Documentation follows the [llmstxt.org](https://llmstxt.org) convention: the self-contained
  operating guide moved `llms.txt` → `llms-full.txt`, and `llms.txt` is now the short curated
  link index over the project docs (`llms-full.txt`, `README.md`, `HIVE-ADMIN.md`,
  `skills/README.md`, `OPERATIONS.md`, and the attribution notice). `README.md` and
  `HIVE-ADMIN.md` now point the detailed explanation at `llms-full.txt` and name `llms.txt` as
  the index.
- Agent-contract `CONTRACT_VERSION` `v.01` → `v.02`: the served onboarding is raised from MAY →
  **MUST install** (the `initialize` instructions, the `hive_health` reference, the installed rules
  block, and the onboarding procedure now direct the install, while keeping the honest "degrades
  safely to the served floor" clause — the install is a salience directive, not a correctness gate).
  The self-heal loop is the `contract_version` beacon stamped on every tool result via the single
  `_tool_result` choke point: the agent compares it against the `contract-version=` embedded in its
  installed HIVEMIND-RULES marker and, on a mismatch (or no block installed), reinstalls the bundle
  (block + hooks + allowlist) per the procedure single-sourced in the block + the `initialize`
  instructions. The bare-string `_tool_error` path stays unbeaconed (the single-owner boundary).
- Auth is now a property of the **listening socket**, not a config mode: the daemon binds a
  tokenless **loopback** door (host-published `127.0.0.1:8765`) and a token-required **tunnel**
  door (compose-internal `8766`, ngrok-forwarded). `HIVE_AUTH__MODE` (the `token|open` switch)
  and the `--tunnel`-refuses-open CLI rail are removed — the tunnel door is structurally
  token-gated. Identity is **per-agent-session**, resolved uniformly on both doors
  (`X-Hive-Agent-Id` → server-minted `Mcp-Session-Id` → `local`); the token authenticates the
  tunnel door but is never the identity, so a fleet of K agents behaves identically whether 1 or
  N engineers run it. Local agents connect tokenless; remote teammates use a per-seat token.
- Convergence KPI window narrowed 14d → 7d (`trends.WINDOW_DAYS`): current vs previous
  one-week windows over the warm store.
- Tool surface is eight verbs (`hive_write` / `hive_capture` / `hive_recall` /
  `hive_supersede` / `hive_prune` / `hive_flag` / `hive_outcome` / `hive_health`); serving is
  decided by the single `lifecycle.is_servable` predicate
  (established, or fresh provisional) at the scan, the index sync, the pipeline resolve
  step, and the per-hit belt.
- Boot order: decay sweep runs before the index rebuild.

## Removed
- The standalone `../matrix` and `../hive-census` engine mirrors — stale, un-remoted duplicates of
  hive-edge's canonical `packages/matrix` / `packages/hive-census`, long drifted from them (an older
  monolith layout carrying its own git history). `tests/container/test_ingest_live.py` drops the
  optional census-regeneration leg that was the mirrors' only consumer — it was pinned to two commits
  living solely in `../matrix`'s history, so it could not follow the engines into hive-edge. The
  load-bearing D7 gate (a real unsigned receipt through the real `hive ingest` into a real running
  server, Docker-skip-guarded) is untouched; real census-build coverage now rides the `hive-edge
  census init` self-test and the live promotion path instead of a sibling checkout.
- All census receipt **signing** machinery — the ed25519 sign/verify path, the key loaders,
  `EnvelopeError`, the `HIVE_CENSUS_SIGNING_KEY` gate + `--unsigned` flag (unsigned is now
  unconditional), and the `securesystemslib` + `cryptography` dependencies. Census emits an
  unsigned in-toto Statement in a DSSE-shaped envelope (`signatures: []`, `unsigned: true`)
  built with the standard library alone, and the kernel drops the vestigial `keyid` from
  `parse_receipt`, `IngestReport`, and the `censusctl` report. Receipt authenticity was never
  the ingest-door trust boundary — transport/auth is (loopback locally, per-seat tokens over a
  tunnel) — so the signature bought nothing here; the earlier "keyid pinning" residual gate is
  thereby dissolved (authenticity is out of scope by design). Integrity is unchanged: the
  subject digest covers the canonical predicate bytes and the ingest door re-checks it, refusing
  any tampered receipt (the Law-7 tamper guard, preserved and tested kernel-side).
- `autonomy.solo_mode` + `autonomy.solo_min_span_days` — demand-promotion is now ONE
  identity-diversity rule for solo and team alike. A solo dev's independent agents each carry a
  distinct per-session identity, so their shared demand promotes with no flag (the elapsed-span
  bypass is gone, making anti-gaming strictly stricter). The `solo_hint` is kept but repurposed:
  it now flags single-identity traffic and points to `hive connect` / `X-Hive-Agent-Id`.
- `hive.sh` — replaced by the `hive` CLI; its up-not-run-rm contract lives on as a
  pytest argv assertion.
- No migration/backfill path ships (clean-store start, human decision): an
  old-format `episodes` table is refused at store construction.
- `hive/research/` — the dev-time benchmark harness (the LongMemEval head-to-head vs mem0, the
  poison-substrate trust bench, and the L4 empirical-gate runs recorded under `bench/runs/`) and
  its mirrored `tests/research/bench/` suite. `tests/test_purity.py` drops the now-obsolete
  `test_research_not_imported_by_runtime` purity-fence test and the dead `research`-path scan
  filter it required elsewhere in the file; `pyproject.toml` drops the `bench` optional-dependency
  extra (`mem0ai`, `huggingface-hub`) and the `hive.research*` wheel-exclude (nothing left to
  exclude); `.dockerignore` drops the matching line. `uv.lock` regenerated, dropping 25 now-orphaned
  transitive packages.

## Fixed
- `hive-edge verify` classifies a prose anchor whose text contains a dot (e.g.
  `the v2.0 rollout`) as `unverifiable`/`not_a_code_anchor` instead of a false
  `stale`/`file_missing`. The code-shaped-path check (`hive/edge/cli.py:_is_code_shaped_path`)
  now requires a whitespace-free path with a clean, letter-initial filename extension — a
  bare `os.path.splitext` extension read a dotted prose token (`.0 rollout`) as a missing
  code file, skipping the not-a-code-anchor reclassification.
- The served onboarding procedure now creates an absent rules file in flight. `ONBOARDING_PROCEDURE`
  step 1 (`hive/app/onboard_ref.py`) spelled out only the "a block is already present, REPLACE it in
  place" case, leaving the DEFAULT first-onboard state — a fresh clone ships no rules file (the rules
  file is gitignored) — to agent inference (BUG-046). Step 1 now names all three states explicitly:
  CREATE when absent, APPEND the block when the file exists without one, REPLACE in place when a
  block is present. Serving-side install text only, so no `CONTRACT_VERSION` bump — the keystone
  (`bundle_digest`) hashes the rules block + hooks + allowlist, never the procedure; guarded by
  `test_procedure_step1_directs_create_append_or_replace_of_the_rules_file`.
- The contract-version pre-commit guard (`scripts/contract_version_guard.py`) bumps
  `CONTRACT_VERSION` only when a staged edit actually changes the INSTALLED bundle (the rules block +
  hooks + allowlist — `onboard_ref.bundle_digest`), not on any edit to a watched file. A
  serving-side-text-only change — the install procedure, the mint/verify/census directives,
  `REMEDIATION_NOTICE`, none of which are in the version-pinned bundle — no longer forces a version
  bump and a fleet-wide re-onboard that reinstalls a byte-identical bundle (BUG-047). The guard
  compares the live digest to HEAD's keystone golden while the tree is still at HEAD's version (so
  the version stamp normalizes out); a bundle edit bumps + regenerates the golden as before, and a
  hand-bump ahead of HEAD is kept. The digest subprocess runs under `-B` so the second import around
  the same-size `v.NN`→`v.NN+1` bump can't reload a stale `.pyc`.
- `skills/hive-bringup` no longer claims the store defaults to `:memory:` — the fifth surface of
  the containerized-default drift (the entrypoint injects `/data/shared.db` when the env is unset;
  only an explicit `:memory:` boots ephemeral), matching the corrected README / `HIVE-ADMIN.md` /
  `.env.example` / boot-WARN wording.
- The ephemeral-store messaging matches the containerized default: the boot WARN
  (`hive/app/container.py`) and the `.env.example` persistence comment no longer claim the store
  defaults to `:memory:` — the entrypoint injects `/data/shared.db` when the env is unset, so only
  an explicit `:memory:` boots ephemeral. The verified-win rung's docstring/comment
  (`hive/domain/lifecycle.py`) now calls the `None`-reader form the opt-OUT form rather than
  "default OFF" (`HIVE_AUTONOMY__VERIFIED_PROMOTION` defaults ON). README's `hive_health` row
  names the full include-flag surface, `OPERATIONS.md`'s complete-set knob table gains
  `HIVE_SYNC__INTERVAL_S` / `HIVE_SYNC__MIRROR_DIR` / `HIVE_HTTP_MAX_BODY_BYTES`, and the TODOS
  language-support record points at `hive/verifier/registry.py` in this repo.
- A `SUBGRAPH_FP_VERSION` bump would have emitted a false `radius: "changed"` advisory on every
  memory minted under the old version (BUG-037). `hive-edge`'s `_verify_core` compared the stored
  `matrix/subgraph_fp` token against the recompute as whole-token raw `!=`, so any version bump
  made every old token compare unequal — a false re-verify alarm fleet-wide, with no way to
  distinguish it from a genuinely moved neighborhood. Fixed in `hive-edge` 0.5.1: a
  `_subgraph_fp_version` envelope parse gates the compare — same version ⇒ body compare exactly as
  before (current-version corpora byte-identical); different/unknown/malformed ⇒ the `radius` key
  is OMITTED (silence, the meta envelope law's failure direction) and the graph recompute is never
  attempted; the post-recall hook's once-per-recall graph load is gated the same way.
- Post-merge census hook built zero receipts on every real merge (BUG-034). git populates a hook
  subprocess's environment with `GIT_DIR`/`GIT_WORK_TREE` pointing at the invoking repo; an absolute
  `GIT_DIR` silently overrides `git -C <path>` targeting, so the census pipeline read the base/head
  worktrees against the WRONG repository and `build_graphs`' own base==named-SHA check tripped with
  an `EngineError`. The hook is fail-open by design, so this produced no error anywhere — a silent
  dark feed. Fixed in `hive-edge` 0.3.0: the environment is scrubbed of git's seven repo-discovery
  vars before every `git -C` subprocess (a `clean_git_env()` in hive-census, an independent
  byte-identical copy in matrix, both pinned by a cross-copy test), both post-merge hook templates
  `unset` those vars at the top (so the hook's own `GIT_DIR` reassignment cannot re-export into the
  build child), and `hive-edge census init` now self-tests the wiring under the hook's own
  `GIT_DIR`/`GIT_WORK_TREE` environment and reports `self-test PASSED`/`FAILED` at wiring time.
  `MIN_EDGE_VERSION` 0.2.0 → 0.3.0 force-propagates the fix (the pre-commit guard bumped the contract
  v.08 → v.09 and regenerated the keystone golden). The regression is reproduced first by unit tests
  that build under a forced hostile `GIT_DIR` and assert the stamp still names the targeted repo.
- `hive` CLI cross-platform portability (BUG-031/033). `_snapshot_to_host` now guards the operator
  chown-back behind `if hasattr(os, "getuid")`, so `hive reset`/`hive upgrade` no longer crash with
  `AttributeError` on native Windows (no POSIX uid/gid, and the Docker Desktop bind is already
  host-owned — the step was both impossible and unnecessary there); Linux and macOS are unchanged.
  `hive connect`'s remote breadcrumb prints a shell-neutral literal `Authorization: Bearer
  <seat-token>` instead of bash `${HIVE_TOKEN}` — `claude mcp add` bakes the header at add-time and
  the line is copy-pasted on an unknown OS/shell, so any expansion syntax (`${VAR}` / `$env:VAR` /
  `%VAR%`) was wrong on two of three shells; the teammate now substitutes the real token by hand.
  Pinned by `test_reset_skips_chown_back_on_non_posix` and the updated `test_connect_renders_mcp_add_line`.
  (The companion GNU-only `mktemp --suffix=` fix that silently killed the macOS post-merge receipt —
  BUG-032 — landed in the `hive-edge` hook renderer; the regenerated `.githooks/post-merge` picks it
  up on the next `hive-edge census init`.)
- `HIVE-ADMIN.md` §8 no longer claims the post-merge hook bakes "resolved absolute paths": the
  hook's bytes are constant and it resolves binaries + device config at run time, so wiring succeeds
  on any device (inert without the `hive` CLI) — the docs now state the per-device semantics, and a
  new "State directory" paragraph documents `~/.hive-edge/` (`HIVE_EDGE_HOME` override, contents,
  safe-to-delete), previously undocumented (BUG-028). `skills/hive-connect-team` /
  `skills/hive-operate` and `README.md`'s edge section carry matching one-liners.
- Bench `claude -p` calls loaded the operator's Claude Code hooks: a user- or
  project-level Stop hook appends its own prompt turn after the answer, and the json
  `result` (the last assistant text) became the hook-turn reply instead of the
  measured answer — every real success/refusal axis read garbage while the offline
  suite stayed green (the real-CLI-only class; hook sibling of the MCP-subprocess
  leak). `ClaudeSubscriptionLLM` now passes `--setting-sources ""` (no user/project/
  local settings ⇒ no hooks; OAuth unaffected, unlike `--bare` which drops keychain
  auth). Replay caches stay valid (the cache key is prompt/system/model-based).
- `hive token` / `hive tokens` / `hive revoke` failed on a zero-config install (no `.env`):
  `authctl` alone fail-fasted when `HIVE_STORE__DB_PATH` was unset, instead of defaulting to the
  in-container `/data/shared.db` like its sibling tools (entrypoint / healthcheck / backupctl).
  Because the `hive` CLI execs `authctl` with no `--db`, the token verbs exited `EX_CONFIG` and
  `hive status` reported `seats: unknown`. `authctl` now defaults the store path the same way;
  an explicit `--db` or `$HIVE_STORE__DB_PATH` still overrides it.
- The served agent contract (`initialize.instructions` + tool `description` fields) silently
  overflowed the MCP client's 2048-char per-field truncation cap — a first-connect agent never
  received the install procedure, rules block, or hooks (`SERVER_INSTRUCTIONS` alone had grown to
  22KB). `hive/app/onboard_ref.py` now single-sources one canonical `CONTRACT_FLOOR` string into
  both capped delivery channels, fitting the new `METADATA_FIELD_LIMIT = 2048` invariant by
  construction with real margin, and relocates every un-compressible install detail — the full
  procedure, the claude-code hooks JSON, the auto-approve allowlist, the edge-CLI reference, the
  identity reference, and the per-kind capture taxonomy — to a new proven-uncapped channel:
  `hive_health(include_onboarding=true)`. `CONTRACT_VERSION` v.06 → v.07.
- `README.md`'s onboarding paragraph claimed "Nothing is written into a rules file" — false: the
  served floor directs `hive_health(include_onboarding=true)`'s optional persistence block to be
  written verbatim into the agent's project rules file, and this repo's own `CLAUDE.md` carries
  exactly that block. Reworded to say nothing is written automatically at connect, and that
  installing the block is an agent-driven step.
