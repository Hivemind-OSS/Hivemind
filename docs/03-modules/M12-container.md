# M12 Container / compose  (container)

**One-line:** A multi-stage Docker image + single-service compose that packages the hive MCP server, in-process git-producer, and baked CPU embedder into one non-root runtime, exposing the server over stdio-into-container, persisting one WAL SQLite file on a named volume, and gating "healthy" on a loaded-embedder probe — so the whole product runs and survives restart with `./hive up` and no model download on the hot path.
**Port disposition:** BUILD-NEW (no reference file). The reference tree (`cls_memory/`) has NO Dockerfile, compose.yaml, entrypoint, or healthcheck — it ships as a Python package launched by host systemd units (`cortex-*.timer/.service`), the exact deployment model M11's teardown.sh DISMANTLES. So M12 is wholly new infrastructure. It PORTS two runtime contracts verbatim from the tree rather than reinventing them: (1) the stdio JSON-RPC transport and `python -m hive.adapters.mcp.server --db --tenant --agent` entrypoint shape from `serving/mcp_server.py:297` (`run_stdio`) + `:320` (`main`); (2) the WAL pragma block from `storage/persistence.py:171-175` (`journal_mode=WAL`, `synchronous=NORMAL`, `foreign_keys=ON`, `busy_timeout=5000`) — the entrypoint asserts these are applied, it does not set them. The HEALTHCHECK consumes the existing `health()` → `HealthSnapshot` contract (`service.py:1354`, `types.py:459`) extended (per the M11 decision) so `ok=True` requires the embedder to be RESIDENT, not merely importable.

---

# M12 — Container / compose / runtime

## 1. Responsibility (one deep module)

M12 is the **runtime envelope**: the single deep module that turns the hive Python package + its three ports' default adapters into **one reproducible, restartable, self-contained process you start with `./hive up`**. Its narrow surface is three operator verbs (`up` / `down` / `logs`) and one declarative `compose.yaml`; the rich hidden work behind that surface is: a multi-stage build that **bakes the bge-small weights into the image** so the hot path never touches the network; a fail-fast **entrypoint** that orders migration → index-build → embedder-warm → serve; a **healthcheck** that refuses to report green until the embedder is *resident*; a **named-volume** WAL SQLite layout that survives restart; a **non-root** final user; and a **secret-free** layer history. Everything an operator must get right to run this product correctly is enclosed here, so the rest of the system can assume "a warm, migrated, indexed, embedder-loaded server on stdio" as a precondition rather than re-deriving it.

The product invariant M12 makes *structural* (not documented): **Local & self-contained / no-network-on-hot-path** (spec §1). By baking weights at build time and setting `HF_HUB_OFFLINE=1`/`TRANSFORMERS_OFFLINE=1`, a runtime network call to fetch a model is impossible, not merely discouraged — provable by running the container with `--network none` and observing recall still works.

## 2. Public surface + ENFORCED contract

The surface is deliberately tiny. Operator-facing: `./hive {up|down|logs|nuke}`. Machine-facing: the **image contract** (entrypoint, healthcheck, volume, user) and the **compose contract**.

**`hive up` — postcondition:** `docker compose up -d` then poll `docker inspect .State.Health.Status` until `healthy` or 180 s; on timeout, dump `docker compose logs` to stderr and **exit non-zero** (fail-fast, per engineering standard §5/§6). It is a *thin liveness wrapper* only — it owns no handshake logic (that is M11 `hive_init`). Separation of liveness (M12) from handshake (M11) is a hard boundary.

**Image entrypoint — `hive.tools.entrypoint.main()`** (signature in interface block). Enforced ordering, each a logged checkpoint: `config.loaded` → `migrate.done` → `index.built` → `embedder.warm` → `serve.ready`. **Pre/post:** never enters `run_stdio` unless every prior step succeeded; a missing required env var exits **78 (EX_CONFIG)**; a dead embedder exits **69 (EX_UNAVAILABLE)**. Error semantics are *exit codes*, not stdout text, because **stdout is the JSON-RPC channel** — all human/diagnostic output goes to **stderr** (ported invariant from `mcp_server.py:351` "stderr stays clean so stdout-bound MCP JSON-RPC isn't polluted").

