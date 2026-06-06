# M11 Config + observability  (config)

**One-line:** One deep module that resolves the entire runtime configuration (layered defaults ← /data/hive.toml ← HIVE_GROUP__FIELD env) into a single frozen, fail-fast-validated Config object, owns the three swap-seam provider registries (embedder / vector-index / outcome-producer) as plain greppable dicts, enforces reload-tier (D/C/B/A) safety as a state machine that refuses unsafe hot-swaps, injects the never-hallucinate floor (H_frac_max) into the abstention gate by frozen-object identity to kill CONFIG_DRIFT, and provides the cross-cutting observability spine (structured JSON logging at every boundary, the hive_health snapshot, and the daily SQLite backup with retention).
**Port disposition:** PORT+SIMPLIFY + two targeted BUILD-NEW fixes, against three reference files. (1) config.py (CortexConfig) — PORT+SIMPLIFY: keep the frozen group-dataclass tree, from_flat, the TOML/env layering, the RELOAD_TIER data table, and __post_init__ fail-fast validation; DELETE the production/LIVE_GEOMETRY dual-default machinery, the _LEGACY_FLAT_TO_GROUP back-compat block + every _flat_property, ConsolidationConfig/PrivacyConfig/SecAggConfig/FederationConfig/SemanticConfig/GovernanceConfig/DaemonConfig groups, and the CORTEX_<FIELD>.upper() env mapping (the verified collision bug at config.py:762-773). BUILD-NEW grafts: (a) HIVE_GROUP__FIELD double-underscore env namespacing replacing the upper-case collision; (b) an enforced reload() tier-guard state machine over the existing RELOAD_TIER pure data; (c) plain-dict provider registries keyed by '<group>.<provider>'; (d) the cfg.recall frozen object passed BY IDENTITY to the single abstention gate. config.py reference Reload-tier table at config.py:385, the validation block at config.py:587-613, and the env-collision bug at config.py:757-781 are all real and cited. (2) ops/observability.py (JSONFormatter + configure_json_logging) — PORT as-is (observability.py:25-97); it already emits the engineering-standard fields and rotates. (3) ops/dr.py backup() + prune_backups() (dr.py:54, dr.py:185) — PORT as-is (online sqlite3.backup() snapshot + keep-N retention). hive_health — PORT+SIMPLIFY of service.py:1354 health() into a config-owned HealthSnapshot that drops cold/schemas/procedures and adds the embedder-resident + producer-tick fields. Per spec §10 this module is the cross-cut that the §10 'Config controller (stays gated)', 'Backup (ops floor)', and 'Migration (tier-D)' rows all touch; it is build-new only where the reference carried a verified bug or greenfield-irrelevant machinery.

---

# M11 — Config + Provider Registry + Observability

> Build-ready module spec. Greenfield package `hive`. Ports from `cls_memory` per the
> §10 map. Every signature, DDL-equivalent, config key, and test name below is concrete.
> Grounded in: spec `HIVEMIND_VMIN_SPEC.md` §4 (hyperparameters/tiers), §4.3 (authoritative
> index), §4.6 (ops), §4.8 (producer knobs), §5-D3 (CONFIG_DRIFT), §6.4 (logging), §10
> (reuse map); and reference `config.py` (CortexConfig, RELOAD_TIER@385, validation@587-613,
> env-collision bug@757-781), `ops/observability.py` (JSONFormatter@25, configure_json_logging@54),
> `ops/dr.py` (backup@54, prune_backups@185), `serving/service.py` health()@1354, `types.py`
> HealthSnapshot@459, `ops/telemetry.py` (the sink this config gates), `storage/persistence.py`
> SqliteMeta@1148 + `_env_int`@44.

---

## 1. Responsibility (one deep module behind a narrow surface)

This is the **composition-root substrate** every other component reads but none of them
re-derives. The meaningful work it hides:

1. **Resolve one frozen `Config`** from a 4-layer precedence stack
   (group defaults ← `/data/hive.toml` ← `HIVE_*` env ← explicit overrides), validated
   **fail-fast** so an illegal store geometry or a gate-disabling floor can never be
   constructed. The surface is one method, `Config.load(db_path=...)`; the depth is the
   whole layering + coercion + validation machine.
