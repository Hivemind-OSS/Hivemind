# CONNECT-REMOTE-REPO-SKILL — plan

Add a shipped operator skill that guides an admin through connecting the server-side census
sync daemon to the team's GitHub repo, then tests that the connection actually works — walking
the operator through any missing prerequisite, and auto-detecting the default branch itself.

## 1. Objective

A new agent-runnable runbook-skill `connect-remote-repo` (sibling to `hive-bringup`,
`hive-connect-team`, `hive-backup-restore`, `hive-operate`) that:

1. **Detects prerequisites** and, when they are missing, walks the operator through completing
   them (repo URL, a read-only token for a private remote, a sync-capable server image).
2. **Auto-detects the repo's default branch** (main vs master vs other) itself — the agent runs
   `git ls-remote --symref <url> HEAD` and sets `HIVE_CENSUS__CANONICAL_REF` from the result;
   the operator never has to name the branch.
3. **Arms** the feed by writing the `HIVE_SYNC__*` keys to `.env` and restarting the server.
4. **Tests the connection** — confirms the daemon authenticated to GitHub and the feed is live
   (a healthy `sync` block in `hive_health`), and diagnoses the failure modes when it is not.

This is the runbook form of `HIVE-ADMIN.md` §4's arming process. `hive-connect-team` already
defers repo-arming to it ("the operator arms `HIVE_SYNC__REPO_URL` … §4"); today that process
lives only in the human doc, with no runnable skill.

## 2. Scope / non-goals

**In scope:** the SKILL.md runbook + the mandatory shipped-skill doc updates. **No code
changes** — the skill orchestrates existing pieces (`.env`, `hive` CLI restart via
`hive-bringup`, `git ls-remote`, the `hive_health` MCP tool). This keeps it low-risk and means
the THEORY §9 *code* checklist does not bind; the binding contract is the shipped-skills
doc-consistency rule (CLAUDE.md) plus code-truth accuracy.

**Explicitly not doing:**
- **No new `hive` CLI verb** (e.g. `hive connect-repo`). The user asked for a *skill*; a skill
  (agent executes the runbook) satisfies "performs this process" without a code change + its
  test surface. A one-command CLI verb is a possible future, noted, not built here.
- **No driving the server image upgrade.** If the running server predates the sync daemon
  (shipped in contract v.13; the live dogfood server is v.12), deploying the sync-capable image
  is a separate release operation. The skill *detects* the gap and stops with guidance — it does
  not attempt the upgrade/cutover.
- **No full verified-promotion E2E** as the connection test. Proving the promotion loop needs a
  PR with tests touching an anchored memory (repo-specific, contrived). The instant test is
  "connection + feed healthy"; a deeper "watch the next merge / open a throwaway PR" validation
  is offered as an optional follow-up.

## 3. Design decisions (with rationale)

- **Name / location:** `skills/connect-remote-repo/SKILL.md`, honoring the user's requested
  name. (The existing set is `hive-`-prefixed; `hive-connect-repo` would sit beside
  `hive-connect-team`. Flagging the deviation — will use `connect-remote-repo` unless you prefer
  the prefix.)
- **Boundary vs neighbors:** `connect-remote-repo` = the one-time *connect + test* flow;
  `hive-operate` = *ongoing* watch/tune of an already-armed feed; `hive-connect-team` = connect
  *agents/teammates* (not the repo). The skill cross-references `HIVE-ADMIN.md` §4 as the
  authoritative long-form (not duplicated) and `hive-bringup` for the restart mechanics.
- **Branch auto-detection = the pre-arming connectivity probe (validated).**
  `git ls-remote --symref <authenticated-url> HEAD` returns `ref: refs/heads/<branch>\tHEAD`
  (verified against a public repo → `master`). For a private repo the same probe, with the token
  in the URL, *also proves the URL + token authenticate before we ever touch `.env` or restart* —
  so detection de-risks the disruptive restart. The agent sets `HIVE_CENSUS__CANONICAL_REF` to
  whatever the probe reports (never hardcoding main/master), which is strictly better than the
  daemon's origin/HEAD fallback because the canonical-scoped `last_verified` freshness rider only
  derives from receipts stamped with that ref.
- **Restart is required and gated.** Config is read at boot (`entrypoint.py` calls `start_sync`
  after serve-ready); arming needs a reboot. Restarting a live memory server is disruptive
  (memory persists in the `hive-data` volume, so it is safe, but sessions drop) — the skill
  **confirms before restarting** (Law 6 directional safety) and defers the actual restart to
  `hive-bringup`.
- **Connection-test pass criteria (definitive signal):** after restart,
  `hive_health(include_census_health=true)` must show a `sync` block with `tracked_ref` = the
  detected branch, `last_tip` populated (a fetch landed), and no `last_error` /
  `status: "sync stalled"`. Give it one poll interval (default 60 s) and re-check.
- **Failure-mode diagnosis** the runbook must cover, each with the fix: partial config boot
  fail-fast (token/webhook set without `repo_url` → `EX_CONFIG`); **no sync block at all after
  restart** = the image predates the daemon (upgrade first); `last_error` auth failure = bad/
  expired token; `last_error`/stalled = unreachable remote / no egress; an SSH URL silently won't
  authenticate (HTTPS-only — no key is provisioned); a non-PR / test-less repo → verified
  promotion stays dark by design (only coarse `change_outcome` accrues) — set that expectation.
- **Secret hygiene:** the token goes only into `.env` (gitignored — verified). Never `hive_capture`
  it, never print the literal token, prefer referencing `${HIVE_SYNC__TOKEN}` over pasting it, and
  rely on the daemon's own redaction (`_redact`) + volume-only remote-config storage. Use a
  least-privilege read-only token (fine-grained PAT, Contents: Read, which also exposes
  `refs/pull/*/head`).