**Healthcheck — `hive.tools.healthcheck.main()`**: exit 0 **iff** `health()['ok'] is True AND health()['embedder_loaded'] is True`. Touches no network. This is the one precondition that must be **designed out, not documented**: "the model might not be loaded yet" is eliminated by making *healthy ≡ loaded* — orchestrators and `hive up` gate on health, so an unloaded server is structurally invisible as "ready."

**Transport choice (picked + justified): stdio-into-container.** The reference server is JSON-RPC 2.0 over stdio (`run_stdio`, `mcp_server.py:297`); MCP's native local transport is stdio; the product is single-tenant, single-process, local, **no network on the hot path** (§1). A TCP/UDS endpoint would (a) add a listener surface that contradicts "no network on the hot path," (b) require auth we explicitly don't build in the high-trust MVP (§9), and (c) diverge from the ported transport. So the harness attaches to the **container's** stdin/stdout (`stdin_open: true, tty: false`); registration is `command: docker, args: ["compose","run","--rm","-T","hive-server"]` (or `docker attach` to the long-lived service). The compose seam leaves a UDS/sidecar swap for later (§3) without changing the server.

**Narrow surface, rich contract:** the operator sees 4 verbs; the enforced contract behind them is the 13 named test assertions in §8 (non-root, no-secret-in-layers, weights-baked-offline, WAL-active, volume-persists, stdio-roundtrip, healthy-iff-loaded, fail-fast-on-missing-env). Contracts that cannot lie: exit codes + `docker inspect` fields + layer-tar scans, not prose in a README.

## 3. Swap seam

M12 itself is not behind a port, but it **operationalizes** the three mandated swap seams (embedding provider, vector storage, outcome producer) at the *deployment* layer, and it stages **one** future swap structurally:

- **Sidecar seam (the compose swap).** The locked decision: "a compose file is present so an embedder/producer sidecar is a LATER swap (not built now)." M12 ships `compose.yaml` with the single `hive-server` service **plus a commented `embedder`/`producer` service block**. To externalize the embedder later, an operator uncovers the sidecar service and flips `HIVE_EMBEDDING__TRANSPORT=loopback` (the embedding port's second adapter, owned by M-embedding) — **no image rebuild of the core, no Dockerfile change to the server**. Proof the swap needs no core change: the server already talks to the embedding **port**; the sidecar adapter is selected by one config key; M12 only adds a service + a network alias. Likewise the producer can move to its own container by adding a `producer` service that mounts the same `/data` volume read-mostly and shares the single-writer discipline via `BEGIN IMMEDIATE` (the producer port's external-process adapter).
- **What a second M12 adapter would be:** a Kubernetes `Deployment`/`StatefulSet` (instead of compose) reusing the *same image contract* (entrypoint, healthcheck, `/data` PVC). It must implement only: a PVC at `/data`, the healthcheck as a `readinessProbe`, and stdin attach (or the loopback transport). It changes **zero** Python. This proves the runtime contract is the port, and compose is just the default adapter.

## 4. Data owned

M12 owns **no tables**. It owns the **on-disk layout and its lifecycle**:

- **Named volume `hive-data` → `/data`**, holding: `shared.db` (the single WAL SQLite file, schema owned by M-store), `shared.db-wal` / `shared.db-shm` (WAL sidecars), `hive.toml` (operator config, optional), and `backups/` (daily SQLite `.backup` snapshots, `retention.backup_keep`=30 per §4.6 — the one ops floor). The volume is the durability boundary: `hive down` preserves it, only `hive nuke` (`down -v`) destroys it.
- **Config keys it READS** (owned by M-config; M12 only plumbs them as `HIVE_<GROUP>__<FIELD>` env): `store.db_path`=`/data/shared.db`, `embedding.model_name`=`BAAI/bge-small-en-v1.5`, `tenant_id`, `agent_id`, `observability.log_level`=`INFO`, `retention.backup_keep`=30. It also sets the build/runtime env `HF_HOME=/opt/hf-cache`, `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1` that make the baked weights authoritative.
- **WAL is asserted, not set, here.** The pragmas (`journal_mode=WAL`, `synchronous=NORMAL`, `foreign_keys=ON`, `busy_timeout=5000`) live in M-store (`persistence.py:171-175`). M12's contract is only that the *containerized* run actually applies them — verified by `test_wal_mode_active_in_container`.

