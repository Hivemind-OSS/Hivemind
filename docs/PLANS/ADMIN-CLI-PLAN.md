# Hivemind — the `hive` Admin CLI

**Status:** LANDED 2026-06-11 — implemented as six green chunks on master (authctl `list` +
`labels()` → CLI skeleton → lifecycle → provisioning → `hive.sh` retirement → docs); suite green +
RULE-2 mutations red/restored per chunk. **Deviations recorded:** (1) the `run()` seam takes the
FULL child argv (program included), not a `docker compose` prefix — §4's health-wait polls
`docker inspect`, which is not a compose verb and could not ride a prefixing runner; (2)
`hive connect` prints the loopback registration line when `NGROK_DOMAIN` is unset (both lines are
the AUTH-PLAN §10 forms; the §12 ngrok-API fallback stays deferred); (3) §9's drop-`ORDER BY`
mutation on `labels()` is an EQUIVALENT mutant (the TEXT-PK covering index already yields label
order) — the listed `return []` alternative pinned the tests instead.
Design-reviewed (`/software-design-review`, Mode B); the CLI-language decision was re-judged and
**resolved to a stdlib-Python `console_scripts` CLI** (§6 D1).
**CONVERGENCE CV6 is now UNBLOCKED** — the `hive credit` git-outcome feedback loop rides this
CLI's dispatch table (01-DECISIONS D10/D-C5).
**Date:** 2026-06-10 · synced 2026-06-11 to the landed system (REMOTE-ACCESS landed; CONVERGENCE
CV1–CV5+CV7 landed): seat-token surface (AC7), the CV6 `credit` extension seam (AC8, §6 D7), runner
stdin forward note (§4). No §6 decision reopened.
**Companion plan:** **`REMOTE-ACCESS-PLAN.md` — LANDED 2026-06-10.** The secure ngrok tunnel + server
hardening this CLI drives (`--tunnel`, `connect`) already exists; that file owns the network/transport
layer, **this file owns the operator front door only** — the CLI orchestrates landed substrate end to
end.
**Builds on:** `AUTH-PLAN.md` (per-device bearer tokens, **landed**) and `REMOTE-ACCESS-PLAN.md`
(tunnel sidecar, **landed**) — **no auth-core or transport change here**; the CLI only *orchestrates*
them.
**Unblocks:** `CONVERGENCE-PLAN.md` **CV6** — `hive credit`: host-side git scan of the `Hive-Trace`
commit trailers agents already emit → in-container `creditctl ingest` → `task_outcomes` → the existing
readiness→keystone→`utility_rerank` chain. CV6 is specified there (§8/§9) and is **not re-specified
here**; this plan guarantees only the surface it rides (§6 D7, AC8).

**Scope.** Replace the `hive.sh` bash wrapper with a **stdlib-Python CLI** exposed as `hive`, wrapping
the *entire* admin surface — lifecycle + per-seat token provisioning (the fleet front door) + tunnel
opt-in + status — behind the simplest possible verbs. It **shells out** to `docker compose` and the in-container Python
tools and **reimplements nothing**: token crypto + schema stay single-sourced in
`hive/adapters/auth_store_sqlite.py`.

**Decisions taken into this draft:**
- The CLI is **in-package Python** (`hive/tools/cli.py`, sibling to the other operator tools),
  **stdlib-only** (argparse + subprocess), exposed as `hive` via `[project.scripts]`.
- Public exposure is an **explicit flag** (`hive up --tunnel`), never a stateful `.env` mutation; the
  default is loopback. The tunnel mechanism itself is in the companion plan.
- Minting stays **operator-on-the-host** (no network mint endpoint) — the AUTH-PLAN model.
- The CLI is the **fleet provisioning front door** the landed convergence docs already lean on:
  `docs/CLIENTS.md` documents `hive token <seat>` (one token per agent seat — the promotion fuel)
  *today*; the verb exists only once this plan lands. `hive credit` (CV6) stays **owned by
  CONVERGENCE-PLAN §8** — this plan ships its landing surface, never its spec (§6 D7).

---

## 0. Goal & acceptance criteria

**Goal.** The operator runs the whole system — build, start, expose over the tunnel, mint/revoke
per-seat tokens, check status, print a teammate's connect line — through one dead-simple CLI
(`hive up --tunnel`, `hive token alice`, `hive connect`, `hive status`), with **no raw
`docker compose …` typing** and **no security logic reimplemented**.