- **No `gh` / no GitHub App / no per-device hooks.** The connection is plain `git`-over-HTTPS
  (the only runtime apt package in the image) with the token in the remote URL. The skill must
  reflect the server-side census model (BUG-042: the old per-device `hive-edge census init` hook
  is deleted) — nothing to wire per device.

## 4. The runbook structure (SKILL.md)

Frontmatter: `name: connect-remote-repo` + a trigger-rich `description` ("Use when asked to
connect / arm / wire the server to a GitHub repo, turn on the automatic census feed, …").

Sections:
1. **What this does / when to use** — arm + test the server-side feed; boundary vs
   `hive-operate` / `hive-connect-team`; pointer to `HIVE-ADMIN.md` §4.
2. **Preflight (walk-through the missing pieces)** — server up? (else `hive-bringup`); repo URL?
   token for a private remote? (mint steps: GitHub → fine-grained PAT → Contents: Read);
   sync-capable image? (contract-version note + the definitive post-restart check).
3. **Detect the default branch** — the `git ls-remote --symref` probe (doubles as the auth
   check); set `HIVE_CENSUS__CANONICAL_REF`.
4. **Arm** — write `HIVE_SYNC__REPO_URL` (+ `HIVE_SYNC__TOKEN`, + canonical ref) to `.env`;
   preserve existing keys; never echo the token.
5. **Restart to load it** — confirm, then hand to `hive-bringup`.
6. **Test the connection** — poll `hive_health(include_census_health=true)`; pass criteria + the
   failure-mode table.
7. **Optional: push-triggered early wake** — `HIVE_SYNC__WEBHOOK_SECRET` + `hive up --tunnel` +
   the GitHub webhook (Settings → Webhooks → `/census-webhook`, HMAC secret); latency only.
8. **Load-bearing invariants** — unarmed is byte-inert; arming needs a restart; token lives only
   in the mirror git config on the volume (least-privilege); detect-only, never a trust mutation;
   one repo per server; HTTPS-only; verified promotion needs PR flow + runnable tests.

## 5. Files

**Create:** `skills/connect-remote-repo/SKILL.md`.

**Modify (same change — the shipped-skills doc contract):**
- `skills/README.md` — index-table row + the activation `for s in …` loop list + intro count.
- `README.md` — repo-tree line 181 (`… backup/restore, operate` → add `connect-repo`) and the
  skill mentions near lines 50–52.
- `llms.txt` — line 20 skill list (`bringup, connect-team, backup-restore, operate`).
- `HIVE-ADMIN.md` — §4: add a pointer to the new skill (the skill points to §4; §4 should point
  back — "absent counts as stale").

## 6. Design review / pressure-test

- **Law 6 (directional safety):** the one destructive step is the restart — gated behind an
  explicit confirm; everything else is additive/reversible. ✔
- **§9.9 byte-inert-when-unarmed & O7 detect-only:** the skill states unset = inert and the feed
  never mutates trust — it must not overclaim the feed as a trust action. ✔
- **Law 5 (durable truth):** the skill notes the mirror is a rebuildable cache; losing it never
  opens a gap. ✔
- **Secret rule (§9 #11):** token only in gitignored `.env`, never captured/printed. ✔
- **BUGS regression check:** BUG-042 (dead `census init` per-device model) — the skill must show
  the server-side model, no per-device hooks. BUG-038/039 (single-source-root fuel death) are
  SOLVED, but they teach that "clone succeeded" ≠ "decisions produced," so the deeper-validation
  note frames verified promotion as needing real decided PR runs. No open sync-path bug conflicts.
- **Top failure mode to get right:** an operator on the v.12 image arms `.env`, restarts, and
  nothing happens (config silently ignored — no daemon in the binary). The skill's post-restart
  "no sync block ⇒ upgrade the image first" diagnosis is the guard that turns a silent no-op into
  a clear next step. This is the single most important correctness element.

## 7. Verification

- **Code-truth pass:** every command/claim in the skill checked against the source (sync.py,
  config.py, entrypoint.py, http_server.py) — the code is truth.
- **Branch-detection command:** already validated live (`git ls-remote --symref` →
  `ref: refs/heads/master`); re-confirm the parse in the skill matches.
- **Doc consistency:** run `/audit-docs --changed` over the touched docs; confirm the
  `skills/README.md` index, the activation loop, `README.md`, and `llms.txt` all name the new
  skill (no drift); confirm frontmatter parses (`name` + `description`).
- **Honest limitation:** a live end-to-end connection test needs the v.13 sync-capable server +
  a real GitHub repo/token — neither is available in this session (live server is v.12). The
  skill is authored, code-truth-verified, and doc-consistent here; the live connect+test is the
  deploy-time validation the operator runs by following it.

## 8. Resolution & status (IMPLEMENTED)

- **Skill name:** RESOLVED — `hive-connect-repo` (operator chose the `hive-` prefix for index
  consistency beside `hive-connect-team`).
- **Built:** `skills/hive-connect-repo/SKILL.md` + index/reference updates in `skills/README.md`,
  `README.md`, `llms.txt`, `HIVE-ADMIN.md` §4. No code changes. Skill-set enumerations verified
  consistent; every claim checked against the source (code is truth); branch-detection command
  validated live.
- **Deferred (deploy-time):** a live end-to-end connect+test needs the v.13 sync-capable server +
  a real GitHub repo/token — run by following the skill once that image is deployed.
