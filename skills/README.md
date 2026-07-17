# Hivemind operator skills

Runbook-skills for the load-bearing `hive` operations, so an agent (or a human) can perform them
without rediscovering the process each time. They ship with the repo; the authoritative long-form
reference is `HIVE-ADMIN.md` (and `OPERATIONS.md` for the tuning evidence).

| Skill | Use it to |
|---|---|
| [`hive-bringup`](hive-bringup/SKILL.md) | start / stop / restart / health-check the server; diagnose a boot crash-loop |
| [`hive-connect-team`](hive-connect-team/SKILL.md) | connect a local agent or a remote teammate (loopback / tunnel / SSH); mint & revoke seat tokens |
| [`hive-connect-repo`](hive-connect-repo/SKILL.md) | arm the server-side automatic census feed against a GitHub repo (`HIVE_SYNC__*`) and test the connection works; auto-detects the default branch |
| [`hive-backup-restore`](hive-backup-restore/SKILL.md) | snapshot, reset (recoverable clean-start), or restore the data store |
| [`hive-operate`](hive-operate/SKILL.md) | read the convergence KPIs over MCP and turn the recall / safety knobs; watch the server-side automatic census feed (`HIVE_SYNC__*`) and feed manual receipts (`hive ingest`); open the loopback browser dashboard (`hive ui`) |

Each is a self-contained `SKILL.md` with trigger-style frontmatter (`name` + `description`).

## Activating them as live skills

These files double as documentation and as ready-to-run skills. To make them invocable in a coding
agent that loads skills from `.claude/skills/` (e.g. Claude Code), copy or symlink the skill folders
there:

```bash
for s in hive-bringup hive-connect-team hive-connect-repo hive-backup-restore hive-operate; do
  ln -s "../../skills/$s" ".claude/skills/$s"
done
```

Then `/hive-bringup`, `/hive-connect-team`, etc. resolve directly. Otherwise an agent can just read
the relevant `SKILL.md`.