## 5. Dependencies

M12 depends on (and only on) the **runtime contracts** of:
- **M-store** — for the migration entry it invokes and the WAL layout it hosts (it knows `/data/shared.db`, not the DDL).
- **M-config** — for config load + the `HIVE_*__` env namespace + fail-fast-on-missing (it calls `load()`, it does not parse env itself).
- **M-embedding** — for the warm-on-boot probe and the baked model path (it knows "load the model object," not how PCA projects).
- **The MCP server adapter (M7/C7)** — for `run_stdio(server)` and `health()`/`HealthSnapshot`.

**What M12 must NOT know (named boundaries):** the **secret-scan logic** (M-admission/C6 — M12 only ensures no secret is baked into a *layer*, a disjoint concern), the **ranker/gate math** (C3/C4), the **producer's §11 join policy** (C10 — M12 only co-locates the producer in-process; it does not know associate/settle/clawback), and the **DDL/credit tables** (M-store). M12 is a driving adapter on the same footing as the MCP adapter (per the hexagonal architecture decision): it boots and supervises the domain, it contains none of it.

## 6. Failure-mode logging (per engineering standard; secrets never logged)

All M12 logging is **structured JSON to stderr** (stdout is the protocol channel), with `timestamp/level/context/message/error/stack`:

| Boundary | Level | Context logged |
|---|---|---|
| Config load, missing required env (tenant_id / db_path / model) | **error** | which var, EX_CONFIG=78; **never the value** |
| Migration run (start/done) | **info** | `migrate.start`/`migrate.done`, schema version, elapsed_ms |
| Index build from scan_approved | **info** | `index.built`, n_approved, elapsed_ms |
| Embedder warm (start) | **info** | `embedder.warm`, model_name, native_dim |
| Embedder load failure | **error** | exception type+stack, model path, EX_UNAVAILABLE=69 |
| Serve ready | **info** | `serve.ready`, tenant_id, pid |
| Healthcheck failure | **warn** | which conjunct failed (`ok` vs `embedder_loaded`), db_path |
| `hive up` health-wait timeout | **error** | elapsed_s, last health status, dumps `docker compose logs` |
| WAL pragma mismatch (defensive assert at boot) | **error** | observed journal_mode vs expected `wal` |

Secrets/PII never written: env *values* are never logged (only var *names*); the secret-scan is upstream (M-admission); layer-secret prevention is a *build* test (`test_no_secret_in_any_layer`), not a log. Long-running ops (embedder warm, index build) log `elapsed_ms` (performance-context standard).

## 7. Port disposition vs §10 map

**BUILD-NEW.** The §10 reuse/delete map has **no container/compose/entrypoint row** — the reference deploys as a host Python package under systemd timers (`cortex-*.service/.timer`), which M11's `teardown.sh` removes. M12 therefore introduces all infra files (`Dockerfile`, `compose.yaml`, `hive/tools/entrypoint.py`, `hive/tools/healthcheck.py`, `hive/tools/bake_model.py`, `./hive`). It **PORTS two runtime contracts** rather than reinventing them: the stdio JSON-RPC transport + `main(--db --tenant --agent)` shape from `serving/mcp_server.py:297,320`, and the WAL pragma expectations from `storage/persistence.py:171-175`. It **EXTENDS** the `HealthSnapshot` (`types.py:459`) with one additive key `embedder_loaded: bool` (the M11 "healthy ≡ resident" contract). Nothing is dropped; nothing is ported verbatim as a file.

## 8. TEST CONTRACT (test-first, full functional coverage)

See the consolidated test list (interface/test_contract fields). Coverage map — every §6 failure mode and every §2 invariant has a named test:

- **Happy path:** `test_image_builds_clean`, `test_boot_runs_migration_then_index_then_serves`, `test_healthcheck_green_when_loaded`, `test_stdio_jsonrpc_roundtrip`, `test_volume_persists_across_restart`.
- **§2 invariant: non-root final user** → `test_final_user_is_non_root` (USER directive is the last layer; `id -u != 0`).
- **§2 invariant: multi-stage / no bloat** → `test_multistage_excludes_build_tree`.
- **§6 boundary: secret never in a layer (§9 infra floor)** → `test_no_secret_in_any_layer`.
- **§1 invariant: no-network-on-hot-path / weights baked** → `test_weights_baked_offline` (+ `--network none` load) and `test_healthcheck_no_network`.
- **§6 boundary: fail-fast on missing required env** → `test_missing_required_env_exits_config` (exit 78) and `test_tenant_id_required_fails_fast` (compose-level).
- **§6 boundary: dead embedder** → `test_embedder_warm_failure_exits_69`.
- **§2 invariant: healthy ≡ embedder resident (M11 contract)** → `test_healthcheck_red_before_embedder_resident` (+ the mutation test).
- **§2 invariant: WAL active in container** → `test_wal_mode_active_in_container`.
- **§4 lifecycle: durability across restart / volume semantics** → `test_volume_persists_across_restart`, `test_nuke_destroys_volume_up_recreates_empty`.
- **§2 transport: MCP reachable + channel hygiene** → `test_stdio_jsonrpc_roundtrip`, `test_stderr_clean_of_jsonrpc_pollution`.
- **§3 compose validity** → `test_compose_config_valid`.

**Acceptance-gate mapping:** M12 owns the *infrastructure* slice of §6.1 #5 substrate-safety — specifically the **secret floor at the build layer** (`test_no_secret_in_any_layer`) and the **Local-&-self-contained** invariant (`test_weights_baked_offline`, `test_healthcheck_no_network`). It does **not** own §6.1 #5 (a)/(b) (secret-scan-before-stage, approved-only-recall) — those are M-admission's gates; M12 only guarantees the *container they run in* is non-root, secret-free in its layers, and offline-capable.

**MUTATION test (RULE 2):** in `healthcheck.main()` drop the `embedder_loaded` conjunct → `test_healthcheck_red_before_embedder_resident` goes **RED** (probe reports healthy while the model is absent) → restore the conjunct → **GREEN**. This proves the "healthy means serving" contract is enforced by a test, not by a comment — the one fault that would let an orchestrator route recalls at an unloaded server. A reviewer can state: no functional path (build, non-root, no-secret, offline-weights, fail-fast-config, dead-embedder, WAL, volume-persistence, stdio-roundtrip, healthy-iff-loaded) is untested.

---

## Design review (independent pass)

**Verdict:** CONDITIONAL — a genuinely deep, well-scoped runtime envelope with an exemplary "define errors out of existence" move (healthy ≡ embedder-resident) and contracts that cannot lie (exit codes + docker inspect fields + layer-tar scans, not README prose). The module boundary is clean: M12 is a driving adapter that boots and supervises the domain and contains none of it, and it correctly names the five things it must NOT know (secret-scan, ranker/gate math, §11 join policy, DDL/credit tables, handshake). Three things block a build-ready sign-off, all rooted in the same place — the transport-and-process model is underspecified where it touches concurrency and lifecycle. (1) The §3 producer-sidecar swap claims a second container can mount the same /data volume and "share the single-writer discipline via BEGIN IMMEDIATE," but spec §1/process-model (lines 866-868) makes the producer in-process *specifically* to keep one writer, and cross-container multi-writer on a WAL file is exactly the multi-writer scenario §9 lists OUT OF SCOPE; this swap is presented as a no-core-change adapter when it is actually a new concurrency contract with no test. (2) The transport registration `docker compose run --rm -T hive-server` contradicts the named-volume, warm, long-lived server the rest of the spec describes: `run --rm` spins a fresh ephemeral container per attach (re-runs entrypoint: migrate→index→warm every connection — defeats the warm-embedder invariant and the §1 cost model), while the parenthetical `docker attach` to a long-lived service is the design that actually matches `hive up` + healthcheck — the spec offers both as equivalent when only one is correct, and neither has a test pinning which the harness uses. (3) The healthcheck contract reads `health()['embedder_loaded']`, a key the reference health() does NOT emit (confirmed at service.py:1354-1410 / types.py:459); M12 declares this an EXTENDS but the test `test_healthcheck_red_before_embedder_resident` will silently pass-as-false (KeyError→falsey or absent-key→red) for the WRONG reason unless M7/M-embedding actually populates it — the cross-module contract is asserted but not enforced at the M12 boundary. Fix the process/transport model decisively (pick attach-to-long-lived, delete the run --rm framing or test it explicitly), demote the producer-sidecar to a named future-with-its-own-concurrency-design rather than a one-config-key swap, and add a contract test that fails if embedder_loaded is absent from the HealthSnapshot — then this signs off.