| AC | Criterion | Owner |
|---|---|---|
| AC1 | The admin runs the **entire** lifecycle + provisioning through `hive`: `up [--tunnel]/down/logs/status/token/revoke/tokens/connect/nuke`. | §4 CLI surface |
| AC2 | **Default is loopback.** `hive up` (no flag) starts no tunnel. `hive up --tunnel` **fail-fasts** if `NGROK_AUTHTOKEN`/`NGROK_DOMAIN` are unset (`EX_CONFIG`, no `compose` call), else adds `--profile tunnel`. | §4 `up` |
| AC3 | The CLI **reimplements no auth/token logic** and **embeds no SQL** — `hive token/revoke/tokens` shell to `python -m hive.tools.authctl {create,revoke,list}`; the 256-bit mint, sha256-at-rest, and the `access_tokens` schema live in **one** place (the Python adapter). | §4 + §6 D5 |
| AC4 | `hive.sh` is removed; every contract it carried (e.g. "up uses `up -d`, never `run --rm`"; the bounded health-wait) is **preserved as a pytest test**, not lost. | §5 |
| AC5 | **Zero new dependency** (runtime or dev): stdlib-only CLI; `pyproject.toml` gains only a `[project.scripts]` entry-point line. | §4 exposure |
| AC6 | The **M11(handshake)/M12(liveness+ops) boundary holds**: the CLI never performs `hive_init`; `hive connect` only emits the transport-registration line. | §1, §6 D2 |
| AC7 | **Seat-token contract surfaced inline** (CONVERGENCE §3.4, landed docs): `hive token`'s stderr handoff hint and `hive connect`'s output both carry "mint one token per seat (`hive token <seat>`) — never share across agents", consistent with the language `docs/CLIENTS.md` already ships. | §3, §5 |
| AC8 | **CV6-ready with zero rework**: adding `hive credit` (CONVERGENCE-PLAN §8) needs only one new dispatch entry + a keyword-only `input=` (stdin NDJSON) widening of the `run()` seam; no verb shipped here changes. The credit verb itself is **out of this plan's scope** — it lands as CV6 immediately after. | §4, §6 D7 |

---

## 1. Principles (locked)

1. **The CLI is a driving adapter, not a reimplementation.** It *orchestrates* the existing deep
   substrate (compose, entrypoint, healthcheck, `authctl`, the ngrok sidecar) and **single-sources**
   all security-sensitive logic in the Python it shells to. (APOSD *pull complexity down*: hide the
   *invocation*, never duplicate the crypto or the schema.)
2. **Coherent operator front door — honest about depth.** The CLI's value is a *single coherent
   surface* hiding "this is a dockerized service you administer via `compose` + `exec` + `python -m`."
   The genuinely non-trivial logic concentrates in **`up`** (bounded health-wait), **`status`**
   (aggregation), and **`connect`** (URL derivation); the rest are deliberately thin forwards (in
   Python a thin forward is a one-line dict entry — cheap, not a "shallow module" tax).
3. **Respect the existing hard boundary.** M12 (liveness/ops, this CLI) and M11 (`hive_init` handshake,
   pure-MCP, agent-driven) stay separate. `hive connect` wires the *transport* (`claude mcp add …`);
   the agent still performs `hive_init` over MCP. The CLI touches none of the swap ports or handshake
   state.
4. **Default-safe, opt-in exposure.** Loopback is the default; the tunnel is a visible flag. The one
   moment a token can travel the public internet is the moment the operator typed `--tunnel` —
   conscious acceptance (and TLS-terminated by the tunnel; see `REMOTE-ACCESS-PLAN.md §10`).
5. **Contracts that can't lie.** Exit codes (sysexits, mirroring the Python tools) + asserted argv via
   an injected runner — not prose. A wrong compose invocation fails a pytest test.
6. **Stay in-language; fulfill the spec.** A `hive` console-script realizes the `hive <cmd>` UX the docs
   always wanted (the `hive.sh` name was a `hive/`-package-dir collision workaround), **without** adding
   a language or toolchain — lowest agent context cost, idiomatic to the repo.

---

## 2. Architecture & where it sits