2. **Own the three swap seams as data.** `EMBEDDING_PROVIDERS`, `INDEX_PROVIDERS`,
   `PRODUCER_PROVIDERS` are plain `dict[str, Callable]`; a config key (`embedding.provider`,
   `index.backend`, `producer.provider`) selects the adapter constructor. Adding a provider
   is one dict entry + one adapter file with **zero core change** — the spec §10 swap mandate
   made literal and greppable (all providers visible at one registration site, not hidden
   behind decorator magic).
3. **Enforce reload-tier safety.** `RELOAD_TIER` is pure data (`"group.field" → "D|C|B|A"`);
   `reload(old, new)` is a state machine that **refuses** a tier-C/D hot-swap with the
   migration instruction instead of silently corrupting a warm store. This turns the
   reference's documentation-only tier table into an enforced contract
   (define-errors-out-of-existence).
4. **Kill CONFIG_DRIFT structurally.** The single `cfg.recall` *frozen object* is passed
   **by identity** into the one abstention gate, so the never-hallucinate floor
   (`H_frac_max`) physically cannot have two values. (One gate today; the §8.3 cascade gate
   is deferred — the test pins identity so the future second gate cannot copy the float.)
5. **Provide the observability spine** the global engineering standard mandates: structured
   JSON logging configured once at startup; a `hive_health` snapshot; and the daily SQLite
   backup with retention — the one ops floor.

It is *deep* because the surface is `Config.load(...)` + `build_*` + `configure_json_logging`
+ `health` + `run_daily_backup`, while behind it sits all of layering, coercion, validation,
tier enforcement, provider wiring, log formatting, health probing, and online backup. A caller
never reasons about precedence, env coercion, or tier rules — it asks for the resolved object.

---

## 2. Public surface + ENFORCED contract

The full surface is in `interface_block`. Contract details and invariants:

### 2.1 `Config` (frozen) + `Config.load`
- **Frozen** (`@dataclass(frozen=True)`) on the root and every group ⇒ an immutable snapshot
  the server can hot-swap atomically. Mutation raises `FrozenInstanceError` — a hot-path
  caller cannot accidentally mutate the floor.
- **`load` precedence (low→high):** group defaults < `/data/hive.toml` < `HIVE_<GROUP>__<FIELD>`
  env < explicit `**overrides`. `db_path` is **required** (no silent default — fail fast per the
  Secrets standard); `tenant_id` defaults to `"default"` (constant label, single-tenant).
- **Env namespacing — the BUILD-NEW fix.** Variables are `HIVE_<GROUP>__<FIELD>` (double
  underscore separating group from field), e.g. `HIVE_RECALL__H_FRAC_MAX=0.4`,
  `HIVE_GEOMETRY__D=384`, `HIVE_INDEX__BACKEND=hnsw`. This **eliminates the verified reference
  bug** (`config.py:757-781`) where upper-casing collided `d`/`D` into one `CORTEX_D` and
  skipped both with a WARN, leaving the two most migration-dangerous tier-D fields unsettable
  from env — unacceptable in a 12-factor single-image Docker product whose own standards
  mandate env config. Each value is coerced to the field's declared type
  (int/float/bool/str/Optional); a non-coercible value is skipped with a WARN (never crashes).
