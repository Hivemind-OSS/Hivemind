# HIVE-ORIGIN-PLAN — `hive credit` → `hive origin`: GitHub-API credit loop, agent-selected memories, one-command setup

**Status: LANDED IN FULL** · drafted 2026-06-12 · landed 2026-06-12 (chunks O1–O7)
**Supersedes:** the CV6 mirror-scan credit flow (`creditctl.scan_repo` + `hive credit`); the
ingest pipe, `task_outcomes` schema, and store substrate are KEPT.
**Design review:** /software-design-review run 2026-06-12 — verdict "shape right, fits seams";
two findings folded in (rebase double-credit dedup; legacy-trailer counter). BUGS.md: empty.

## 0. Goal

Close the memory→outcome feedback loop with the least operator work possible:

- `hive origin <owner/repo>` is **end-to-end**: resolve token → validate repo access →
  persist origin config → install ONE hourly cron line → run a first 90-day backfill sync.
- Agents credit **only memories that materially shaped the committed code** via a new
  trailer `Hive-Credit: <trace_id> <episode_id> [...]` (selective, not whole-exposure-set).
- Scanner is a **stateless GitHub-API poller** (no mirror, no cursor): merged reality only.
- Loop closure consumer: recall envelope hits carry `"credit": {wins, losses}`. No ranking
  change — the utility flip stays keystone-gated. The whole path stays non-load-bearing.

Out of scope (deferred, deliberately): ranking/boost changes, keystone/readiness changes,
non-GitHub repos (mirror scanner deleted; git history retains it), cursor state, GitLab.

## 1. Locked design decisions