```
 OPERATOR (host: repo + Python + Docker)              TEAMMATE (any device)
   │ hive up --tunnel        ┌────────────────────────┐   │ hive connect → prints:
   │ hive token alice ──────▶│ hive/tools/cli.py       │   │   claude mcp add --transport http hive \
   │ hive status             │ (stdlib: argparse,      │   │     https://<NGROK_DOMAIN>/mcp \
   └──────────┬──────────────┘  subprocess) — driving  │   │     --header "Authorization: Bearer $HIVE_TOKEN"
              │ run() seam:     adapter; imports NO     │   ▼
              ▼                 brain runtime           │  agent does hive_init over MCP (M11, UNCHANGED)
   docker compose {up -d [--profile tunnel],   docker compose exec -T hive-server \
                   down, logs, ps}               python -m hive.tools.authctl {create,revoke,list}
              │                                   │
              ▼                                   ▼
   ┌─────────────────────────────────────────────────────────┐
   │ compose project (REMOTE-ACCESS-PLAN.md — landed)         │
   │  hive-server (run_http · verify · 413/429 belts)         │
   │  ngrok sidecar (profile: tunnel)                         │
   │  SqliteTokenStore.{create,verify,revoke,labels}          │
   └─────────────────────────────────────────────────────────┘

 CV6 successor (CONVERGENCE-PLAN §8 — lands right after this plan; shown for the seam it rides):
   hive credit [path] ── host-side git log scan: the Hive-Trace trailers agents already emit;
     merged-to-main = win / reverted = loss (rev-list ancestry, never diff text)
     ──► NDJSON ──► docker compose exec -T hive-server python -m hive.tools.creditctl ingest
     ──► task_outcomes (idempotent (sha,eid) upsert) ──► readiness → keystone → utility_rerank
```

