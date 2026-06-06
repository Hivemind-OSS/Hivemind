# M07 Onboarding / hive_init  (onboard)

**One-line:** A first-run onboarding handshake that turns a typed, content-hashed InstallPlan into a self-wired agent (rules-file block + commit-trailer convention) confirmed by a lie-proof content hash recorded in the existing kv meta store, plus deterministic teardown.sh / import.sh scripts that archive-not-delete the old cortex system and re-embed-from-text its corpus into the new store.
**Port disposition:** BUILD-NEW (the hive_init handshake, the InstallPlan/link-record domain, teardown.sh, import.sh) layered on PORT surfaces: hive_health is PORT+EXTEND of service.py:1354 (add optional `linked`/`link` fields to the HealthSnapshot TypedDict, types.py — byte-identical no-link path, tool count stays 11 per mcp_server.py:200); the link record persists via PORT of SqliteMeta.set/get UPSERT kv (persistence.py:1148-1163 — NO new table); the trailer_key is sourced single-origin from the producer (C10, M-producer) not hard-coded; import.sh is a thin CLI wrapper over PORT+SIMPLIFY of ProjectionMigrator.reembed_from_text (ops/migration.py:184, the verbatim-text re-embed-through-new-PCA path, dropping bi-temporal/supersession columns on the way in per §12). hive_init itself sits ENTIRELY ABOVE the three swap ports (embedding / vector-index / outcome-producer) and touches none of them — it does NOT write producer config (the rejected pure-B coupling); at most it emits a WARN note if repo_path is not yet in producer.watch_repos.

---

## M07 — Onboarding: hive_init + container bootstrap + teardown/import

### 1. Responsibility (one deep module behind a narrow surface)

This module owns the **first-run journey** that turns a freshly `docker compose up`'d Hivemind container into a *linked, self-wired* project, plus the two operational scripts that decommission the old AgentCortex system and seed the new store from it. The narrow surface is **one new MCP tool (`hive_init`)**, an **additive extension of `hive_health`**, and **two shell entry points (`teardown.sh`, `import.sh`)**. The meaningful work hidden behind that surface:

- Render the **exact marker-delimited rules-file block** the agent installs into the harness's primary rules file (CLAUDE.md / AGENTS.md / .cursorrules / …) — the block teaches *when to `hive_write`*, the *`Hive-Trace` commit-trailer convention* (so move-#6 attribution works the moment a producer is configured), and the *approve-in-native-chat protocol* (`hive_pending`/`hive_approve`/`hive_reject`).
- Drive a **two-phase, content-hash-confirmed, idempotent handshake** so the recorded link **cannot lie** about which block content actually landed.
- Persist the link in the **existing `meta` kv store** (no new table) and surface it through `hive_health` so the onboarding sequence ends with a verifiable `linked: true → ready`.
- `teardown.sh`: **archive (mv, not rm)** `~/cortex`, disable+remove the 11 systemd `cortex-*` units, and **strip only the cortex hooks** from the global `~/.claude/settings.json` — idempotent and reversible.
- `import.sh`: **re-embed-from-text** the archived old store into the new one through the new PCA head, landing rows `status='pending'`.

The keystone design move (winning synthesis): **B's typed-InstallPlan skeleton + B's +1-tool / extend-health / no-new-table economy + B's single-source trailer_key, hardened by A's content-hash phase-2 confirm and A's liveness/healthcheck rigor, MINUS B's premature producer-enrollment coupling.** `hive_init` sits *entirely above* the three swap ports and writes no producer config.

### 2. Public surface + ENFORCED contract

**`hive_init(repo_path: str, confirm_hash: Optional[str] = None) -> dict`** — two-phase:

- **Phase 1 (`confirm_hash is None`)** — *pure except read-only probes*. Resolves the primary rules file (ordered candidate list, first existing wins, else CLAUDE.md→AGENTS.md fallback), detects hook support for this harness, renders a `RulesBlock` whose `trailer_key` is read from `producer.stamp_trailer` (the single source), and returns an `InstallPlan` JSON carrying `expected_confirm_hash == rules_block.block_hash`. **Postcondition: zero writes** (meta kv unchanged). This is the "agent is the universal installer" prompt, but as a *typed contract that cannot lie* rather than a prose block.
- **Phase 2 (`confirm_hash` given)** — the agent has written the block; the server **recomputes the expected `block_hash` and requires `confirm_hash == block_hash`**.
  - **match** → upsert a `LinkRecord` into meta kv at key `hive_init:link:<repo_path>`; return `{linked: True, link: {...}}`. Exactly one write.
  - **mismatch** → return `{linked: False, error: 'stale_or_wrong_block', expected: <hash>}`; **zero rows written**. The recorded link can never claim a block content that was not installed.
  - **Idempotent**: re-running phase-2 with the same hash is a no-op upsert.