1. **Trailer v2:** `Hive-Credit: <trace32hex> <episode_id> [<episode_id>...]` — strict parse
   (first token 32-hex, rest positive ints; commas tolerated as separators; a line failing
   the grammar is counted `malformed_trailer_lines`, never guessed). Multiple lines per
   message allowed. Key single-sourced from `producer.stamp_trailer` (default flips to
   `"Hive-Credit"`). Old `Hive-Trace` trailers become inert (v1 wording promised "changes
   no reward", so no contract break) but are **counted** as `legacy_trailers_seen` in every
   sync report — the re-onboard nudge.
2. **Selective credit with an integrity line:** ingest credits `claimed ∩ exposures_by_trace(trace_id)`
   — only memories actually served on that trace can be credited (anti-gaming; also recovers
   the real `recall_margin` from the exposure row). Claims outside the served set are counted
   `unserved_claims`, written nowhere.
3. **Win sources (stateless 14d window, hourly):**
   a. **Merged PRs** (primary): `pulls?state=closed&sort=updated&direction=desc` paginated to
      cutoff; keep `merged_at >= cutoff`. Trailers harvested from `/pulls/{n}/commits`
      (GitHub keeps `refs/pull/N/head` — squash-safe **by construction**, no repo-settings
      dance, no aged-unsettled alarm). Win rows keyed `merge_commit_sha`, ts = `merged_at`.
   b. **Direct pushes**: default-branch `commits?since=cutoff` listing; trailer commits whose
      **trace set is not already claimed by a PR row this scan** become win rows keyed their
      own sha. Trace-claim dedup (NOT sha set-difference) is what kills the rebase-merge
      double-credit: rebase rewrites constituent shas, so only the trace ids are stable.
      Dropped copies counted `deduped_rebase_copies`.
4. **Losses:** same commits listing, `This reverts commit <40hex>` → loss row per named sha.
   `record_outcome` keeps `ON CONFLICT DO NOTHING`; a NEW one-way store method
   `settle_loss(commit_sha)` flips that sha's win rows to loss. Wins processed before losses
   within a batch. DO-NOTHING insert + one-way flip = **monotone** under stateless hourly
   rescans (no win/loss oscillation; fixes the landed CV6 gap where a revert arriving after
   the win's ingest was silently swallowed). Revert-of-revert re-lands under a NEW sha → new
   win rows; correct.
5. **Cron + config:** one marker-tagged user-crontab line per HOST (sync iterates all
   configured origins):
   `@hourly cd <checkout> && set -a && . ./.env && <sys.executable> -m hive.tools.cli origin sync >> ~/.config/hive/origin-sync.log 2>&1 # hive-origin`
   `cd` first ⇒ `python -m` works installed or not; `. ./.env` ⇒ HIVE_TENANT_ID + compose
   interpolation under cron's bare env. Link verb validates `compose.yaml` exists at cwd AND
   `.env` contains/env carries `HIVE_TENANT_ID` (fail EX_CONFIG with the one-line fix).
   Origins config at `$XDG_CONFIG_HOME|~/.config/hive/origins.json`, **0600**, token inline —
   documented deviation from the env-var rule (cron has no env); `$GITHUB_TOKEN` at sync time
   **overrides** the stored token; `ls` never prints tokens. Token ladder at link:
   `--token-stdin` > `$GITHUB_TOKEN`/`$GH_TOKEN` > `gh auth token` > none (public-repo mode).
6. **Verb dispatch:** `hive origin <target>` — target containing `/` or `github.com` ⇒ link;
   else reserved word `sync|ls|rm` (repo args always carry `/`, so the grammar is
   collision-free; rule stated in parser help + EX_USAGE message). Flags: `--lookback DAYS`,
   `--token-stdin`, `--no-cron`. All origin verbs stay behind the tenant gate.
7. **Module:** new `hive/tools/originctl.py` (stdlib-only top level; injectable `fetch` seam;
   container ingest half follows the authctl `main(argv, env, connect_fn, out, stdin)`
   contract). `creditctl.py` + its git-fixture tests are DELETED in the CLI-migration chunk;
   ingest tests are ported to the v2 row shape.
8. **Store methods land on the concrete `SqliteEpisodeStore` only** (precedent:
   `record_outcome`/`exposures_by_trace` are adapter-only; no Protocol widening, so no
   port-conformance fan-out). Schema UNCHANGED (`settle_loss` rides the PK prefix;
   stats ride `idx_task_outcomes_episode`). `repo` column now carries `owner/name`.
9. **Consumer:** `_handle_recall` annotates each post-belt hit with
   `"credit": {"wins": w, "losses": l}` via a `getattr` feature-probe (absent on fakes ⇒
   skip; present-but-raising ⇒ log + omit field — never a blanket except that masks wiring).
10. **Rules block v2** (`block_version` 1→2 at the container composition root): credit section
    rewritten — exact example line (examples-as-spec), "ONLY memories that materially shaped
    the committed code; nothing if none did". Already-linked repos keep v1 until re-onboarded
    (HOWTO documents the re-link nudge + the `legacy_trailers_seen` counter surfaces it).
11. **eval_membrane** de-confounding strip handles BOTH keys (history carries old trailers).

## 2. NDJSON row + report shapes (the host↔container contract)

```jsonc
// win
{"kind":"win","commit_sha":"<sha>","commit_ts":1760000000,"repo":"owner/name",
 "credits":[{"trace_id":"<32hex>","episode_ids":[12,34]}]}
// loss
{"kind":"loss","commit_sha":"<sha>","commit_ts":1760003600,"repo":"owner/name"}
```

Scan summary (host stderr): `{win_rows, loss_rows, credit_groups, malformed_trailer_lines,
legacy_trailers_seen, deduped_rebase_copies, prs_scanned}`.
Ingest report (container stdout, one JSON line): `{wins_ingested, duplicates,
unknown_traces, unserved_claims, losses_flipped, totals}`.

## 3. Files changed & exact signatures

| File | Change |
|---|---|
| `hive/adapters/store_sqlite.py` | + `settle_loss(self, commit_sha: str, *, ts: int) -> int` (one-way `UPDATE … SET outcome='loss', ingested_ts=? WHERE commit_sha=? AND outcome='win'`, in `tx`); + `outcome_stats_for_episodes(self, episode_ids: Sequence[int]) -> dict[int, tuple[int, int]]` |
| `hive/tools/originctl.py` (NEW) | `TRAILER_KEY_DEFAULT="Hive-Credit"`, `LEGACY_TRAILER_KEY="Hive-Trace"`, `DEFAULT_LOOKBACK_DAYS=14`, `FIRST_SYNC_LOOKBACK_DAYS=90`; `Fetch = Callable[[str, Mapping[str,str]], tuple[int, Mapping[str,str], bytes]]`; `parse_repo_arg(target: str) -> str`; `parse_trailer_lines(body: str, *, trailer_key: str) -> tuple[list[tuple[str, list[int]]], int]`; `scan_github(repo: str, *, token: Optional[str], now: int, lookback_days: int = 14, trailer_key: str = TRAILER_KEY_DEFAULT, fetch: Optional[Fetch] = None) -> tuple[list[dict], dict]`; `ingest(store: Any, rows, *, now: int) -> dict`; `main(argv=None, *, env=None, connect_fn=None, out=None, stdin=None) -> int` |
| `hive/tools/creditctl.py` | DELETED (chunk 4, together with CLI rewire) |
| `hive/tools/cli.py` | `_credit` → `_origin` family: `_origin` (dispatch), `_origin_link`, `_origin_sync`, `_origin_ls`, `_origin_rm`; helpers `_origins_path(env)`, `_read_origins`/`_write_origins` (0600), `_resolve_token(args, env, run)`, `_install_cron(run, env, *, checkout)` / `_remove_cron(run, env)` (marker `# hive-origin`, strip-then-append idempotent); parser: positional `target`, optional `extra`, `--lookback`, `--token-stdin`, `--no-cron`; container exec target → `hive.tools.originctl` |
| `hive/app/config.py` | `ProducerConfig.stamp_trailer` default `"Hive-Trace"` → `"Hive-Credit"` |
| `hive/app/onboard.py` | `_BLOCK_TEMPLATE` credit section v2 (selective wording + exact example line) |
| `hive/app/container.py` | `block_version=1` → `2` (line ~265) |
| `hive/app/mcp_server.py` | ctor default `trailer_key="Hive-Credit"`; `_handle_recall` hit dicts gain `"credit"` via feature-probe |
| `hive/research/eval_membrane.py` | trailer strip covers both keys |
| `tests/tools/test_originctl.py` (NEW) | scan + ingest contracts (below); `test_creditctl.py` DELETED, ingest cases ported |
| `tests/…` (existing) | onboard block-render tests updated for v2 wording/version; mcp recall test + degrade test; cli origin tests |
| `HOWTO.md` §4, `CLAUDE.md` (hive verb list), `docs/02-CONTRACTS.md` §0b, `CHANGELOG.md` | docs delta (chunk 7) |

New dependencies: **none** (stdlib `urllib.request`, `json`, `re`, `subprocess`; note the
groundcheck urllib-submodule false-positive — prove with a test run, don't rewrite imports).

## 4. Test contracts (TDD — written first per chunk)

**Store (chunk 1):** `settle_loss` flips only `outcome='win'` rows for the sha, returns
rowcount, in-tx; stats return `(wins, losses)` keyed eid with zero-default; conn built via
prod `connect()` (deferred-isolation memory).
**Scan (chunk 2, canned-`fetch` seam, zero network):** PR pagination stops at cutoff;
merged-only filter; constituent-commit trailer harvest keyed `merge_commit_sha` (squash
scenario = branch-deleted PR, commits still served by `/pulls/{n}/commits`); strict parse
(32hex+ints, comma tolerance, malformed counted); direct-push win; **rebase copy dropped by
trace-claim dedup** (same traces via PR ⇒ branch-listing twin suppressed, counted); revert ⇒
loss row; `legacy_trailers_seen` counts old-key sightings; `parse_repo_arg` URL forms.
**Ingest (chunk 3):** claimed∩served (unserved counted, not written); unknown trace counted;
idempotent re-ingest (duplicates); merge+revert in ONE batch ⇒ wins land then flip;
**monotone: win → settle_loss → re-ingest same win ⇒ stays loss** (the CV6-gap pin);
loss for uncredited sha ⇒ 0 flips; `main` NDJSON/stdin/report + EX_CONFIG without db (ported).
**CLI (chunk 4, fake run/fetch):** link e2e writes 0600 config + installs cron line + first
sync runs (exec argv carries `originctl ingest`, NDJSON via `input=`); link twice ⇒ ONE cron
line; link from dir without compose.yaml ⇒ EX_CONFIG; missing tenant ⇒ EX_CONFIG; private
repo w/o token ⇒ EX_CONFIG with PAT-scope hint; `sync` env-token overrides stored; `rm` last
origin removes cron line; dispatch grammar (`owner/repo` vs `sync|ls|rm`, bad word ⇒ EX_USAGE).
**Convention v2 (chunk 5):** block renders trailer example + version marker 2; empty-trailer
fail-fast unchanged; eval_membrane strips both keys.
**Consumer (chunk 6):** hits carry credit counts; store without the method ⇒ hits unchanged,
no field; method raising ⇒ logged, field omitted, recall succeeds.

**Mutation protocol (RULE-2, per chunk before "done"):** named faults — `settle_loss` return
0 / drop `AND outcome='win'`; drop the `eid in served` intersection; drop trace-claim dedup;
drop cron strip-before-append; drop the loss-after-win batch ordering. Run under `timeout`,
foreground; restore via Edit (unique anchors); clear `__pycache__` after same-size restores.

## 5. Chunk order (each lands green + committed)

1. **Store substrate** — `settle_loss` + `outcome_stats_for_episodes` (+tests, mutation).
   Additive, nothing calls them yet.
2. **originctl scan half** — parse helpers + `scan_github` + summary (+hermetic tests).
   New module, no callers.
3. **originctl ingest half** — `ingest` + `main` (+ported/extended tests). Container half
   complete; old flow still live.
4. **CLI migration** — origin verb family; exec target flips to originctl; DELETE
   `creditctl.py` + `test_creditctl.py` (same chunk — deleting the module while its tests
   exist would break collection; cli import is the last consumer).
5. **Trailer convention v2** — config default + rules block v2 + `block_version=2` +
   mcp_server default + eval_membrane both-keys (+test updates). After 4 so the scanner
   reading the new key exists before agents are told to emit it.
6. **Recall envelope credit annotation** (+degrade tests).
7. **Docs** — HOWTO §4 rewrite (origin one-command setup, PAT scopes: classic `repo` or
   fine-grained Contents:Read + Pull requests:Read; >lookback outage ⇒ `--lookback` backfill;
   GitHub-only note; log path), CLAUDE.md verb list, 02-CONTRACTS §0b delta, CHANGELOG;
   `graphify update .`.

Why this order is safe: 1–3 are purely additive behind no callers; 4 swaps the CLI atop a
landed, tested scanner+ingest; 5 changes agent-facing defaults only after the reader exists;
6 reads a table only 3–4 write; 7 documents what shipped.

## 6. Risks & accepted trade-offs

- **Cron gap > lookback loses wins permanently** — accepted (non-load-bearing path); 90d
  first-sync backfill + `--lookback` recovery + HOWTO note.
- **Rebase-merge loss flips are partial** (revert names rebased shas, wins keyed merge sha)
  — best-effort loss labels, documented; same class as CV6.
- **Agent attribution noise** — selective credit trades coverage for precision; right trade
  (false credit poisons ranking worse than missed credit).
- **Token at rest in 0600 file** — documented deviation (cron has no env); env override at
  sync; never logged/printed.
- **Already-onboarded seats keep stamping the inert old key** until re-link — surfaced every
  sync via `legacy_trailers_seen`.
- **GitHub-only** — local/other-host repos lose the loop; mirror scanner recoverable from
  git history if ever needed.
