---
name: hive-bringup
description: "Stand up, restart, stop, or health-check the Hivemind MCP server on its host via the `hive` CLI. Use when asked to start / boot / bring up / restart / stop the hive server, check whether it is healthy, follow its logs, or diagnose a boot crash-loop. Covers the zero-config first boot, the bounded health-wait, and the schema-refusal failure mode."
---

# hive-bringup — start, stop & health-check the server

Bring the Hivemind daemon up on its host and confirm it is serving. Lifecycle only: to connect
agents use **hive-connect-team**, to snapshot / reset the store use **hive-backup-restore**, to tune
a running server use **hive-operate**. Full reference: `HIVE-ADMIN.md` §1 & §5.

## Prerequisites (server host)

- **Docker** + **Docker Compose v2**, and **Python 3.11+** (the `hive` CLI drives Compose).
- The `hive` command: `pip install -e .` from the repo root (on a PEP-668 "externally-managed"
  Python, inside a venv — or skip the install entirely: the CLI is stdlib-only). Uninstalled,
  every `hive …` below is exactly `python3 -m hive.tools.cli …` (Windows: `py -m hive.tools.cli`).
  Resolve the form mechanically,
  once per shell, and every literal command below works either way:

```bash
command -v hive >/dev/null 2>&1 || hive() { python3 -m hive.tools.cli "$@"; }
```

## Bring it up

```bash
cp .env.example .env   # optional: makes the persistent-store choice explicit (sets HIVE_STORE__DB_PATH)
hive up                # zero-config: builds the image, warms the embedder, blocks until healthy
```

- **The store persists by default.** The containerized daemon DEFAULTS to `/data/shared.db` in the
  `hive-data` volume (the entrypoint injects it when the env is unset); only an explicit
  `HIVE_STORE__DB_PATH=:memory:` boots ephemeral — such a boot WARNs loudly
  (`container.store_ephemeral`) and `hive_health` reports `store_ephemeral`. `cp .env.example .env`
  makes the persistent choice explicit. Edit `.env` for any other override (**hive-operate**) or the
  tunnel secrets (**hive-connect-team**).
- **The first `hive up` is slow, and that is normal** — it builds the image and bakes the offline
  embedder (no network at runtime). It is not hung.
- `up` then **blocks on a bounded health-wait** (default 180 s; "healthy" ≡ the embedder is
  resident). On a slow host raise it: `HIVE_HEALTH_TIMEOUT=600 hive up`. On timeout or unhealthy it
  dumps recent logs and exits non-zero.

Confirm:

```bash
hive status    # → "server: up (healthy)" + tunnel state + seat count
```

## Stop / restart / logs

```bash
hive down      # stop the stack; PRESERVES the hive-data volume
hive up        # restart (config is read only at boot — restart to apply any .env change)
hive logs      # follow the daemon logs (Ctrl-C detaches); `hive logs ngrok` for the tunnel
```

## Failure modes

- **`hive up` crash-loops right after pulling / rebuilding a new build** ⇒ the `hive-data` volume
  predates the current schema. This build **refuses old-format tables at boot — no silent
  migration.** Recovery is a single `hive reset` (it snapshots the old store out to the host first,
  so it is recoverable; `hive restore` rolls back) — run it via **hive-backup-restore**. Do **not**
  hand-roll `docker compose down -v`: that destroys the store with no snapshot.
- **Boot is a fail-fast state machine** — `config → migrate → index → embedder.warm → serve.ready`.
  The exit code is the contract: `0` clean · `78` bad config · `70` an internal boot step failed ·
  `69` the embedder never became resident (no recall possible — treat as unhealthy).
- **Exit `78` names what to fix — read the last ERROR line, do not guess.** Its causes: an
  out-of-range `.env` knob (it fails loudly, never clamps) · a registered repo's `--token-env`
  var absent from the server's environment (`entrypoint.token_env_missing`, naming the var) ·
  the store's directory not writable by the container's user (`entrypoint.db_dir_not_writable`,
  naming the path and uid — a data dir whose ownership does not match, rather than the
  `hive-data` volume this stack creates for you) · both HTTP doors resolved onto one port
  (`entrypoint.bind_port_collision`, only reachable if you set the address knobs at all).
- **No live reload** — config applies only at boot. After any `.env` edit, `hive up` to restart.

## Verify it actually serves (optional smoke test)

Connect one local agent (**hive-connect-team**) and round-trip `hive_recall("anything")` → expect an
empty / abstained result on a fresh store (not an error), then `hive_capture(...)` → expect an ack.
That proves transport + embedder + store end to end.

Or open the dashboard: `hive ui` serves the loopback operator console in your browser — the live
SERVER card shows up/down + health + tunnel + seat count at a glance (`--no-open` on a headless
host, then browse `http://127.0.0.1:4173/` over an SSH tunnel). It is loopback-only; it can activate
the tunnel and restore from an in-volume backup (guarded, typed confirm), but exposes no reset.