- **`__post_init__` fail-fast validation (the contract that cannot lie):**
  - `0.0 < recall.H_frac_max <= 1.0` else `ValueError` (the never-hallucinate floor; a 0.0 or
    >1.0 would silently disable the gate).
  - `embedding.st_projection_head == "pca"` else `ValueError` (random JL is rejected — measured
    worse at every d; spec §4.1).
  - `index.backend in {"exhaustive"} | set(INDEX_PROVIDERS)` else `ValueError` listing valid keys.
  - `embedding.provider in EMBEDDING_PROVIDERS`, `producer.provider in PRODUCER_PROVIDERS` else
    `ValueError` (fail fast on a typo'd seam at construction, not at first recall).
  - `producer.epsilon_explore > 0` else `ValueError` (§4.7 guardrail-1 — ε must stay >0 so novel
    memories get exposure; a 0 silently starves the loop).
  - `geometry.d > 0`, `geometry.W_version >= 1`, `retention.backup_keep >= 1`.
- **Units:** `*_s`/`*_days` are seconds/days; `*_bytes` bytes; `H_frac_max` a unitless fraction
  of `ln(N_eff)`; rewards unitless on the Beta-Bernoulli scale.
- **Precondition DESIGNED OUT, not documented:** "don't set two different floors" is made
  *unrepresentable* by passing the frozen `cfg.recall` object by identity (§2.3), not by a
  docstring warning.

### 2.2 Provider registries + `build_*`
- `EMBEDDING_PROVIDERS`/`INDEX_PROVIDERS`/`PRODUCER_PROVIDERS`: `dict[str, Callable[..., Port]]`.
- `build_embedder(cfg)`/`build_index(cfg)`/`build_producer(cfg)`: look up by the config key,
  call the constructor with the relevant group, **fail fast** (`KeyError`→re-raised as a clear
  `ValueError` with the available keys) at startup on an unknown key. Postcondition: the returned
  object satisfies its port Protocol (`runtime_checkable`).
- `build_index` postcondition: the returned `VectorIndex.is_authoritative` is `True` for
  `backend="exhaustive"` — the spec §4.3 "exhaustive is AUTHORITATIVE, never silently flips to
  ANN" property is a build-time assertion here, closing the `approx_threshold` trap at the seam.

### 2.3 The CONFIG_DRIFT floor (designed-out precondition)
The abstention gate (C4, owned by another module) is constructed with `cfg.recall` **passed by
object identity**, not `cfg.recall.H_frac_max` copied as a float. Invariant: `gate._recall is
cfg.recall`. With one gate this is trivially drift-free; the contract test + its mutation pin the
identity so the future §8.3 cascade gate cannot reintroduce two floors. This is the synthesis
DECISION (reject B's Derived god-object for a one-gate system; pass the frozen object by identity
— A's zero-machinery mechanism).

### 2.4 `RELOAD_TIER` + `reload`
- `RELOAD_TIER: dict[str,str]` — pure, greppable data, one entry per settable field. Tiers per
  spec §4: D=re-embed/migration, C=restart, B=hot-swap, A=next-run.
- `diff_tier(old, new) -> "A".."D"`: the **strictest** tier among changed fields governs.
- `reload(old, new) -> Config`: returns `new` for tier A/B (hot-swap/next-run safe); raises
  `TierViolation` for tier C/D carrying the exact remediation ("restart the server" / "bump
  geometry.W_version and run the re-embed migration"). This is the enforced state machine that
  the reference left as prose.

### 2.5 Observability surface
- `configure_json_logging(level, file, max_bytes, backup_count, stream)`: idempotent; wires the
  `JSONFormatter` to the `"hive"` root logger; called once at server start. (PORT of
  `observability.py:54`.)
- `health(cfg, store, embedder, producer) -> HealthSnapshot`: cheap, poll-safe; **never raises**
  — fail-soft to `{ok=False, error, db_path}` on any probe failure. Includes `embedder_resident`
  (False until the model is loaded — so a container HEALTHCHECK can't be called healthy before
  the model is in RAM) and `index_authoritative`.
- `backup(src, dest)` / `prune_backups(dir, keep)` / `run_daily_backup(cfg)`: online
  `sqlite3.backup()` snapshot (WAL-safe) + keep-N retention. (PORT of `dr.py`.)

---

## 3. Swap seam

This module **is** the registry that backs the three mandated swap seams; it does not itself
sit behind a port, but it *defines* how the other three are selected.

| Seam | Config key | Default adapter | A second adapter must implement |
|---|---|---|---|
| **Embedder** (C1) | `embedding.provider` | `"local_st"` → `LocalSTEmbedder` (bge-small + PCA) | the `TextEmbedder` Protocol (`encode(str)->value[d]`, unit-norm, `encode==encode_batch[0]`); register one dict entry `EMBEDDING_PROVIDERS["remote_loopback"]=...`. No core file changes. |
| **Vector index** (C5/C3) | `index.backend` | `"exhaustive"` → `ExhaustiveIndex` (`is_authoritative=True`) | the `VectorIndex` Protocol (`scan_approved`-fed, `is_authoritative`, `rebuild_from_store`); register `INDEX_PROVIDERS["hnsw"]=...`. |
| **Outcome producer** (C10) | `producer.provider` | `"git_inproc"` → in-process `OutcomeProducer.step()` | the `OutcomeProducer` Protocol (`step(now)->ProducerTick`); register `PRODUCER_PROVIDERS["sidecar"]=...`. |

**Proof the swap needs no core change:** core modules import only the *port Protocol* and call
`build_embedder(cfg)` / `build_index(cfg)` / `build_producer(cfg)`. The provider string is data;
the constructor is a dict value. `test_second_adapter_swaps_with_no_core_change` registers a new
embedding adapter and flips `embedding.provider` while importing **only** `registry.py` + the new
adapter file — proving no core edit. This is why a plain dict beats the reference's decorator
registry (B's `ProviderRegistry` was rejected in the config DECISION as an agent-native
implicit-wiring hop; the dict is greppable in one read).

---

## 4. Data owned

**Tables/blobs:** none of its own at the episode level. It **reads** the SQLite file at
`runtime.db_path` (for `health` counts via the Store port) and **owns the backup artifacts**
under `retention.backup_dir` (default `<db_dir>/backups/`), pruned to `retention.backup_keep`.

**Cross-process meta:** `last_producer_tick_ts` and `W_version` for the health snapshot are read
from the existing **`SqliteMeta`** kv UPSERT store (`persistence.py:1148`) — no new table, per the
onboarding DECISION's "no new table" economy (the producer writes its tick ts there; health
reads it).

**Config keys read:** the complete owned set is enumerated at the bottom of `interface_block`
(every `group.field` across the 9 groups). Env override syntax: `HIVE_<GROUP>__<FIELD>`.
Infra-tier knobs (`HIVE_SQLITE_*` busy/backoff, like the reference `_env_int` reads at
`persistence.py:44`) stay in the storage module — this module is the **Python-runtime tuning
surface only**, matching the reference's deliberate boundary.

---

## 5. Dependencies (and the boundary it must NOT cross)

- **Depends on:** the three **port Protocols** (`TextEmbedder`, `VectorIndex`, `OutcomeProducer`)
  for type hints only — never their concrete adapters (adapters are imported lazily inside the
  registry dict values / `build_*`, so importing `config` does not import torch). The **Store
  port** (read-only) for `health` counts. Python stdlib `tomllib`, `logging`, `logging.handlers`,
  `sqlite3`, `os`.
- **Must NOT know about:** the *internals* of any adapter (no `import torch`, no
  `sentence_transformers` at module top — the embedder adapter owns that), the abstention-gate
  *algorithm* (it only hands the gate the frozen `cfg.recall` object), the SQL of the episode
  schema (it reaches the store only through the read-only port), and the producer's git-parsing
  logic. **Named boundary:** Config is *upstream* of every port and *below* the abstention gate;
  it wires, it does not compute. It must never reach into `core/` domain logic — that would make
  the composition root a god-object (the rejected "Derived" layer).

---

## 6. Failure-mode logging (per the engineering standard; secrets never logged)

Every boundary logs structured JSON (`JSONFormatter`) with `ts/level/logger/message` + context
extras (`request_id`/`trace_id` where a flow has one). Levels follow the standard. **No secret,
credential, or PII is ever logged — only identifiers, paths, counts, and the field name.**

| Boundary | Level | What is logged (context) |
|---|---|---|
| Config load: missing `/data/hive.toml` | **info** | `"no hive.toml at %s; env-only config"` + path |
| Config load: malformed TOML | **warn** | `"hive.toml ignored (%s)"` + exception *type name only* (no values) |
| Config load: env coercion failure | **warn** | `"HIVE_%s not coercible to %s; ignored"` + field name + type (NOT the raw value — could carry a secret) |
| Config load: unknown env key | **warn** | `"unknown env override %s ignored"` + key name |
| Config validation failure | **error** | the `ValueError` message (field + bound) — value is a bound/enum, never a secret |
| `build_embedder` model load start/done | **info** | `"loading embedder %s"` / `"embedder resident %s in %.2fs"` (model name + load time — long-running op, per the Performance standard) |
| `build_embedder`/`build_index`/`build_producer` unknown provider | **error** | `"unknown <seam> provider %r; valid=%s"` (fail-fast at startup) |
| `build_index` non-authoritative selected | **warn** | `"index backend %s is approximate; exact-eval recall not guaranteed"` (the §4.3 trap is loud) |
| `reload` tier-C/D refused | **warn** | `"reload refused: %s is tier %s; %s"` (field, tier, remediation) |
| `health` probe DB error | **error** | `"health: store probe raised %s"` + type name + db_path (fail-soft, returns ok=False) |
| Producer idle (no watch_repos) — surfaced in health | **warn** | `"producer idle: no watch_repos (loop starved, not broken)"` (§4.8) |
| Abstain decision (the gate logs, but the floor it reads is config's) | **debug** | `"abstain H/lnN=%.3f > floor=%.3f"` (entropy + floor — diagnostic, no text) |
| Secret-scan refusal (write path, config provides the patterns) | **warn** | `"write refused: secret-pattern %s at offset %d"` — pattern *name* + offset, **never the matched bytes** |
| Backup ok / failure | **info / error** | `"backup ok dest=%s bytes=%d dt=%.3fs"` / `"backup failed %s"` + type name |
| Telemetry sink disabled (init failed) | **warn** | `"telemetry disabled (%s)"` + type name |

`test_secret_never_logged` greps this module's own emitted boundary lines for `sk-`/`AKIA`/`ghp_`
and asserts none appear (CLAUDE.md §6 + spec §6.4).

---

## 7. Port disposition vs the §10 reuse/delete map

| Concern | Reference file | Disposition |
|---|---|---|
| Config tree | `config.py` `CortexConfig` | **PORT+SIMPLIFY** — keep frozen groups + `from_flat`/`load` layering + `RELOAD_TIER` (config.py:385) + `__post_init__` validation (config.py:587-613). **DROP** the `production`/`LIVE_GEOMETRY` dual-default machinery, `_LEGACY_FLAT_TO_GROUP` + every `_flat_property`, and the 9 greenfield-irrelevant groups (consolidation/privacy/secagg/federation/semantic/governance/daemon + their tier rows). |
| Env mapping | `config.py:757-781` `_env_flat` | **BUILD-NEW** — replace the upper-case `CORTEX_<FIELD>` (the verified `CORTEX_D` collision bug) with `HIVE_<GROUP>__<FIELD>` namespacing. |
| Reload-tier enforcement | `config.py:385` (data only) | **BUILD-NEW** — the `reload()` tier-guard state machine over the ported pure-data table. |
| Provider registry | (decorator-style implied) | **BUILD-NEW** — plain `dict[str,Callable]` registries (rejected the reference/Approach-B decorator class per the config DECISION). |
| JSON logging | `ops/observability.py:25,54` | **PORT as-is** — `JSONFormatter` + `configure_json_logging` already emit the standard fields and rotate. |
| Health | `serving/service.py:1354` + `types.py:459` | **PORT+SIMPLIFY** — drop cold/schemas/procedures; add `embedder_resident`, `index_authoritative`, `producer_watch_repos`, `last_producer_tick_ts`. |
| Backup | `ops/dr.py:54,185` (`cortex-backup.timer`) | **PORT** — online `sqlite3.backup()` + `prune_backups`; the one ops floor (spec §4.6). |
| Metrics registry (`InMemoryMetricsRegistry`) | `observability.py:103` | **DROP for MVP** — minimize to health only (spec §10 "Diagnostics → health only"); re-add if/when a Prometheus surface is scoped. |

---

## 8. TEST CONTRACT (first-class, written test-first)

The full list with exact assertions + the failure each catches is in `test_contract`.
Coverage map (no functional path untested):

- **Happy path:** `test_defaults_match_spec_geometry`, `test_build_*_default`,
  `test_tier_A_hot_swap_allowed`, `test_json_formatter_emits_standard_fields`,
  `test_health_ok_shape`, `test_backup_roundtrip`.
- **Every §6 failure mode →** a test: missing/malformed TOML, env-coercion failure, unknown env
  key, validation failures (5 named), unknown provider (fail-fast), tier C/D refusal, health DB
  error (fail-soft), producer-idle surfaced, secret-never-logged, backup retention.
- **Every §2 invariant →** a test: floor bounds (`test_h_frac_max_*`), pca-only
  (`test_projection_head_random_rejected`), ε>0 (`test_epsilon_explore_must_be_positive`),
  precedence order (`test_layering.py`), env namespacing closes the collision
  (`test_env_namespacing_d_vs_no_collision`), authoritative index
  (`test_health_index_authoritative_true`), floor-by-identity (`test_gate_reads_same_frozen_recall_object`),
  swap-with-no-core-change (`test_second_adapter_swaps_with_no_core_change`).

**Mutation tests (RULE 2) — four, each mapping to a §6 acceptance gate:**

1. **Reload tier guard** (spec §4.1 tier-D corruption). FAULT: weaken `reload()`'s guard from
   `tier in ("C","D")` to `tier == "D"`. RED: `test_tier_C_restart_refused`. Restore → green.
   Proves the tier guard blocks unsafe swaps structurally.
2. **CONFIG_DRIFT floor** (spec §5-D3). FAULT: wire the gate with `cfg.recall.H_frac_max` (float
   copy) instead of the frozen `cfg.recall` object. RED: `test_gate_reads_same_frozen_recall_object`.
   Restore → green. Proves the never-hallucinate floor is shared by identity, not value.
3. **Backup retention** (spec §4.6). FAULT: `prune_backups` sorts ascending before slicing. RED:
   `test_prune_keeps_n_most_recent` (deletes the newest, keeps the oldest). Restore → green.
4. **Env namespacing regression** (the verified `CORTEX_D` bug). `test_env_namespacing_d_vs_no_collision`
   is the standing regression: under the old upper-case scheme `geometry.D` was unsettable from
   env; this test fails the moment anyone reverts to the colliding mapping.

**Acceptance-gate responsibilities this module owns:**
- §6.4 *failure-mode logging* — `tests/obs/test_logging.py` proves every boundary logs and
  secrets never do.
- §4.3 *authoritative index / approx_threshold trap* — `test_health_index_authoritative_true` +
  `build_index` postcondition assertion.
- §5-D3 *CONFIG_DRIFT* — `tests/config/test_floor_identity.py` + mutation #2.
- §4.1 *tier-D warm-store corruption* — `test_tier_D_migration_refused` + mutation #1.
- §4.6 *backup ops floor* — `tests/obs/test_backup.py` + mutation #3.

A reviewer can say "no functional path is untested": every public method, every validation
branch, every boundary log, and every swap seam has a named test, and the four riskiest
mechanisms (tier guard, floor identity, retention, env namespacing) each have a mutation proof.


---

## Design review (independent pass)

**Verdict:** STRONG DESIGN, NOT YET BUILD-READY. M11 is a genuinely deep module behind a narrow surface (Config.load + build_* + configure_json_logging + health + run_daily_backup hides 4-layer precedence, type coercion, fail-fast validation, the tier state-machine, provider wiring, log formatting, health probing, and online backup) — APOSD #4/#6 applied well, and it correctly turns three reference documentation-only constructs (the CORTEX_D env-collision bug verified at config.py:757-781, the prose tier table at config.py:385, the CONFIG_DRIFT floor) into enforced contracts that 'cannot lie' per agent-native §6. The plain-dict registries (rejecting the reference decorator class) are the right Implicit-Wiring call for an agent reader, and the swap-with-no-core-change test is the load-bearing proof of the SWAPPABILITY mandate. BUT three things block sign-off: (1) the entire enforceable surface and the entire Test Contract are deferred to two companion blocks (`interface_block`, `test_contract`) that are NOT present in the repo or this deliverable — a reviewer cannot verify a single signature or assertion, so 'no functional path untested' is asserted, not demonstrated; (2) the `producer.epsilon_explore > 0` validation is grounded in §4.7 guardrail-1, but that ε is a RECALL-time knob (belongs in the `recall` group, consumed by C3), not a producer knob — the spec's `producer.*` group (§4.8) has no `epsilon_explore` field, so M11 validates a non-existent field while leaving the guardrail that actually matters (recall ε>0) unvalidated — a CONFIG_DRIFT-class misplacement of the very floor M11 exists to protect; (3) the floor-identity invariant (`gate._recall is cfg.recall`) reaches across M11's own stated boundary (§5 says M11 'hands the gate the frozen cfg.recall object' but 'must NOT know about the abstention-gate'), making the gate's constructor call site a hidden cross-module contract M11 cannot enforce alone — the mutation test pins it but the design owes one explicit named owner for who constructs the gate with that object.

**Scores (1–10):**
- design_complexity: 3
- cognitive_load: 4
- information_leakage: 3
- extensibility_fit: 8
- agent_navigability: 7
- contract_enforcement: 6
- test_coverage: 5

**Red flags:**
- Prose-Only Contract on Tricky Semantics @ §2.3 floor-identity invariant (`gate._recall is cfg.recall`) — the cross-module 'pass the frozen object by identity, never the float' rule is stated in prose + one mutation test, but the constructor call site that must honor it is unspecified, so the contract can silently drift if a future gate module copies the float. root: dependency → an agent editing the gate constructor has no type/signature forcing object-identity; the floor can re-fork.
- Hidden cross-module knowledge @ §2.2 `build_index` postcondition asserting `VectorIndex.is_authoritative is True` for backend=exhaustive — M11 here encodes the §4.3 authoritative-vs-ANN semantics of the index it claims (§5) not to understand internally. The assertion is correct and valuable, but it is index-domain knowledge living in the config module. root: dependency → the approx_threshold trap rule now lives in two places (the index module AND M11's build assertion); if the index's authoritative-flag semantics change, M11's postcondition must move in lockstep. Mild and deliberate — keep it as belt-and-suspenders but name it as shared knowledge.
- Scattered Truth (agent-native §9) @ the deferred companion blocks + repeated env syntax — the config-key set is 'enumerated at the bottom of interface_block', the env syntax is restated in §2.1, §3, and §4, and RELOAD_TIER lives partly here and partly in reference config.py:385. With truth spread across this spec + two absent blocks + the reference file, an agent must reconcile 4 sources to learn the real key set. root: obscurity → until interface_block is inline and single-source, an agent burns budget deciding which enumeration to trust.
- Special-General Mixture (minor) @ §6 logging-table row 'Secret-scan refusal (write path, config provides the patterns)' — M11 is declared the Python-runtime tuning surface that 'wires, does not compute' (§5), yet here it is said to OWN the secret-pattern set the write-path/C6 handler computes against. Per §9 + §10 the secret scan is BUILD-NEW in C6 and the patterns are a security policy, not a runtime-tuning knob. root: dependency → putting the credential patterns in M11 leaks security policy into the composition root; if they live in C6 they should not appear in M11's ownership table. Pick one owner and stop straddling.
- Overexposure risk @ `configure_json_logging(level, file, max_bytes, backup_count, stream)` ported as-is from observability.py:54 — the common case (server start, JSON to stderr at INFO) is forced to coexist with rotation params most callers never set. Reference defaults them, so it is borderline, but the ported signature exposes 5 knobs for a one-call-at-startup function. root: obscurity → an agent reading the call must decide about rotation it does not care about. Minor; flag only — the defaults keep the common case one-arg.

**Test gaps:**
- The Test Contract itself is unverifiable: `test_contract` (the block holding 'file::test + exact assertion + the failure it catches' for every test named in §8) is absent. Every §8 claim ('every public method, every validation branch, every boundary log has a named test') is a test NAME with no assertion body; the four mutation tests name a fault and a red test but no block proves the test as written would actually go red. Coverage cannot be signed off from names alone.
- No end-to-end PRECEDENCE test. §8 names test_layering.py but the contract must assert the full chain group-default < hive.toml < HIVE_ env < explicit **override with one field set at ALL FOUR layers (explicit wins) AND a field set only at layer 2 surviving unset layers 3/4. A single 'env beats default' test would not catch a toml-vs-env inversion.
- No test that frozen-ness holds on the GROUP objects, not just the root. §2.1 claims frozen on root and every group; the hot-swap safety relies on `cfg.recall.H_frac_max = x` raising FrozenInstanceError. A test must mutate a NESTED group field and assert the raise — a frozen root with a non-frozen group passes a root-only test and still lets the floor be mutated.
- No accept-path test for tier B reload. The state machine has two accept arms (A,B) and two reject arms (C,D); §8 covers tier-A allowed, tier-C/D refused, but no test asserts a tier-B change (the most common hot-swap, e.g. H_frac_max retune) is ACCEPTED and returns `new`. A guard wrongly rejecting tier B ships uncaught.
- `diff_tier` 'strictest tier governs' has no mixed-change test. A change touching one tier-A AND one tier-D field must return D and refuse; without a multi-field test a max/min inversion (returning the loosest) ships silently — the warm-store-corruption path mutation #1 only guards a different spot.
- Secret-never-logged is under-strong. `test_secret_never_logged` greps for sk-/AKIA/ghp_ in emitted lines, but the env-coercion-failure path is the one §6 says omits the raw value because it 'could carry a secret'. The test must assert the offending RAW VALUE substring is absent on that path, not just 3 canary prefixes — a non-prefixed high-entropy secret in an env var would slip past a 3-prefix grep.
- No fail-fast test for an UNKNOWN provider at build_*. §2.2 claims KeyError→ValueError-with-valid-keys and §6 logs it at error, but the §8 invariant list only lists the positive swap test. The typo'd-seam path (the likely agent error) is prose-only — add a test asserting build_embedder/index/producer raise ValueError listing valid keys on an unknown string.
- WAL-safety of backup is untested. §2.5/§7 claim sqlite3.backup() is WAL-safe under concurrent writers; test_backup_roundtrip only proves a quiescent copy. The contract needs a test that opens the DB in WAL mode, holds a concurrent/uncommitted write, runs backup, and PRAGMA integrity_check's the dest — else the load-bearing 'WAL-safe' claim is untested prose.
- health fail-soft is tested only for the DB probe. §2.5 says health never raises and fails soft on ANY probe (store, embedder, producer); §8 names only the DB-error case. The embedder-probe-raises and producer-tick-read-raises paths need their own fail-soft tests, or a container HEALTHCHECK could crash the health endpoint itself.
- embedder_resident gating is unenforced. §2.5 claims a HEALTHCHECK 'can't be called healthy before the model is in RAM'; the contract needs a test asserting health() returns embedder_resident=False (and the documented healthy predicate excludes that state) before build_embedder has loaded the model — otherwise the operational claim is prose only.

**Must-fix:**
- BLOCKER (build-readiness): the `interface_block` and `test_contract` companion blocks referenced ~12 times ('The full surface is in interface_block', 'The full list with exact assertions + the failure each catches is in test_contract', 'the complete owned set is enumerated at the bottom of interface_block') do NOT exist in the repo or this deliverable. Per the LOCKED test mandate (every module spec MUST ship the failing tests written BEFORE implementation, file::test + exact assertion + failure caught), these are first-class deliverables, not appendices. No build-ready sign-off is possible without the actual signatures and the actual assertions in hand. Ship both blocks inline.
- BLOCKER (correctness): `producer.epsilon_explore > 0` validation is misgrounded. §4.7 guardrail-1's ε ('a fraction of RECALLS ignore utility entirely') is a recall-path knob that belongs in the `recall` group and is consumed by the C3 ranker, NOT the producer. The spec's §4.8 `producer.*` group has no `epsilon_explore` field (its knobs are watch_repos/poll_interval_s/assoc_window_s/stamp_trailer/bugfix_pattern/require_stamp). As written M11 validates a field the config tree does not define, and the guardrail that genuinely prevents loop-starvation (recall ε>0) gets no validation. Fix: validate `recall.epsilon_explore > 0` (or the agreed recall-group key) and remove/relocate the producer claim. This is a CONFIG_DRIFT-class error in the module whose stated job is to kill CONFIG_DRIFT.
- Make the gate-construction contract explicit and located. §5 says M11 'must NOT know about the abstention-gate algorithm' yet §2.3 requires `gate._recall is cfg.recall` (M11 must pass the frozen object by identity at construction). The text leaves WHO calls the gate constructor undefined — if it is the gate's own module or the serving composition root, the identity invariant lives outside M11 and M11's mutation #2 cannot enforce it from inside M11. Name the single call site (e.g. a `build_gate(cfg)` added to M11's registries, or an explicit assertion contract the serving layer must satisfy) so floor-identity has one enforced owner, not a prose handshake across a boundary M11 declares it does not cross.
- Resolve the env-namespacing example against the dropped sparse geometry. The example `HIVE_GEOMETRY__D=384` (§2.1) and the regression test `test_env_namespacing_d_vs_no_collision` both reference a `geometry.D` field, but per §10/D1 the sparse `geometry.D` (and k) are DROPPED in the greenfield — only dense `geometry.d` survives. With `D` gone there is no collision left to regress against, weakening the standing regression's premise. Clarify: restate the regression as 'case-sensitive field names never collide' using a pair that actually exists post-drop, and document that the original d/D collision is now structurally impossible because `D` no longer exists; otherwise the example teaches an agent to set a non-existent field.
- Add validation tying `geometry.d` to the FIXED GEOMETRY window. The spec validates only `geometry.d > 0`, but the project locks d to the measured optimum (256 default; 256–384 window). `d>0` admits d=7, silently producing a store whose recall cannot meet the §6.5 targets and that mismatches a PCA head fit at 256. Either validate `d in {256,384}` (or the agreed window) or document explicitly why arbitrary d is allowed and how W_version migration covers it — otherwise an agent can construct a legal-but-broken geometry.
