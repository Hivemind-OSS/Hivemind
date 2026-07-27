---
name: hive-upgrade
description: "Move a running Hivemind server to a different release ref safely via `hive upgrade` — snapshot-gated, health-verified, auto-rolling-back — after a schema pre-flight that proves the target ref will accept the store you already have. Use when asked to upgrade / update / move / bump the hive server to a new version, tag, or release; to roll back to an older ref; or to check whether a new version is safe to adopt before committing to it. This build has NO in-place store migration, so a schema-breaking ref is a data-retention decision, not a routine update."
---

# hive-upgrade — move to a release ref without losing the store

`hive upgrade` moves the **server's checkout** to a different git ref and rebuilds it. That is
different from `hive up` (which rebuilds whatever is already checked out) and from `hive restore`
(which replaces store contents, not code). Lifecycle basics: **hive-bringup**. Snapshots and
recovery: **hive-backup-restore**. Full reference: `HIVE-ADMIN.md` §8.

**CLI resolve (once per shell):** `command -v hive >/dev/null 2>&1 || hive() { python3 -m hive.tools.cli "$@"; }`
— makes every `hive …` line below run on an uninstalled checkout (the CLI is stdlib-only;
Windows shells: `py -m hive.tools.cli <verb>`).

## The one thing that can actually cost you data

**This build has no in-place store migration.** `SqliteEpisodeStore.__init__` refuses to open a
store whose shape does not match the code, on purpose — a `CREATE IF NOT EXISTS` would leave an
old table in place and limp to a cryptic mid-query crash. The refusal surfaces as a **boot exit
70** and a crash-loop, with `store.schema_pre_v3` (or `store.schema_predates_v3` /
`store.schema_missing_episode_anchors`) in the logs.

So a schema-breaking release is never a routine update — it is a decision about whether to keep
the corpus. `hive upgrade` protects the *data* automatically; it cannot make an incompatible ref
adoptable.

**`hive up` after a manual `git checkout` is the dangerous path** — no snapshot gate, no health
gate, no rollback. If the new ref refuses the store you get a crash-loop against a store nothing
snapshotted. Always move refs with `hive upgrade`.

## 1. Pre-flight — will the target ref accept this store?

Run this **before** anything else. It reads the target ref via `git show` + `ast` (never imported,
never executed) and the live store `mode=ro`, then compares them against the three assertions the
target's own boot makes:

```bash
python3 skills/hive-upgrade/preflight.py release     # or any tag / branch / SHA
```

| Verdict | Exit | What it means | Do |
|---|---|---|---|
| `PASS` | 0 | the target ref's schema contract accepts the live store | continue to step 2 |
| `SCHEMA BREAK` | 1 | the target will refuse this store at boot; it names the exact columns/tables | **stop** — go to "when the pre-flight fails" |
| `UNKNOWN` | 2 | the contract could not be read (ref predates these constants, or they were renamed) | **stop and treat as a break** — it is not proven safe |

It **fails closed**: only `PASS` means safe. `UNKNOWN` is never "probably fine."

Requires the server to be **up** (it reads the live store). If it is down, bring it up first
(**hive-bringup**) or run the pre-flight after a `hive up`.

## 2. Clear the conditions `hive upgrade` aborts on

Clear these first so the verb does not abort halfway through your maintenance window:

```bash
git status --porcelain     # MUST be empty — a dirty tree aborts (no auto-stash)
hive status                # server up + healthy, so the health gate means something
df -h .                    # room for a full store snapshot on the HOST
```

Know your rollback target before you start:

```bash
git rev-parse --short HEAD   # this is what an auto-rollback returns you to
```

## 3. Upgrade

```bash
hive upgrade                       # default ref: "release" — the maintainer's vetted pin
hive upgrade --ref v1.2.0          # any tag / branch / SHA
hive upgrade --ref <older-tag>     # this is also how you deliberately go backwards
hive upgrade --yes                 # skip the typed 'upgrade' confirmation (scripted only)
```

Without `--yes` it asks you to type `upgrade`, echoing the ref and the rollback target first.

What it does, in this order — snapshot-first, abort-before-destroy:

1. **dirty tree aborts** — nothing changed;
2. `git fetch --tags`, then assert the ref **resolves** — still nothing changed;
3. capture `HEAD` as the rollback target;
4. take a **host** snapshot into `./hive-backups/` that **must succeed before the checkout** — if
   the snapshot fails, the ref is never checked out;
5. `git checkout <ref>` → `compose up -d --build` → bounded health-wait → app status gate;
6. **any** post-checkout failure auto-reverts *both* code and store (`git checkout <prev>` +
   restore the snapshot into the volume + rebuild + re-verify health).

Failures fall toward the recoverable direction: before the snapshot nothing has changed; after it,
the pre-upgrade store is always on the host.

## 4. Verify the upgrade actually landed

```bash
hive status                                   # up (healthy) — the gate the verb already applied
hive logs | tail -40                          # no schema refusal, no restart loop
git rev-parse --short HEAD                    # you are on the ref you asked for
```

Then confirm the server is doing real work, not merely healthy:

```
hive_health(include_census_health=true)
```

Read the `fleet` block first: `last_error` must be null and `last_sync_ts` must ADVANCE across two
poll intervals. That block is the daemon itself — if the new image cannot read the registry at
all, the tick dies in the shell before touching any repo, and every `repos` block below stays
frozen at its pre-upgrade values and still reads as passing. Then check each entry in `repos`
still has its `sync` sub-block with `tracked_ref` + `last_tip` set, no fresh `last_error`, and its
own `last_sync_ts` advancing. The repo registry and mirrors live in the `hive-data` volume, so an
upgrade does not deregister anything — a repo that went dark after an upgrade is a real
regression, not expected churn (see **hive-connect-repo**). Finally, run one real `hive_recall` against a repo you
know has memories: healthy + empty recall is the signature of a store that came up but lost its
index.

## What the exit codes tell you

| Exit | Meaning | State you are left in |
|---|---|---|
| `0` | upgrade complete, health-gated | on the new ref; snapshot kept in `./hive-backups/` |
| `64` | usage — dirty tree, unresolvable ref, or unconfirmed | **nothing changed** |
| `69` | fetch unreachable **or** pre-upgrade snapshot failed | **nothing changed** — the checkout never ran |
| `69` | `git checkout <ref>` itself failed | server unchanged; the snapshot path is printed |
| `69` | the new ref came up unhealthy and rollback **succeeded** | back on the old ref, store restored |
| `70` | the **rollback itself** failed | see below — the exact recovery is printed |

Exit `69` after a failed upgrade is the *designed* outcome, not a bug: the upgrade failed and the
server is back where it started.

## When the rollback itself fails (exit 70)

The verb prints the exact two-line recovery before exiting. Run it verbatim:

```bash
git checkout <prev-sha>
hive restore ./hive-backups/hive-<stamp>.db
```

You are never left without a printed way back. If `hive restore` also fails, the snapshot file is
still a plain SQLite database on the host — treat it as the source of truth and see
**hive-backup-restore**.

## When the pre-flight fails (SCHEMA BREAK / UNKNOWN)

There is no migration to run. Decide explicitly, and do not discover this by attempting the
upgrade:

- **Stay put (default).** Remain on the current ref. Nothing is wrong with a running server whose
  data is intact; a version bump is not worth the corpus.
- **Adopt the ref and start clean — the corpus does not carry over.** Snapshot for the record,
  then reset and upgrade:

  ```bash
  hive backup                       # in-volume snapshot; note the printed path
  hive reset --out ./hive-backups   # host snapshot FIRST, then recreate the volume empty
  hive upgrade --ref <ref>          # now trivially compatible: an empty store takes any schema
  ```

  Keep the pre-reset snapshot. It is not restorable into the new build (that is what the pre-flight
  told you), but it is still readable with any SQLite client if you need to mine the old corpus.
- **Re-register afterward.** A reset clears the repo registry with everything else, so
  re-run `hive repo add …` per repo (**hive-connect-repo**) and re-verify each feed.

Do **not** try to force it by editing the store by hand. The refusal is a correctness gate, not an
inconvenience.

## Load-bearing invariants (do not relearn these the hard way)

- **Never move refs with `git checkout` + `hive up`.** That path has no snapshot, no health gate,
  and no rollback. `hive upgrade` is the only ref-moving verb that is safe by construction.
- **The snapshot gate is absolute.** If the pre-upgrade host snapshot fails, the checkout never
  happens. A failure before the snapshot changes nothing; after it, the store is always on the host.
- **The health gate is the adoption test.** "Built successfully" is not "works" — the verb requires
  a bounded health-wait *and* an app status probe before it calls the upgrade good.
- **Rollback restores code AND store**, in that order, releasing WAL locks with a `compose down`
  before copying the snapshot back.
- **A rebuildable cache may be dropped at boot; that is not a schema break.** When a derived
  table's KEY changes shape, the new build drops and recreates it (the current instance:
  `anchor_drift`, whose key gained the per-binding baseline commit). sqlite cannot alter a primary
  key in place, and the table is a Law-5 cache, so this needs no migration and the pre-flight does
  not flag it — it only checks that the table EXISTS. The visible effect is one tick of
  `drift.type: "unverifiable"` on anchored recall hits — the honest unknown, never a false
  verdict — until the daemon re-materializes. Episode rows, anchors and memory text are never
  read or rewritten.
- **Config knobs are boot config; the repo registry is not.** The rebuild re-reads `.env`, and an
  env var the new version no longer knows is **ignored with a WARN, never a switch** — so a knob
  that was renamed or removed silently stops doing anything while your `.env` still lists it.
  Re-check `HIVE_SYNC__*` and the recall/safety knobs against the new version's tables after a
  major bump (**hive-operate**), and read the boot logs for unknown-field warnings. The synced-repo
  registry lives in the store, not in config, and survives untouched.
- **`hive upgrade` is for a different ref; `/update-dogfood-server` is for the current working
  tree.** They are not interchangeable — the latter deliberately never calls this verb.
