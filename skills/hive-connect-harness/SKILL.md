---
name: hive-connect-harness
description: "Install the optional agent-loop harness — the hive-loop Claude Code plugin — on a workstation and point it at a running Hivemind server: on your own device, or as a copy-pasteable block an admin forwards so a teammate's agent installs it at a pinned commit. Use when asked to install / enable / roll out the harness or the hive-loop plugin, make the memory loop mechanical or enforced client-side, onboard a teammate to the harness, or diagnose an installed harness that appears to do nothing."
---

# hive-connect-harness — install the client-side harness

Add the **optional** client-side enforcement layer on top of the MCP connection
**hive-connect-team** already made. Not a substitute for it — without that connection there is
nothing to enforce against — and not a server operation: installing or removing the harness changes
no server behavior (**hive-bringup** owns the server). Full reference: `harnesses/README.md`;
operator posture: `HIVE-ADMIN.md` §9.

The harness is a Claude Code **plugin** whose root is this repo's `harnesses/` directory. There is
no build step and no package to publish — Node runs the committed TypeScript directly. **A `git
clone` alone installs nothing:** the files are inert until the plugin is installed *and* the session
restarts.

## Prerequisites

| Need | Check | If it fails |
|---|---|---|
| An endpoint this workstation can reach | on the server host, `hive connect` prints the URL for its current posture | run **hive-connect-team** first |
| **Node ≥ 23.6** | `node --version` | upgrade. Below this the install still succeeds and the harness does nothing, with no message |
| Claude Code | `claude --version` | the only adapter that ships today |

## Case 1 — this device

**1 · Put the endpoint in a shell profile, not in this shell.** The plugin's manifest interpolates
these at session start, so a session launched from a desktop icon or a fresh terminal must see them
too:

```bash
# append to ~/.bashrc or ~/.zshrc — NOT just `export` at a prompt
export HIVE_MCP_URL=http://localhost:8765/mcp   # loopback: no token needed, leave HIVE_TOKEN unset
# remote seat instead:
#   export HIVE_MCP_URL=https://<your-domain>/mcp
#   export HIVE_TOKEN=<seat-token>              # from `hive token <seat>`, never a literal in a file you commit
```

**2 · Install it at user scope.** Agents work in repositories other than this one, so a user-scope
install is the one that covers them all:

```bash
mkdir -p ~/.claude/skills
ln -sfn /path/to/hivemind/harnesses ~/.claude/skills/hive-loop
```

A symlink means `git pull` in the checkout updates the installed plugin. Copy the directory instead
if you want the installed version pinned independently of the checkout.

**3 · Restart Claude Code.** Hooks load at session start, so the session you ran this from is not
governed — only the next one is.

For one invocation without installing anything — trying it out, or CI:
`claude --plugin-dir /path/to/hivemind/harnesses`.

## Case 2 — the block to forward

Fill the three placeholders and send this to the teammate; their agent can execute it as written.
**Send the seat token separately** — this block is written to be safe to paste into a chat.

Get the commit to pin from the server checkout — and confirm it is **pushed**, because a teammate
cannot check out a commit that never left your machine:

```bash
git rev-parse HEAD
git branch -r --contains HEAD   # must name a remote branch; empty output means push first
```

The clone URL must also be one the teammate can reach — a public repo, or one they have access to.

```text
Install the hive-loop harness (optional — it makes the memory loop mechanical on
your side; the server is unchanged either way).

  git clone --no-checkout <repo-url> ~/hivemind-harness
  cd ~/hivemind-harness
  git sparse-checkout set --no-cone harnesses
  git checkout <sha>
  mkdir -p ~/.claude/skills
  ln -sfn ~/hivemind-harness/harnesses ~/.claude/skills/hive-loop

That clones only the harness directory, pinned to the commit the server runs.

Add these to your shell profile (~/.bashrc or ~/.zshrc), NOT just the current
shell — otherwise sessions you start any other way will not see them:

  export HIVE_MCP_URL=https://<public-url>/mcp
  export HIVE_TOKEN=<the seat token sent to you separately>

Requires Node >= 23.6 — check with `node --version`. On anything older this
installs cleanly and then does nothing, silently.

Then restart Claude Code. Hooks load at session start, so your current session is
not governed. In the new session, confirm you have EIGHT tools whose names contain
hive — under either prefix, mcp__plugin_hive-loop_hive__* or mcp__hive__*. If you
have none, see the checks below.
```

If the teammate already ran `claude mcp add hive` — which is exactly what **hive-connect-team**
hands them — they now carry both that registration and the plugin's own. That combination is
**supported and measured**: the two servers share the name `hive`, the file-configured one wins,
and the agent sees eight tools under `mcp__hive__*` rather than sixteen under two prefixes.
Enforcement is unaffected, because the hook matchers never encode a prefix.

## Two routes to the endpoint — pick one

Both the `claude mcp add` registration and the plugin's manifest can carry the endpoint, and when
both exist **the registration wins silently**. That is cosmetic on loopback, where the two say the
same unchanging `http://localhost:8765/mcp`. It is not cosmetic for a remote seat, because the two
carry different things and drift apart:

| | endpoint + token live in | rotating them means |
|---|---|---|
| `claude mcp add hive …` (**hive-connect-team**) | `~/.claude.json` — the seat token baked in as a literal header | re-running `claude mcp add` |
| the plugin's manifest (**this skill**) | `HIVE_MCP_URL` / `HIVE_TOKEN` in a shell profile | editing the profile |

