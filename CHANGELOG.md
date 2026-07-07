# Changelog

All notable changes to this project are documented here.

## Added
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
- `autonomy.solo_mode` + `autonomy.solo_min_span_days` — demand-promotion is now ONE
  identity-diversity rule for solo and team alike. A solo dev's independent agents each carry a
  distinct per-session identity, so their shared demand promotes with no flag (the elapsed-span
  bypass is gone, making anti-gaming strictly stricter). The `solo_hint` is kept but repurposed:
  it now flags single-identity traffic and points to `hive connect` / `X-Hive-Agent-Id`.
- `hive.sh` — replaced by the `hive` CLI; its up-not-run-rm contract lives on as a
  pytest argv assertion.
- No migration/backfill path ships (clean-store start, human decision): an
  old-format `episodes` table is refused at store construction.

## Fixed
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
