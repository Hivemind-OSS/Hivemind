# Changelog

All notable changes to this project are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]
### Added
- `hive backup` — a manual durability verb (the in-container `hive.tools.backupctl`
  entry runs the existing `run_daily_backup` and prints the snapshot path).
- Self-serve onboarding: the `hive_health` tool description carries the static,
  marker-delimited rules block a connected agent writes into its primary rules file
  (CLAUDE.md / AGENTS.md / …) and skips re-touch when the marker is already present —
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
### Changed
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
- Tool surface is now exactly 4 verbs (`hive_write` / `hive_capture` / `hive_recall` /
  `hive_health`); serving is decided by the single `lifecycle.is_servable` predicate
  (established, or fresh provisional) at the scan, the index sync, the pipeline resolve
  step, and the per-hit belt.
- Boot order: decay sweep runs before the index rebuild.
### Fixed
### Removed
- `autonomy.solo_mode` + `autonomy.solo_min_span_days` — demand-promotion is now ONE
  identity-diversity rule for solo and team alike. A solo dev's independent agents each carry a
  distinct per-session identity, so their shared demand promotes with no flag (the elapsed-span
  bypass is gone, making anti-gaming strictly stricter). The `solo_hint` is kept but repurposed:
  it now flags single-identity traffic and points to `hive connect` / `X-Hive-Agent-Id`.
- `hive.sh` — replaced by the `hive` CLI; its up-not-run-rm contract lives on as a
  pytest argv assertion.
- No migration/backfill path ships (clean-store start, human decision): an
  old-format `episodes` table is refused at store construction.
