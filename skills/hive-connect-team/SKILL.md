---
name: hive-connect-team
description: "Connect agents and teammates to a running Hivemind server: pick the transport (local loopback / ngrok tunnel / SSH), mint and hand off per-seat tokens, register the MCP client, and revoke seats. Use when asked to connect / add / provision an agent or teammate, expose the server to remote machines, share the hive, or offboard a seat."
---

# hive-connect-team — connect agents & teammates

Register clients against a server that is already up (**hive-bringup**). Full reference:
`HIVE-ADMIN.md` §2 & §3.

**CLI resolve (once per shell):** `command -v hive >/dev/null 2>&1 || hive() { python3 -m hive.tools.cli "$@"; }`
— makes every `hive …` line below run on an uninstalled checkout (the CLI is stdlib-only;
Windows shells: `py -m hive.tools.cli <verb>`; prerequisites: **hive-bringup**).

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

   This line bakes the URL and the seat token into the teammate's `~/.claude.json`. If they also
   install the **hive-connect-harness** plugin, that registration **wins** over the plugin's own
   endpoint declaration — so `HIVE_MCP_URL` / `HIVE_TOKEN` in their profile become inert, and a
   rotated seat has to be applied by re-running this line rather than by editing the environment.
   Pick one route per teammate; that skill's *Two routes to the endpoint* section has the tradeoff.

## Remote teammate — SSH (no extra accounts)

```bash
ssh -NL 8765:localhost:8765 you@host    # forward the loopback door over SSH
```
Then the teammate uses the **local** loopback line above as-is — no ngrok, no token.

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
  remote-reachable one. **Never publish `0.0.0.0:8765`** — that door is tokenless, so publishing
  it hands unauthenticated recall and write to the whole LAN.
  What supplies that protection is the compose port map's `127.0.0.1:` prefix — inside the
  container the door binds all interfaces, because a published port could not reach it otherwise.
  So if you ever run the daemon without this compose file, that guarantee is yours to re-supply:
  `HIVE-ADMIN.md` §3.
- **The usage contract is served, never installed.** The MCP registration above is the only step
  **required** to connect — there is no handshake call, and nothing has to be installed on the
  workstation or in any repo for an agent to receive the contract. The whole contract (recall-first, scoped store/recall, write-vs-capture, outcome
  evidence, machine-gated retirement) reaches every agent automatically at connect via the MCP
  `initialize` result's `instructions` field, fresh every session; a reconnect picks up a changed
  contract. Census change evidence needs **no per-device setup** either: it is computed
  server-side by the sync daemon off the repo registry (`hive repo add` — **hive-connect-repo**),
  so there are no git hooks and no signing key on workstations.
  What an operator *may* additionally install is the optional client-side harness, which makes that
  served discipline mechanical rather than advisory — **hive-connect-harness**. It carries no
  contract text of its own, so this invariant survives it intact.
