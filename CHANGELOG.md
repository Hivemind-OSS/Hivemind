# Changelog

All notable changes to this project are documented here.

## Added
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
- `hive token` / `hive tokens` / `hive revoke` failed on a zero-config install (no `.env`):
  `authctl` alone fail-fasted when `HIVE_STORE__DB_PATH` was unset, instead of defaulting to the
  in-container `/data/shared.db` like its sibling tools (entrypoint / healthcheck / backupctl).
  Because the `hive` CLI execs `authctl` with no `--db`, the token verbs exited `EX_CONFIG` and
  `hive status` reported `seats: unknown`. `authctl` now defaults the store path the same way;
  an explicit `--db` or `$HIVE_STORE__DB_PATH` still overrides it.
