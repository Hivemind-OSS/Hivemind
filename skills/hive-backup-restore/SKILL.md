---
name: hive-backup-restore
description: "Snapshot, reset, or restore the Hivemind data store via the `hive` CLI. Use when asked to back up / snapshot the store, wipe and recreate it empty, recover from a post-upgrade boot crash-loop, or roll the store back to a prior snapshot. `reset` and `restore` are destructive and require a typed confirmation."
---

# hive-backup-restore — snapshot, reset & restore the store

The data lives in the `hive-data` Docker volume (in-container path `/data/shared.db`). Full
reference: `HIVE-ADMIN.md` §1 & §5.

**CLI resolve (once per shell):** `command -v hive >/dev/null 2>&1 || hive() { python3 -m hive.tools.cli "$@"; }`
— makes every `hive …` line below run on an uninstalled checkout (the CLI is stdlib-only;
Windows shells: `py -m hive.tools.cli <verb>`; prerequisites: **hive-bringup**).

## `hive backup` — safe snapshot

```bash
hive backup        # snapshots the warm store NOW; prints the snapshot path
```
Manual (no scheduler) — run it on whatever cadence you like. Keeps the
`HIVE_RETENTION__BACKUP_KEEP` (default 30) most-recent snapshots in `HIVE_RETENTION__BACKUP_DIR`
(default `<db_dir>/backups`, **inside the volume**). For a copy that survives the volume being
destroyed, use `reset`'s host snapshot (below) or copy a snapshot out.

## `hive reset` — recoverable clean-start (DESTRUCTIVE)

```bash
hive reset                 # asks you to type 'reset' to confirm
hive reset --out ./snaps   # choose the host snapshot dir (default ./hive-backups)
hive reset --yes           # skip the prompt (scripted dev only)
```
In order: (1) snapshots the store **out to the host** — a pure file-level copy that works even when
the daemon can't boot against an incompatible store; (2) **aborts before any destruction if that
snapshot fails** (fail-safe — an accidental reset costs a restart, not the memory); (3) only then
`down -v` + recreate the volume empty + warm. It prints the host snapshot path and the exact
`hive restore …` line to roll back.

**Primary use: recovering from a schema-generation crash-loop** (`hive up` failing after a rebuild —
see **hive-bringup**). Also a deliberate clean slate. Prefer this over a bare
`docker compose down -v`, which destroys the store with **no** snapshot.

## `hive restore <snapshot.db>` — roll back (DESTRUCTIVE, inverse of reset)

```bash
hive restore ./hive-backups/hive-<stamp>.db    # asks you to type 'restore' to confirm
hive restore <snap> --yes                      # skip the prompt
```
Stops the daemon (releases the WAL locks), overwrites the live store with the snapshot (clearing
stale WAL sidecars), then rebuilds + restarts. Works with either a `reset`-produced host snapshot or
a `hive backup` snapshot. After it returns, recall reflects the snapshot's contents — re-check the
KPIs (**hive-operate**).

## Rules

- `reset` and `restore` are the **only** destructive verbs and both gate on a typed word; `down`
  always preserves the volume.
- `reset` is recoverable **by construction** (snapshot-first, abort-on-fail). Never substitute a
  hand-rolled `down -v`.
