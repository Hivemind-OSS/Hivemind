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
   export HIVE_TOKEN=hive_…
   claude mcp add --transport http hive https://<your-domain>/mcp \
     --header "Authorization: Bearer ${HIVE_TOKEN}"
   ```
   TLS terminates at the ngrok edge, so the token is encrypted in transit.

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
  remote-reachable one. **Never publish `0.0.0.0:8765`** — a bearer token over plain LAN HTTP is
  cleartext.
- **Onboarding is served-only — install nothing.** The full usage contract (recall-first,
  capture-by-default, the capture taxonomy, the identity model) reaches every agent over MCP at
  connect via the `initialize` instructions. There is no rules-file block to write and no handshake
  call; the only client-side step is the MCP registration above. (Claude Code only: optional
  lifecycle-hook nudges are listed in the `hive_health` tool description — merge into
  `.claude/settings.json` for active recall / capture reminders.)
