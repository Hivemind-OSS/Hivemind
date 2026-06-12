# Changelog

All notable changes to this project are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]
### Added
- Outcome credit (HIVE-ORIGIN; supersedes the unreleased CV6 mirror-scan flow in
  this same cycle): `hive origin <owner/repo>` is one-command end-to-end setup —
  token ladder (`--token-stdin` > `$GITHUB_TOKEN`/`$GH_TOKEN` > `gh auth token` >
  public-repo mode), repo-access validation, 0600 `~/.config/hive/origins.json`
  (token inline; `$GITHUB_TOKEN` overrides at sync), ONE marker-tagged `@hourly`
  crontab line, first 90-day backfill. `hive origin sync|ls|rm` operate on linked
  origins. The scanner (`hive.tools.originctl`) is a stateless GitHub-API poller
  over merged reality: merged-PR wins keyed `merge_commit_sha` with trailers
  harvested from the PR's constituent commits (squash-safe by construction),
  direct-push wins, rebase twins dropped by trace-claim dedup, reverts flipping
  wins to losses one-way (`SqliteEpisodeStore.settle_loss` — monotone under
  stateless hourly re-scans). Trailer v2 `Hive-Credit: <trace_id> <episode_id> …`
  is SELECTIVE (only memories that materially shaped the committed code; nothing
  if none did); ingest credits claimed ∩ served-on-that-trace (`unserved_claims`
  counted, written nowhere). The `task_outcomes` v2 ledger and its idempotent
  `(commit_sha, episode_id)` upsert carry over unchanged.
- `hive_recall` hits now carry `"credit": {"wins", "losses"}` from the outcome
  ledger (`outcome_stats_for_episodes` behind a getattr feature-probe — annotation
  only; ranking unchanged, the utility flip stays keystone-gated).
- Rules block v2 (`block_version=2`): selective-credit section with an exact
  copyable trailer example; `producer.stamp_trailer` default is now `Hive-Credit`.
  Old `Hive-Trace` stamps stay inert but are counted as `legacy_trailers_seen`
  every sync (the re-onboard nudge); `eval_membrane.strip_stamped_tokens` strips
  BOTH trailer generations.
### Removed
- `hive credit`, `hive/tools/creditctl.py`, and the mirror-scan deployment (the
  `--mirror` clone + cron + squash-settings discipline + aged-unsettled alarm) —
  replaced wholesale by `hive origin`: the GitHub API serves constituent-commit
  trailers directly, so squash survival needs no repo settings and unsettled
  states don't exist (merged reality only).
- `hive` operator CLI (`hive/tools/cli.py`, stdlib-only, `[project.scripts]` entry
  point; uninstalled: `python -m hive.tools.cli`): `up [--tunnel]` (bounded
  health-wait; tunnel secrets fail-fast), `down`, `logs`, `nuke` (typed confirm),
  `status` (health + tunnel + seat count), `token`/`revoke`/`tokens` (shell to the
  in-container authctl — crypto + schema stay single-sourced), `connect` (teammate
  MCP registration line; transport only, never `hive_init`).
- `authctl list` subcommand + `SqliteTokenStore.labels()` — seat inventory, labels
  only, never hashes.
- Mechanical memory lifecycle (AUTONOMY-PLAN v2): `hive_capture` lands insights
  quarantined (embedded, structurally unservable); measured recall-miss demand from
  ≥1 non-writer identity auto-promotes to `provisional`, served WITH its trust label;
  TTL decay retires unused quarantined/provisional rows; `hive_write(replaces=)` is
  the human supersession path (the only retirement of `established`).
- `recall_misses` + `evidence_events` tables; exposure rows now carry `agent_id` and
  refresh the served rows' liveness clock in the same transaction.
- Recall hits carry `trust` + `ts`; `hive_fetch` annotates a superseded row's
  terminal successor; `hive_health` adds `trust_counts`, `n_misses_7d`,
  `include_gaps` (clustered demand-gap report) and `manifest_outdated`.
- `autonomy` config group (`enabled` restart-tier; demand/TTL knobs hot-swappable).
- Hook manifest v2: capture-without-asking via `hive_capture`; `correction` hook for
  `hive_write(replaces=)`.
### Changed
- `task_outcomes` replaced (clean-store, boot-guarded): the dormant Phase-0 clawback
  shape (`task_ref`/`trace_id` PK, state machine, diff-text columns) gives way to one
  settled win|loss row per `(commit_sha, episode_id)`; `settled_exposures_since` now
  reads it directly (`repo` is the family carrier, settle clock = `ingested_ts`).
- Tool surface is now exactly 6 verbs (+`hive_capture`); serving is decided by the
  single `lifecycle.is_servable` predicate (established, or fresh provisional) at the
  scan, the index sync, the pipeline resolve step, and the per-hit belt.
- Boot order: decay sweep runs before the index rebuild.
### Fixed
### Removed
- `hive.sh` — replaced by the `hive` CLI; its up-not-run-rm contract lives on as a
  pytest argv assertion.
- No migration/backfill path ships (clean-store start, human decision): an
  old-format `episodes` table is refused at store construction.