**Scores (1–10):**
- design_complexity: 3
- cognitive_load: 4
- information_leakage: 3
- extensibility_fit: 8
- agent_navigability: 8
- contract_enforcement: 7
- test_coverage: 6

**Red flags:**
- Nonobvious Code / Implicit Wiring @ §2 transport registration ('docker compose run --rm -T' vs 'docker attach') — two structurally different runtime models are presented as interchangeable; an agent reading this cannot determine which container the harness attaches to or whether the embedder stays warm — root: obscurity → unknown-unknowns (an agent will pick run --rm, the simpler-looking one, and silently break the warm-server cost model).
- Information Leakage @ §3 producer-sidecar swap ↔ §4 single-writer WAL discipline — the single-writer concurrency decision (owned by the process model / M-store BEGIN IMMEDIATE) is restated and weakened in M12's deployment layer, which now claims a second container can share it via the same pragma. The same design decision (who may write the WAL) is now spread across the process model, M-store, and M12's compose swap — root: dependency → change amplification (tightening the writer discipline now requires a coordinated edit in M12's sidecar story).
- Prose-Only Contract on Tricky Semantics @ §3 'Proof the swap needs no core change' — the claim that the producer/embedder externalization needs zero core change is asserted in prose with no test (unlike the rest of M12, which is exemplary about enforced contracts). The K8s second-adapter claim ('changes ZERO Python') is similarly prose-only — root: obscurity → confident misuse (an operator/agent trusts a swap that has hidden concurrency cost).
- Hard to Describe @ §2 healthcheck exit-0 condition 'health()['ok'] is True AND health()['embedder_loaded'] is True' — the contract depends on a key the source health() doesn't yet emit (EXTENDS in §7), so the precondition 'M7 populates embedder_loaded' must be carried as prose across the M12↔M7 boundary rather than enforced by a shared type at the seam — root: dependency → silent drift if M7 ships health() without the key.

**Test gaps:**
- No test that recall actually SUCCEEDS under `--network none` (the §1 structural invariant). §8 lists test_weights_baked_offline '(+ --network none load)' and test_healthcheck_no_network, but loading the model offline ≠ a full recall round-trip succeeding with the network namespace removed — the named end-to-end proof of 'no network on the hot path' is missing as its own assertion.
- No test for the `hive up` 180s health-wait TIMEOUT path: §2 specifies on-timeout dump logs to stderr and exit non-zero, and §6 logs it, but §8 has no test_hive_up_times_out_dumps_logs_and_exits_nonzero (the fail-fast operator contract is untested).
- No test for the WAL pragma DEFENSIVE ASSERT-at-boot failure mode. §6 lists 'WAL pragma mismatch (defensive assert at boot) → error'; §4 has test_wal_mode_active_in_container (the success case) but nothing that exercises the boot-time assert firing when journal_mode≠wal — the defensive path is logged but never tested.
- No test that the producer in-process tick + the MCP serve loop coexist as a single writer (the in-process single-writer discipline M12 hosts). §5 says M12 'co-locates the producer in-process'; no test confirms concurrent producer-write + recall-read don't deadlock/SQLITE_BUSY under busy_timeout in the container.
- No test for backups/ retention (retention.backup_keep=30, the '§4.6 ops floor' M12 claims to own). §4 names the backups/ dir and the keep=30 floor but §8 has no test_backup_retention_keeps_30 — an owned lifecycle behavior with zero coverage.
- test_stderr_clean_of_jsonrpc_pollution covers structured logs going to stderr, but there is no test that a CRASH/uncaught-exception traceback also goes to stderr (not stdout). The ported invariant (mcp_server.py:351) is 'stdout stays clean'; a Python default traceback prints to stderr already, but the entrypoint's own error formatting and any library that prints to stdout would pollute the JSON-RPC channel — a test_no_stdout_writes_except_jsonrpc would close this.
- No test pins that the secret-floor build scan (test_no_secret_in_any_layer) actually inspects ALL layers including the HF cache layer where baked weights live — a planted secret in /opt/hf-cache or an intermediate builder layer that's COPY'd forward could evade a final-image-only scan. The test name implies all layers but the assertion granularity isn't specified.

**Must-fix:**
- TRANSPORT AMBIGUITY (design + test): §2 offers `docker compose run --rm -T hive-server` AND `docker attach` to a long-lived service as equivalent registration options. `run --rm` spins a fresh container per attach and re-runs the full entrypoint (migrate→index→embedder-warm) on every connection, defeating the warm-embedder invariant and the §1 no-network/cost model; only attach-to-long-lived matches `hive up`+healthcheck. Pick one, delete the other, and add a test (e.g. test_attach_reuses_warm_server) that fails if a second client connection triggers a cold embedder warm.
- PRODUCER-SIDECAR SWAP UNDERSPECIFIED (design): §3 claims the producer can move to a separate container mounting the same /data volume and 'share the single-writer discipline via BEGIN IMMEDIATE' as a no-core-change swap. Spec lines 866-868 make the producer IN-PROCESS to guarantee one writer; two OS processes in two containers writing one WAL file is the multi-writer case §9 declares OUT OF SCOPE. Either remove this from the 'one config key, no core change' swap claim and label it a future adapter requiring its own concurrency design, or add a real cross-container single-writer contract test (BEGIN IMMEDIATE + busy_timeout under concurrent writes). As written it is an unenforced concurrency contract.
- embedder_loaded CONTRACT NOT ENFORCED AT BOUNDARY (design + test): healthcheck.main() and `hive up` gate on health()['embedder_loaded'], an additive key the reference HealthSnapshot does NOT emit (service.py:1354-1410, types.py:459 confirmed). test_healthcheck_red_before_embedder_resident can pass for the wrong reason (missing key reads falsey ⇒ red) even if M7 never populates the key. Add test_health_snapshot_has_embedder_loaded_key asserting the key is PRESENT and boolean, so a missing-key regression goes red distinctly from an unloaded-embedder red.
- MUTATION TEST INCOMPLETE (test, RULE 2): §8 names exactly ONE mutation (drop the embedder_loaded conjunct). The user mandate requires mutation tests for every gate/state-machine/credit path; M12's entrypoint is an ordered state machine (config→migrate→index→warm→serve with exit-78/exit-69 fail-fast). Add mutations: (a) remove the missing-env guard ⇒ test_missing_required_env_exits_config must go red; (b) swallow the embedder-load exception (continue to serve) ⇒ test_embedder_warm_failure_exits_69 must go red; (c) reorder serve-before-migrate ⇒ a boot-order test must go red. Name the fault and the catching test for each.
- BOOT-ORDER INVARIANT UNTESTED (test): §2 asserts the strict ordering config.loaded→migrate.done→index.built→embedder.warm→serve.ready and 'never enters run_stdio unless every prior step succeeded', but §8 has no test that pins the ORDER (test_boot_runs_migration_then_index_then_serves checks the happy sequence ran, not that serve is unreachable when an earlier step fails). Add test_serve_unreachable_when_migration_fails (inject a migrate failure ⇒ run_stdio never called, exit non-zero).
