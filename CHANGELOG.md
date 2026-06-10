# Changelog

All notable changes to this project are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]
### Added
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
- Tool surface is now exactly 6 verbs (+`hive_capture`); serving is decided by the
  single `lifecycle.is_servable` predicate (established, or fresh provisional) at the
  scan, the index sync, the pipeline resolve step, and the per-hit belt.
- Boot order: decay sweep runs before the index rebuild.
### Fixed
### Removed
- No migration/backfill path ships (clean-store start, human decision): an
  old-format `episodes` table is refused at store construction.
