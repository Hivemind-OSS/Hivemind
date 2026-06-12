# Changelog

All notable changes to this project are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]
### Added
- Outcome credit (CONVERGENCE CV6): `hive credit [path]` scans a clone/mirror's
  `Hive-Trace:` trailer commits host-side (win = ancestor of main, loss = named by a
  revert on main, else unsettled — ancestry only, never diff text) and pipes settled
  rows into the in-container `creditctl ingest`; idempotent `(commit_sha, episode_id)`
  upsert into the new `task_outcomes` v2 ledger feeds readiness → keystone →
  `utility_rerank`. Scan summary carries an aged-unsettled squash-leak alarm.
  Canonical deployment: one `git clone --mirror` + cron on the server host (HOWTO.md).
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