**Invariants (enforced, not prose):**
- `RulesBlock.__post_init__` makes an ill-formed block (missing markers, missing version embed, or `block_hash != sha256(rendered_text)`) **unconstructable** — a contract that cannot lie.
- `trailer_key` is **always** `producer.stamp_trailer`; it is never literal in the template (kills the CONFIG_DRIFT / silent-join-failure of §11/§6.1#6).
- The link record's `block_hash` is the **confirmed installed-block hash**, the round-trip credential (A's `test_phase2_stale_hash_refused` invariant).
- `hive_init` **never mutates `producer.watch_repos`** (or any producer config); the most it does about an unwatched repo is set `InstallPlan.watch_warning` (a WARN note). This is the deliberate cut of the rejected pure-B coupling — onboarding is a Phase-1 substrate concern, producer enrollment is a Phase-2-adjacent C10 concern behind its own swap port.

**`hive_health` extension** — `HealthSnapshot` (types.py, `TypedDict total=False`) gains optional `linked: bool` and `link: dict | None`; `health()` gains optional `repo_path: str | None = None`. **No `repo_path` ⇒ byte-identical to the ported payload** (honoring the "tool count stays 11" / no-token discipline — total tools become 12 only because of the one new `hive_init`). The two existing fail-fast early returns (service.py:1364, 1374) are unchanged and still omit the link keys.

**Precondition DESIGNED OUT, not documented:** "the agent must remember which block content it wrote" is designed out — the server recomputes and compares the hash, so the agent cannot mis-report a faithful install, and a partial/edited install is *refused* rather than silently linked.

