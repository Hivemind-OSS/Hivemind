---
name: hive-connect-team
description: "Connect agents and teammates to a running Hivemind server: pick the transport (local loopback / ngrok tunnel / SSH), mint and hand off per-seat tokens, register the MCP client, and revoke seats. Use when asked to connect / onboard / add / provision an agent or teammate, expose the server to remote machines, share the hive, or offboard a seat."
---

# hive-connect-team — connect agents & teammates

Register clients against a server that is already up (**hive-bringup**). Full reference:
`HIVE-ADMIN.md` §2 & §3.

## Pick the door

| Who | Door | Token? |
|---|---|---|
| An agent on the **server host** | loopback `127.0.0.1:8765` | **no** |
| A **teammate on another machine** | ngrok **tunnel** (recommended) or **SSH** forward | tunnel: yes · SSH: no |

`hive connect` always prints the correct ready-made `claude mcp add` line for the host's current
posture (the tunnel line when `NGROK_DOMAIN` is set, else the loopback line).

## Local agent (tokenless)

```bash
hive connect
claude mcp add --transport http hive http://localhost:8765/mcp
```

## Remote teammate — tunnel (recommended)

1. Free ngrok account → set both in `.env` (a plain `hive up` never exposes anything; `--tunnel`
   fail-fasts if either is missing):
   ```bash
   NGROK_AUTHTOKEN=...           # from the ngrok dashboard
   NGROK_DOMAIN=your.ngrok.app   # your account's static domain (stable URL)
   ```
2. Start the tunnel sidecar and mint a seat:
   ```bash
   hive up --tunnel              # starts the ngrok sidecar; forwards https://<domain>/mcp
   hive token alice-laptop       # prints the token ONCE — hand over via a secret manager
   ```
3. The teammate registers (the exact line `hive connect` prints once `NGROK_DOMAIN` is set):
   ```bash
   claude mcp add --transport http hive https://<your-domain>/mcp \
     --header "Authorization: Bearer <seat-token>"   # replace <seat-token> with the seat's token
   ```
   TLS terminates at the ngrok edge, so the token is encrypted in transit.

## Remote teammate — SSH (no extra accounts)

```bash
ssh -NL 8765:localhost:8765 you@host    # forward the loopback door over SSH
```
Then the teammate uses the **local** loopback line above as-is — no ngrok, no token.

## Edge tools — install & stay current

Every connected participant (local or remote) also installs the **`hive-edge` CLI** and wires the
fail-open census hooks (post-merge + post-commit) — one install, then `hive-edge census init`:

```bash
uv tool install hive-edge --from git+https://github.com/Hivemind-OSS/Hive-edge@release
hive-edge census init --repo . --hive-url <the /mcp URL `hive connect` printed>
```

The wiring is per-device and succeeds even where the `hive` server CLI is absent — the hooks'
bytes are constant and they resolve binaries + config at run time on each device, staying inert
(fail-open) until they exist. Together the pair receipts every landing exactly once (post-merge:
clean merges; post-commit: direct commits + conflict-resolved merges) and keeps the persistent
per-repo code graph current. When both `hive-edge` and `hive` resolve, `census init` self-tests the
wiring immediately — a zero-diff receipt build in the hooks' own `GIT_DIR`/`GIT_WORK_TREE`
environment — and prints `self-test PASSED`/`FAILED`, so breakage is caught at wiring time rather
than only later in the hook logs (`~/.hive-edge/last-postmerge.log` / `last-postcommit.log`).

The full flow — the daily release nudge, `hive-edge upgrade`, the per-device state directory
(`~/.hive-edge/`), and the server's own `hive upgrade` — is **`HIVE-ADMIN.md` §8** (the single
source; not duplicated here).

## Seat hygiene & offboarding

- **One token per seat, never shared across agents.** `hive tokens` lists provisioned seat labels
  (never the tokens themselves).
- `hive revoke <seat>` — offboard a seat; its next request 401s.

## Load-bearing invariants (do not relearn these the hard way)

- **The token is not the identity.** Identity is **per-agent-session** — the server-minted
  `Mcp-Session-Id` a conforming client echoes, or an explicit `X-Hive-Agent-Id` header. The bearer
  only *authenticates* the tunnel door. That per-session diversity is the fuel that promotes
  captures, so a fleet of K agents promotes identically whether 1 or N engineers run it.
- **Auth is a property of the socket, not a config knob.** There is no `HIVE_AUTH__MODE`. Loopback
  is tokenless; the tunnel door (compose-internal `8766`) is token-required and is the only
  remote-reachable one. **Never publish `0.0.0.0:8765`** — a bearer token over plain LAN HTTP is
  cleartext.
- **Onboarding's floor is served — the operator installs nothing.** A terse behavioral floor
  (recall-first, capture-by-default, the value bar, an identity pointer) reaches every agent over
  MCP at connect via the `initialize` instructions; the only client-side step is the MCP
  registration above, and there is no handshake call. The full contract — the install procedure,
  the capture taxonomy, the identity/auth reference, the hooks, and the allowlist — is fetched on
  demand over `hive_health(include_onboarding=true)`. On top of the served floor an agent MAY
  optionally persist the contract as a **version-stamped rules block** in its own project rules
  file — the server beacons a `contract_version` on every result so a stale block re-onboards, and
  a missing block degrades to the served floor — but that is the agent's own act, never an operator
  step. (Claude Code only: the fetched payload also carries optional lifecycle-hook nudges + the
  read-verb auto-approve allowlist — merge into the project `.claude/settings.json` for active
  recall / capture reminders.)