- **In-package operator tool** (`hive/tools/cli.py`), sibling-in-location to
  `authctl`/`healthcheck`/`bake_model`/`entrypoint` (the project's convention). A *layer above* them —
  it orchestrates via `docker compose exec`, never imports them — so it stays decoupled.
- **Imports only stdlib** (argparse, subprocess, os, sys) and **never the brain runtime** — so it pulls
  in no torch/sqlite/embedder, runs on the host with nothing installed beyond the repo + Docker, and is
  invokable as `python -m hive.tools.cli <cmd>` even before any `pip install`. (CV6's `credit` will
  additionally import the host-side scan half of `hive/tools/creditctl.py` — any sibling tool module
  the CLI imports must hold this same host-importable, stdlib-only property.)
- **The `run()` seam** is the one place that shells to `docker compose`; commands depend on the injected
  runner, not subprocess directly — so they unit-test without Docker (mirrors `authctl`'s `connect_fn`
  / `entrypoint`'s `run_http` injection idiom).

---

## 3. Command surface (simplest verbs that cover the whole admin job)

| Command | Hides / does | Shells to (via the `run()` seam) |
|---|---|---|
| `hive up [--tunnel]` | build + start + **bounded wait-healthy** (timeout → dump logs → non-zero); `--tunnel` → **fail-fast assert** `NGROK_AUTHTOKEN` + `NGROK_DOMAIN` set (else `EX_CONFIG`), then `--profile tunnel`; default loopback | `docker compose [--profile tunnel] up -d --build hive-server` + poll `docker inspect …Health` |
| `hive down` | stop, **preserve** the volume | `docker compose down` |
| `hive nuke` | destroy data — **typed confirmation required** | `docker compose down -v` |
| `hive logs [svc]` | follow logs (incl. `ngrok`) | `docker compose logs -f` |
| `hive status` | up? healthy? **tunnel on/off + public URL + token count** | `docker compose ps` + `… exec … healthcheck` + `… authctl list` |
| `hive token <seat>` | mint a per-seat identity (seat = authctl's `label`); print **only** the token to stdout; the stderr handoff hint carries the seat contract — "mint one token per seat, never share across agents" (AC7) | `… exec -T hive-server python -m hive.tools.authctl create <seat>` |
| `hive revoke <seat>` | revoke (next request → 401) | `… authctl revoke <seat>` |
| `hive tokens` | **list seat labels** (via the new `authctl list` — no SQL in the CLI) | `… authctl list` |
| `hive connect` | print the teammate's `claude mcp add …` line with the resolved tunnel URL + the inline seat hint (AC7) (transport wiring only — **not** `hive_init`) | (local; reads `NGROK_DOMAIN`) |

Core = `up/down/logs/status/token/revoke`. Recommended = `tokens/connect/nuke`. **Successor** (lands
immediately after this plan, spec owned by CONVERGENCE-PLAN §8): `hive credit [path]` — closes the
git-artifact outcome loop (trailer scan → `creditctl ingest`; AC8, §6 D7). **Deferred** (§12):
`hive doctor` (preflight), `hive migrate import-corpus …`.

### Single-source the token list — small additive Python changes
- `hive/adapters/auth_store_sqlite.py`: add `def labels(self) -> list[str]` (`SELECT label FROM
  access_tokens ORDER BY label`). **The schema read stays in the adapter that owns the table** — no SQL
  anywhere else.
- `hive/tools/authctl.py`: add a third subparser `list` → prints `store.labels()` one-per-line to stdout
  (mirrors the existing `create`/`revoke` structure + `out` injection seam).

### `hive connect` — URL resolution
Resolves the URL from **`NGROK_DOMAIN`** (the stable static domain pinned by the sidecar in
`REMOTE-ACCESS-PLAN.md §3`) and prints
`claude mcp add --transport http hive https://<NGROK_DOMAIN>/mcp --header "Authorization: Bearer ${HIVE_TOKEN}"`
plus the inline seat hint — "mint one token per seat (`hive token <seat>`)" — the CONVERGENCE §3.4
requirement whose wording `docs/CLIENTS.md` already ships (AC7).
*(Deferred fallback for random-URL setups: read the ngrok agent API at `:4040/api/tunnels`; §12.)*

---

## 4. Module shape (one file; argparse subparsers + a dispatch dict — don't over-structure)
```python
# hive/tools/cli.py — stdlib only; imports no brain runtime
SERVICE = "hive-server"          # single source of the compose service name (mirrors compose.yaml)
HTTP_PORT = 8765
AUTHCTL = ("python", "-m", "hive.tools.authctl")     # the in-container admin tool
TUNNEL_PROFILE = "tunnel"        # mirrors REMOTE-ACCESS-PLAN.md compose `profiles: [tunnel]`

# sysexits, mirroring authctl/entrypoint
EX_OK, EX_USAGE, EX_UNAVAILABLE, EX_SOFTWARE, EX_CONFIG = 0, 64, 69, 70, 78

Run = Callable[[Sequence[str], Optional[Mapping[str, str]]], "subprocess.CompletedProcess"]

def main(argv: Optional[list[str]] = None, *, run: Optional[Run] = None,
         out: Optional[TextIO] = None, env: Optional[Mapping[str, str]] = None) -> int:
    """Dispatch one admin verb; return a sysexits code. `run` (default: a subprocess wrapper),
    `out`, and `env` are injection seams — every command is unit-testable without Docker,
    exactly as authctl/entrypoint already do it."""
```
- `run()` default = a thin `subprocess.run(["docker","compose",*args], ...)` wrapper. Tests inject a
  fake that **records the argv** (the can't-lie contract).
- **CV6 forward note (recorded, not built):** `hive credit` pipes scan NDJSON to
  `… exec -T … creditctl ingest` via **stdin**, so CV6 widens the default runner with a keyword-only
  `input=` pass-through to `subprocess.run` — additive, no signature break. Nothing here ships dead
  plumbing for it, but nothing may preclude it: the runner stays a single thin wrapper, dispatch stays
  a dict (AC8).
- Deployment constants (`SERVICE`, `HTTP_PORT`, `AUTHCTL`, `TUNNEL_PROFILE`) are defined **once** here;
  the residual `SERVICE`/port/profile ↔ `compose.yaml` coupling is contained and acceptable (parsing
  compose to derive them adds more complexity than it removes).
- `up`'s bounded health-wait is the one ported-from-`hive.sh` non-trivial routine (poll
  `docker inspect -f '{{.State.Health.Status}}'`; on timeout dump logs + return `EX_UNAVAILABLE`).
- `up --tunnel` resolves `NGROK_AUTHTOKEN`/`NGROK_DOMAIN` from `env`; absent → log + `EX_CONFIG`
  **before any `run` call** (secrets-via-env fail-fast at the front door — the compose file uses `:-`
  defaults precisely so this CLI owns the fail-fast, not a parse-time interpolation error).

### Exposure
`pyproject.toml` gains: `[project.scripts]` → `hive = "hive.tools.cli:main"`. **No dependency added.**
`pip install -e .` on the host gives the `hive` command; until then `python -m hive.tools.cli <cmd>`
works from the repo (stdlib-only, consistent with the other tools).

---

## 5. Tests (written first — the enforced contracts; all pytest)

**`tests/container/test_cli.py` (new) — inject a fake `run`, assert argv + exit code:**
| Test | Contract |
|---|---|
| `test_up_uses_up_detached_not_run_rm` | `up` → fake `run` gets `["up","-d","--build","hive-server"]`; **never `run --rm`** (migrates the old `hive.sh` contract). |
| `test_up_loopback_by_default` | no flag → **no** `--profile tunnel` in the argv. |
| `test_up_tunnel_requires_secrets` | `up --tunnel` with `NGROK_AUTHTOKEN`/`NGROK_DOMAIN` **unset** → `EX_CONFIG`, **no** `run` call. |
| `test_up_tunnel_sets_profile` | `up --tunnel` with both set → argv contains `--profile tunnel`. |
| `test_up_health_timeout_dumps_logs_exits_unavailable` | fake `run` that never reports healthy → bounded wait → dumps logs, returns `EX_UNAVAILABLE` (mirrors `hive.sh _wait_healthy`). |
| `test_token_builds_authctl_create` / `test_revoke_builds_authctl_revoke` / `test_tokens_builds_authctl_list` | exact `… exec -T hive-server python -m hive.tools.authctl {create,revoke,list} …`; token = child **stdout only**. |
| `test_nuke_requires_confirmation` | without the typed confirmation, `down -v` is **not** issued. |
| `test_connect_renders_mcp_add_line` | output contains `claude mcp add --transport http hive https://<NGROK_DOMAIN>/mcp --header "Authorization: Bearer ${HIVE_TOKEN}"` **and** the seat hint `hive token <seat>` (AC7); **no `hive_init`**. |
| `test_missing_tenant_fails_fast` | missing `HIVE_TENANT_ID` → `EX_CONFIG`, no `run` call. |

**`tests/container/test_authctl.py` (extend):** `test_list_prints_provisioned_labels` — `create` two
labels, `list` prints both (one per line); empty store → no output, `EX_OK`.

**`tests/store/test_auth_store.py` (extend):** `test_labels_returns_sorted` — `labels()` returns the
created labels sorted; reflects `revoke`.

**Reuse:** `tests/container/test_compose.py` **drops** `_HIVE_SH` + `test_hive_up_uses_up_not_run_rm`
(its contract moves to `test_up_uses_up_detached_…`). Auth/entrypoint/healthcheck tests are **unchanged
and stay green** — proof the auth core + transport are untouched by the CLI.

**Successor extension (not this plan):** CV6 adds its `credit` argv tests to this same
`tests/container/test_cli.py` (CONVERGENCE-PLAN §9) — keep the fake-runner argv-assertion helper
reusable, not test-local.

---

## 6. Design decisions (design-it-twice) — chosen vs. rejected

**D1 — Language/runtime for the admin CLI (RESOLVED by `/software-design-review`).**
- *Rejected — bash (`hive.sh` kept/extended):* zero toolchain, the minimal floor, but fragile for real
  UX (flags/validation/help) and effectively untestable (the current "test" just reads the file text).
- *Rejected — external Go binary:* a single static binary is attractive in the abstract, **but the
  payoff doesn't cash in here**: the admin host *is* the repo+Python+Docker host, so "no Python on the
  host" is false; teammates run nothing (they copy-paste `hive connect`'s output). Against nil benefit
  it imports a **second toolchain**, a build/release step, and a cross-language boundary that raises
  agent context cost and **fights the zero-dep / one-language convention**. It also tempted a schema
  leak (`SELECT … FROM access_tokens` in Go).
- **Chosen — stdlib-Python `console_scripts` CLI:** identical UX, **zero new dependency**, **one
  language** (lowest agent context cost, idiomatic), reuses the pytest harness + the project's
  `connect_fn`/`out`/`run` injection idiom, fulfils the docs' `hive <cmd>` intent. Higher on every
  rubric dimension for *this* deployment. *(If a standalone, repo-independent ops binary ever becomes a
  real requirement, revisit Go then — it is not one today.)*

**D2 — Depth without crossing the M11/M12 boundary.** The CLI owns the *admin* workflow but must **not**
absorb the handshake. `hive connect` emits the `claude mcp add` transport line *only*; `hive_init` stays
pure-MCP, agent-driven. *Rejected:* a `hive init`/`onboard` performing the handshake — it would split
the trust boundary off the server (what `01-DECISIONS.md` D4 rejected).

**D3 — The `run()` injection seam.** One injected callable hides "we shell to `docker compose`". Gains:
unit-testability (assert argv, no Docker) and a future substrate swap (podman/k8s) with no command-code
change. The *same* idiom `authctl` (`connect_fn`) and `entrypoint` (`run_http`) already use. *Rejected:*
commands calling `subprocess.run` directly — untestable, leaks docker into every verb. The seam's first
external consumer is already named: CV6's `credit` widens it keyword-only (`input=` stdin piping for the
NDJSON ingest) — recorded in §4, additive.

**D4 — Exposure opt-in as a flag, not stateful `.env` mutation.** `hive up --tunnel` adds
`--profile tunnel` for that invocation; default is loopback. *Rejected:* a `hive bind`/`hive expose`
that edits `.env` — stateful, surprising, makes exposure sticky/invisible.

**D5 — Shell out; never reimplement security logic, never embed SQL.** `hive token`/`tokens`/`revoke`
invoke `python -m hive.tools.authctl` — the 256-bit mint, sha256-at-rest, **and the `access_tokens`
schema** live only in the adapter (already TDD- + mutation-verified; `labels()` joins them additively).
*Rejected:* the CLI holding its own SQL or hashing — duplicate security-critical knowledge is the real
hazard. The CLI hides the *invocation*, not the logic.

**D6 — stdlib `argparse`, no `click`/`typer`.** Keep the zero-dependency invariant; ~9 verbs don't need
a framework. *Rejected:* `click`/`typer` — an unjustified runtime dependency.

**D7 — `hive credit` is CONVERGENCE-owned; this plan ships its landing surface only (added 2026-06-11).**
The outcome-credit verb — host-side `Hive-Trace` trailer scan, merged=win / reverted=loss by rev-list
ancestry, NDJSON piped to in-container `creditctl ingest` (the authctl pipe pattern exactly) — is fully
specified in CONVERGENCE-PLAN §8/§9 with its boundary locked in 01-DECISIONS D10/D-C5: the server never
reads repos; the scan is operator-cadence, *accelerant never fuel*. Re-specifying it here would fork the
spec. This plan's obligations to it are exactly three (held by AC8): a dispatch dict that grows by one
entry, a runner seam widenable with `input=` (§4), and the sibling-tool import rule (§2 — host-importable,
stdlib-only). *Rejected:* absorbing CV6 into this plan — it has its own files/tests/RULE-2 ladder and
lands as its own green step once this CLI exists.

---

## 7. Dependencies — **none new (runtime or dev).**
Stdlib only (`argparse`, `subprocess`, `os`, `sys`). `pyproject.toml` gains a single `[project.scripts]`
entry-point line (a declaration, not a dependency). No Go toolchain, no build step.

## 8. Implementation order (each green before the next) — *prerequisite `REMOTE-ACCESS-PLAN.md` LANDED 2026-06-10; nothing blocks this plan*
1. **`authctl list` + `SqliteTokenStore.labels()`** (+ tests) — additive, single-sources the schema.
2. **CLI skeleton** — `run()` seam + fake, dispatch dict, exit codes (+ tests).
3. **Lifecycle verbs** — `up` (+`--tunnel`/health-wait) / `down` / `logs` / `nuke` / `status` (+ tests).
4. **Provisioning verbs** — `token` / `revoke` / `tokens` / `connect` (+ tests).
5. **Remove `hive.sh`**; migrate its one Python test (done in steps 1/3); add `[project.scripts]`.
6. **Docs + `CLAUDE.md`** — README/AUTH-PLAN §10 runbook to the `hive` CLI; note the new entry point +
   `python -m hive.tools.cli` invocation in the instruction layer.

Safe because: steps 1–4 are **additive** (the suite stays green throughout); no server/transport/auth
code is modified — only *added to* (`labels`/`list`).

**Landing step 6 unblocks CONVERGENCE CV6** — `credit` then rides the dispatch table as its own green
step (`hive/tools/creditctl.py` NEW, `cli.py` + one dict entry, `tests/container/test_cli.py` + argv
tests; spec: CONVERGENCE-PLAN §8/§9 — not re-specified here, §6 D7).

## 9. Mutation testing protocol (RULE-2; per chunk)
- **`authctl list`:** make `labels()` drop its `ORDER BY` / return `[]` → the list/labels tests red.
- **CLI:** make `up` use `run --rm` → `test_up_uses_up_detached_…` red; drop `-T` on the authctl exec →
  token test red; drop the `--tunnel` secret assert → `test_up_tunnel_requires_secrets` red; flip the
  `--tunnel` profile add to always-on → `test_up_loopback_by_default` red; skip the health-wait timeout
  → `test_up_health_timeout_…` red. Restore → green, report.

## 10. Security model
- **Secret-safe CLI:** `hive token` surfaces the credential on stdout only; never logs it.
  `hive up --tunnel` reads `NGROK_AUTHTOKEN` from env and **fail-fasts** if absent (never defaults).
- **No new attack surface:** minting stays operator-on-host; the CLI adds no network endpoint,
  reimplements no crypto, embeds no SQL (D5). The internet-facing hardening lives in
  `REMOTE-ACCESS-PLAN.md §10`.
- **Default-safe:** loopback unless the operator types `--tunnel`. **Rollback exposure:** `hive up`
  (no flag) → loopback again.

## 11. Files touched / removed
- **Add:** `hive/tools/cli.py`; `tests/container/test_cli.py`.
- **Modify (additive):** `hive/adapters/auth_store_sqlite.py` (+`labels()`); `hive/tools/authctl.py`
  (+`list`); `tests/container/test_authctl.py` + `tests/store/test_auth_store.py` (+tests);
  `pyproject.toml` (+`[project.scripts]`); `tests/container/test_compose.py` (drop `_HIVE_SH` test).
- **Remove:** `hive.sh`.
- **Docs → `hive` CLI:** `AUTH-PLAN.md §10` (`docker compose exec … authctl` → `hive token/revoke`);
  `CLAUDE.md` (note the `hive` / `python -m hive.tools.cli` entry point); `HOOK-RELOCATION-PLAN.md`
  (`./hive.sh up` → `hive up`). The many `./hive up` references elsewhere are **already** in the
  intended `hive up` form — the console-script fulfills them; spot-confirm wording only.
  `docs/CLIENTS.md` + the CV1-landed seat-token sections already use the final `hive token <seat>`
  form — landing this plan makes those promises true; **no edit**, spot-confirm only.

## 12. Deferred / out of scope (add-back paths)
| Deferred | Add-back |
|---|---|
| `hive doctor` (preflight: docker present, compose valid, tenant set, tunnel reachable) | a `doctor` handler composing existing checks |
| `hive migrate import-corpus …` | a `migrate` handler shelling to `python -m hive.ops.migration` |
| Random-URL ngrok (no static domain) for `hive connect`/`status` | read the ngrok agent API at `:4040/api/tunnels` |
| A standalone, repo-independent ops binary | revisit the Go option **only if** that becomes a real requirement (D1) |

---

**This plan replaces `hive.sh` with an in-language, stdlib-only operator CLI** that *orchestrates* the
existing deep substrate (compose + the in-container tools + the ngrok sidecar from
`REMOTE-ACCESS-PLAN.md`) behind the simplest verbs, single-sourcing all token logic + schema in the
Python it shells to. It changes **no** server, transport, or auth code — only *adds* `labels()`/`list`.
The CLI-language decision was design-reviewed and resolved to Python (§6 D1) — zero new dependency, one
language, best fit. Landing it is the last prerequisite for **CONVERGENCE CV6**: the `hive credit` verb
then closes the git-artifact feedback loop — `Hive-Trace` commit trailers → outcome credit →
`task_outcomes` → the readiness→keystone gate — on this dispatch table (§6 D7), and the seat-token
contract the landed docs already promise (`hive token <seat>`) becomes a real command (AC7).