**Container liveness (separated from the handshake):** `./hive up` is a thin liveness wrapper around `docker compose up` with a bounded `_wait_healthy` (hard timeout → dump logs → non-zero exit). The container `HEALTHCHECK` probes a `hive_health` that **touches the loaded embedder**, so "healthy" cannot be declared before the CPU model is resident (A's honest-weakness #4, adopted). Liveness (`./hive up`) and handshake (`hive_init`) are distinct concerns.

### 3. Swap seam disposition

`hive_init` is **not itself a swap port** and is the design's central restraint: it sits above the embedding / vector-index / outcome-producer ports and touches none of them. It *reads* `producer.stamp_trailer` (single-source the trailer convention) and *reads* `producer.watch_repos` (compute a warning) but writes neither — so swapping the outcome producer (the C10 port) requires **no change here**. The rules-file resolution is the one variation axis that legitimately belongs to onboarding (the **harness axis** — a `HookCatalog` of supported hook points); a new harness adds a `HookSpec`/candidate entry, not a core change. The link store is behind the existing `SqliteMeta` kv abstraction; moving links to a dedicated table later is a storage-adapter change, not a surface change.

### 4. Data owned

**No new table.** The link record is one namespaced JSON blob in the existing `meta(key, value)` kv store (persistence.py:1148 `SqliteMeta.set/get`, a verified UPSERT), under keys `hive_init:link:<repo_path>` and `hive_init:block_version`. This is strictly less surface than a `CREATE TABLE bootstrap(...)` under the single-writer CAS discipline (the rejected pure-A cost).

**Config keys read** (none owned exclusively, none written): `producer.stamp_trailer`, `producer.watch_repos`, `onboarding.rules_file_candidates` (ordered list), `onboarding.block_version`.

**Files owned (scripts, not executed this pass):** `teardown.sh`, `import.sh`, `./hive` (liveness wrapper), and the rendered rules-block template body.

### 5. Dependencies (and the boundary it must NOT cross)

- Depends on: `SqliteMeta` (link persistence), the config reader (for `producer.stamp_trailer` / `watch_repos` — read-only), `health()` (extend), and — for `import.sh` only — `ProjectionMigrator.reembed_from_text` (ops/migration.py:184).
- **Must NOT know about:** the embedding provider, the vector index internals, the outcome producer's tick / state machine, or the secret scanner's internals. It must **not write producer config** — the boundary is explicit: onboarding teaches the *trailer convention* so the producer can later join on it, but never enrolls a repo or mutates `watch_repos`. It must **not** implement its own recall/approval logic — it only *teaches* the approve-in-native-chat protocol in the rules block.

### 6. Failure-mode logging (structured JSON; secrets never logged)

| Boundary | Level | Context (no secrets) |
|---|---|---|
| Rules-file resolution: none of the candidates exist and CLAUDE.md created | INFO | `{repo_path, chosen_file, candidates_tried}` |
| Rules-file resolution: repo_path not a dir / unreadable | ERROR | `{repo_path, errno}` then fail-fast |
| Phase-1 render: `producer.stamp_trailer` missing/empty | ERROR | `{config_key:'producer.stamp_trailer'}` — fail fast (do not default-silently, per Secrets/fail-fast standard) |
| Phase-2 hash mismatch | WARN | `{repo_path, expected_hash, got_hash_prefix}` (hash only; never the block body if it could contain pasted secrets) |
| Phase-2 link upsert success | INFO | `{repo_path, block_version, block_hash, server_version}` (success checkpoint) |
| `watch_warning` emitted (repo unwatched) | WARN | `{repo_path, watch_repos_count}` — recoverable, note only |
| meta kv write failure (SQLite I/O) | ERROR | `{repo_path, sqlite_err}` then surface to caller |
| Container `_wait_healthy` timeout | ERROR | `{elapsed_s, last_health, compose_logs_tail}` then non-zero exit |
| teardown: unit disable/rm `|| true` skip | WARN | `{unit, reason:'not_present'}` (idempotent path) |
| teardown: settings.json hook strip | INFO | `{removed_commands[], preserved_commands[]}` |
| import: reembed stranded/quarantined episode | WARN | `{count, w_version}` (mirrors migration.py:271-273) |
| import: resume in-flight | WARN | `{w_version, reembed_inflight}` (migration.py:287) |

### 7. Port disposition vs §10 map

- `hive_init`, `InstallPlan`/`RulesBlock`/`LinkRecord` domain, `teardown.sh`, `import.sh` wrapper, `./hive` liveness — **BUILD-NEW**.
- `hive_health` — **PORT+EXTEND** of `service.py:1354` (`HealthSnapshot` TypedDict in types.py gains additive optional keys; no-link path byte-identical).
- Link persistence — **PORT** of `SqliteMeta.set/get` (persistence.py:1148; no new table).
- `import.sh` core — **PORT+SIMPLIFY** of `ProjectionMigrator.reembed_from_text` (ops/migration.py:184), dropping bi-temporal/supersession columns on the way in (§12 one-time import).
- MCP registration — **PORT+EXTEND** of `mcp_server.py` tool table (mcp_server.py:200 "tool count" discipline; +1 tool only).
- trailer convention — single-sourced from the **build-new** producer (C10) `producer.stamp_trailer`.

### 8. TEST CONTRACT (test-first; see `test_contract` field for the full list)

Covers, with file::test naming, the happy path, every §6 failure mode, and every §2 invariant:
- **RulesBlock invariants** (`test_rules_block.py`): markers+version present, hash==body, ill-formed block unconstructable, trailer single-sourced. **MUTATION** `test_mutation_trailer_drift`: hard-code the trailer literal → `test_trailer_key_is_single_sourced` MUST go red → restore → green (proves the join cannot silently diverge).
- **Phase-1 purity & resolution** (`test_hive_init_phase1.py`): returns InstallPlan with confirm credential, writes nothing, resolves primary rules file by priority, offers the universal git hook, emits `watch_warning` when unwatched **and proves producer config is unchanged** (guards against the rejected coupling), no warning when watched.
- **Phase-2 lie-proof confirm** (`test_hive_init_phase2.py`): good hash links; **stale hash refused with zero rows written**; idempotent re-link; persists via meta kv not a new table. **MUTATION** `test_mutation_confirm_compare`: flip `!=`→`==` → `test_phase2_stale_hash_refused` MUST go red → restore → green (proves the content-hash confirm is load-bearing — the A invariant B lacked).
- **Health link surfacing** (`test_health_link.py`): unlinked path byte-identical (no token cost), link reported when present, fail-fast early return still omits link keys, tool-count invariant (+1 → 12).
- **teardown.sh** (`test_teardown_sh.py`, sandbox HOME): archives-not-deletes (mv invariant), removes all 11 units, strips only cortex hooks (groundcheck/git-ai survive), idempotent, `--dry-run` mutates nothing, `--restore` round-trips (reversibility).
- **import.sh** (`test_import_sh.py`): re-embeds-from-text through new geometry, **lands rows `status='pending'`** (admission floor preserved — maps the §6.1#5b "pending never recallable" gate to import), drops bi-temporal columns, idempotent resume (reembed_inflight watermark).

**§6.1 acceptance-gate mapping:** this module is responsible for the *onboarding* half of §6.1#5b's admission floor at import time — `test_import_lands_pending` proves a seeded corpus is never auto-recallable. The end-to-end first-run sequence (clone → README → `./hive up` → MCP connect → `hive_init` phase-1 → write block → `hive_init` phase-2 confirm → `hive_health(repo_path)` → `linked:true`) is exercised by `test_phase2_good_hash_links` + `test_health_reports_link` together; no functional path on the handshake, the teardown, or the import is left untested, and every state-machine/credit-adjacent comparison (the phase-2 hash compare, the trailer single-source) carries a named mutation fault.

---

## Design review (independent pass)

**Verdict:** STRONG DESIGN, TEST CONTRACT NOT YET BUILD-READY. M07 is a genuinely deep module behind a narrow surface (+1 MCP tool, additive hive_health extension, two scripts) and its central design move — the two-phase content-hash handshake that makes the recorded link unable to lie (Principle 11 "define errors out of existence," applied to the agent's "did I install the right block?" precondition) — is exactly right for an agent-native installer. The deliberate cut of B's producer-enrollment coupling (hive_init reads producer.stamp_trailer / producer.watch_repos but writes neither) is the correct boundary and keeps the swap-port discipline intact (Principle 8, separate general/special; the C10 producer port is untouched). RulesBlock.__post_init__ making an ill-formed block unconstructable, and trailer_key always sourced from producer.stamp_trailer (never literal), are contracts-that-cannot-lie that kill the §11/§6.1#6 CONFIG_DRIFT silent-join-failure at the source. Scores reflect a clean structure; the binding constraint is the TEST CONTRACT, which has real coverage holes around the secret-safe invariant at import time, the cross-module status='pending' dependency, container-liveness behavior, and several §6 failure modes that carry no named test. Two MUST-FIX items block sign-off; the design itself needs only the watch_warning-vs-fail-fast precondition pinned down. Verified against the tree: SqliteMeta.set/get is a real idempotent UPSERT (persistence.py:1170), health() has exactly the two fail-fast early returns the spec references (service.py:1355/1364/1374) and is TypedDict total=False so additive keys are byte-identical on the no-repo_path path, the 'tool count stays 11' discipline is a real comment (mcp_server.py:200), reembed_from_text re-embeds verbatim blob text through a fresh PCA head with a reembed_inflight resume watermark (migration.py:184), and there is NO existing status/pending/approved column — confirming the staging schema import.sh writes into is BUILD-NEW elsewhere, a dependency this spec under-states.

**Scores (1–10):**
- design_complexity: 3
- cognitive_load: 4
- information_leakage: 3
- extensibility_fit: 8
- agent_navigability: 8
- contract_enforcement: 7
- test_coverage: 5

**Red flags:**
- Information Leakage @ the rules-block template body (owned by M07) vs the C10 producer's stamp_trailer convention vs the M06 approval protocol — the rendered block teaches the Hive-Trace trailer format AND the hive_pending/approve/reject protocol, both of which are owned by other modules. Mitigated correctly for the trailer (single-sourced from producer.stamp_trailer, with test_trailer_key_is_single_sourced + mutation), but the APPROVAL-protocol prose in the block is NOT single-sourced — if M06 renames a tool or changes the batch-approve shape, the template drifts silently with no test catching it. root: dependency → change amplification (a §8.2 surface change forces a coordinated edit in the M07 template body that no contract enforces).
- Prose-Only Contract on Tricky Semantics @ §2 the watch_warning-vs-fail-fast decision — the spec says hive_init 'never mutates watch_repos' and emits a WARN note for an unwatched repo, but whether an unwatched repo at phase-2 should still LINK (it does, per the design) vs warn-and-refuse is stated only in prose. The InstallPlan.watch_warning field is typed, but the rule 'link succeeds even when unwatched' is not encoded as an enforced contract or a named test beyond 'emits watch_warning'. root: obscurity → an agent reading the types cannot tell that a warning is non-blocking; add an explicit test that phase-2 links DESPITE an unwatched repo.
- Missing Feedback Signal @ ./hive up liveness wrapper and the container HEALTHCHECK that 'touches the loaded embedder' — this is a shell + Dockerfile concern with NO test in the Test Contract at all (§8 lists test_teardown_sh and test_import_sh but no test_hive_up / no healthcheck-probes-embedder test). The honest-weakness #4 claim ('healthy cannot be declared before the CPU model is resident') is an enforced-sounding guarantee with zero enforcement. root: obscurity → the load-bearing 'healthy implies embedder resident' invariant is prose-only; an agent editing the healthcheck has no failing test to catch a regression to a shallow ping.
- Hard to Describe @ the rules-file resolution path — 'ordered candidate list, first existing wins, else CLAUDE.md→AGENTS.md fallback' plus a per-harness HookCatalog/HookSpec is a multi-clause precondition. test_hive_init_phase1 claims to test 'resolves primary rules file by priority' but the Test Contract names only one assertion; the multi-candidate tie-break, the not-a-dir ERROR path (§6 row 2), and the create-CLAUDE.md-when-none-exist path (§6 row 1) are §6 failure modes with NO corresponding named test. root: obscurity → branchy resolution logic with under-named tests; the agent cannot prove the priority order is load-bearing.

**Test gaps:**
- test_import_scans_secrets — MISSING: no test that a credential planted in an archived old-store episode is refused/redacted on import (the secret-safe invariant is untested at the import boundary; §9 floor is bypassed by reembed-from-text). This is the single most important gap.
- test_hive_init_phase2 lacks a 'links DESPITE unwatched repo' assertion — the design says watch_warning is non-blocking but no test pins that phase-2 still returns linked:True when the repo is unwatched (only the phase-1 warning emission is tested).
- Rules-file resolution failure modes untested: §6 row 1 (no candidate exists ⇒ create CLAUDE.md, INFO) and §6 row 2 (repo_path not a dir / unreadable ⇒ ERROR fail-fast) have structured-log table entries but NO named test in §8. The branchy first-existing-wins tie-break is asserted only loosely.
- Phase-1 fail-fast on missing producer.stamp_trailer (§6 row 3, 'do not default-silently') has a structured-log entry and matches the Secrets/fail-fast standard, but NO named test asserts hive_init raises rather than rendering a block with an empty trailer — a silent-empty-trailer would reintroduce the exact CONFIG_DRIFT the module exists to kill.
- Container liveness has zero tests: ./hive up bounded _wait_healthy timeout→dump-logs→non-zero-exit (§6 row 8) and the HEALTHCHECK-touches-embedder guarantee (the adopted honest-weakness #4) are entirely untested. No test_hive_up_timeout, no test_healthcheck_requires_resident_embedder.
- meta kv write-failure path (§6 'meta kv write failure (SQLite I/O) ⇒ ERROR then surface to caller') has no named test — the phase-2 upsert is the one write in the module and its failure surfacing is unverified.
- teardown --restore round-trip is named (test_teardown_sh reversibility) but the partial-failure case (some units present, some absent; the '|| true' idempotent skip at §6 row 9) is only implied, not asserted as a distinct test — idempotency under partial prior teardown is the realistic re-run case.
- import idempotent-resume is named (reembed_inflight watermark) but there is no test that import does NOT double-credit / double-insert episodes on resume — the verifiable-credit / dedup-by-content_hash invariant at import is unasserted.
- Swap-seam coverage is asserted only negatively (test_phase1 proves producer config is unchanged) — good for the producer port, but there is NO test that swapping onboarding.rules_file_candidates (the one legitimate variation axis / HookCatalog) adds a HookSpec without a core change. The claimed harness-axis extensibility is unproven by any test.

**Must-fix:**
- TEST GAP (secret-safe invariant at import boundary): import.sh re-embeds the ENTIRE archived old store verbatim-from-text and lands it status='pending'. The §9 secret-scan floor is specified as the hive_write handler's job, but import.sh bypasses hive_write — it ports reembed_from_text, which does NOT scan. The Test Contract has NO test that a planted credential in an OLD-STORE episode is refused/redacted on import. status='pending' is NOT sufficient (a pending row is still persisted to disk + daily backups; §9 is explicit the substrate 'never persists a raw secret'). Add test_import_scans_secrets: plant an AKIA/sk- token in an archived episode, assert it is refused-or-redacted before landing, mutation-test by disabling the scan. Either route import through the secret scanner or state explicitly in the design why old-store rows are exempt — but the current spec neither tests nor justifies the gap.
- DESIGN+TEST GAP (cross-module dependency under-specified): import.sh's headline invariant 'lands rows status=pending' depends on a status column + staging machine that does NOT exist in the ported reembed_from_text (verified: no status/pending/approved in persistence.py) and is owned by a different module (M02/M06 store + write path). The spec ports reembed_from_text 'as core' but that function writes live/approved-equivalent rows with no status field and requires Capability.CONSOLIDATE + an admin_identity it does not mention. Pin the exact seam: which module owns the status='pending' default on the import write path, what admin identity import.sh runs under, and add test_import_lands_pending must assert against the REAL post-M06 schema, not a hypothetical. As written test_import_lands_pending cannot be authored test-first because the column it asserts on is out of this module's surface.