So a teammate carrying both who is handed a fresh seat, updates `HIVE_TOKEN`, and restarts still
presents the **old** token — the winning registration never read the variable. The failure looks
like "I just updated the token and now I get 401s," and the 401 row in the table below sends you
around the same loop again. A moved tunnel URL fails the same way.

**For a remote seat, prefer the plugin's manifest alone:** skip `claude mcp add` and let
`HIVE_MCP_URL` / `HIVE_TOKEN` be the single source of truth, so rotation is a profile edit and the
token stays out of `~/.claude.json`. If the registration is already there, that is fine too — just
rotate *it*, and treat the environment variables as inert. What does not work is keeping both and
assuming the environment is authoritative.

If you keep both, confirm they name the same endpoint. Nothing reports it when they do not.

## Verifying it is governing

In a **new** session, on the machine you installed it on:

| Check | Expected |
|---|---|
| `claude plugin list` | `hive-loop@skills-dir` · Scope: user · Status: `✔ loaded` |
| ask the agent to list its tools containing `hive` | **eight** — under whichever prefix applies, see below |
| ask it to edit a file before it has recalled | the call is denied, with a reason naming the missing recall |

The last row is the only one that proves the *hooks* are live; the first two prove the plugin and
its endpoint declaration are. Check all three — they fail independently.

**Count eight; do not check the prefix.** Which prefix the verbs arrive under depends on what else
declares a `hive` server, and both are correct — enforcement is identical, since the hook matchers
are prefix-agnostic by construction:

| Situation | The eight arrive as |
|---|---|
| the plugin is the only thing declaring `hive` (a teammate who only ran Case 2) | `mcp__plugin_hive-loop_hive__*` |
| a `hive` server is already registered in a config file — `claude mcp add hive`, i.e. anyone who followed **hive-connect-team** | `mcp__hive__*`; the file-configured server **shadows** the plugin's identically-named one |

**When it does nothing.** Every failure here is quiet, so diagnose by elimination:

| Symptom | Cause |
|---|---|
| no hive tools at all, under **either** prefix | `HIVE_MCP_URL` unset in the session's environment — the manifest cannot resolve, and nothing reports it |
| eight tools, but named `mcp__hive__*` | **not a fault** — a file-configured `hive` server outranks the plugin's declaration. Confirm the two name the same endpoint |
| tools present, nothing is ever denied | hooks did not load: the session predates the install (restart), or Node is < 23.6, or `HIVE_LOOP__ENABLED=0` |
| tools present, denials work, calls 401 | the endpoint is the remote door and `HIVE_TOKEN` is unset or revoked — mint a fresh seat with `hive token <seat>` |
| hive tools vanish in a headless `claude -p` run | `--mcp-config` (and its stronger form `--strict-mcp-config`) replaces the **plugin-declared** server set. Drop the flag, or declare the server in the `--mcp-config` payload yourself |
| a rotated seat token or a moved endpoint changes nothing | a file-configured `hive` registration is winning, and it carries its own baked URL and token. See *Two routes* above — edit the registration, not the environment |

## Turning it off

`HIVE_LOOP__ENABLED=0` makes every hook byte-inert. `claude --bare` skips hooks for one invocation,
and a session that never loaded the plugin is not governed at all. To uninstall, remove the symlink
(`rm ~/.claude/skills/hive-loop`) and restart. Enforcement is strong *inside* a governed session and
bypassable *at launch* — that is deliberate.

## Load-bearing invariants (do not relearn these the hard way)

- **`claude plugin install <path>` does not work.** That verb resolves a plugin *name* against a
  configured marketplace; given a path it fails with "not found in any configured marketplace",
  which reads like a broken plugin rather than a wrong command. This repo ships no marketplace
  manifest, so the two supported routes are the user-scope symlink above and `--plugin-dir`.
- **`claude plugin details hive-loop@skills-dir` reports `MCP servers (0)` even when all eight tools
  are live.** It inventories hooks accurately and the endpoint declaration not at all. Trust the
  session's tool list, never that line.
- **An identically-named `hive` server shadows the plugin's declaration, silently.** The
  file-configured one wins; you get eight tools under `mcp__hive__*` and no warning that a second
  declaration was dropped. Harmless when both name the same endpoint, and a wrong-server bug when
  they do not.
- **`--mcp-config` drops plugin-declared servers**, and `--strict-mcp-config` is the stronger form
  of the same thing. Measured: with either, the session has zero hive verbs, so an armed headless
  run can never satisfy the loop. It happens even when the injected server's name does not collide,
  so it is the flag and not a name conflict. `--setting-sources ""` does *not* do this.
- **The loopback door needs no `HIVE_TOKEN`.** An unresolved `${HIVE_TOKEN}` in the manifest does
  not drop the server: measured, all eight verbs load and a real `hive_recall` returns over
  `http://localhost:8765/mcp` with the variable unset.
- **Installed is not enforcing.** Hooks load at session start. Every install, upgrade and toggle
  here takes effect on the *next* session, and there is no warning that the current one is ungoverned.
- **The environment must outlive the shell.** An `export` at a prompt reaches only sessions launched
  from that prompt. In a profile it reaches all of them.
- **The harness opens no socket and calls no hive verb.** It reads hook payloads and blocks or allows
  a call the agent itself chose to make. `HIVE_TOKEN` is read by Claude Code for the MCP connection,
  never by the harness.
- **The harness is not a second contract.** It states no memory semantics — what to store, what a
  label means, when something is promoted are the served contract's to say, and it reaches every
  session over MCP at connect. A harness left at an old commit therefore costs *enforcement*, never
  *correctness*: what the agent is told to do stays current on its own.
