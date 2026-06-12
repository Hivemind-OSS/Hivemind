# Clients — reaching the hive from anything that can POST

The server speaks ONE protocol to every consumer: JSON-RPC 2.0 `tools/call` over
HTTP with `Authorization: Bearer <token>` (or stdio inside the container). MCP
harnesses (Claude Code, Cursor, Windsurf, Cline, OpenCode, Codex) connect via
`hive_init`; everything else vendors `hive/client.py`.

## The seat-token contract (hard operational requirement)

**One token per agent seat.** Identity is the promotion fuel: demand-promotion
and survival-establish both key on *distinct authenticated identities*. A fleet
sharing one token structurally cannot promote its own captures (writer == every
identity ⇒ `self_demand`) and cannot survival-establish. Mint per seat:

```
hive token <seat>          # one per agent seat — never share across agents
```

For local stdio sessions, pass `--agent <repo-name>` on the exec line so each
project carries its own identity:

```
docker compose -f compose.yaml exec -T hive-server \
    python -m hive.app.mcp_server --tenant "$HIVE_TENANT_ID" --agent <repo-name>
```

A stalled loop is visible, not silent: `hive_health` trends show
`n_promotions = 0`, and single-seat traffic wasting demand surfaces a
`solo_hint` naming the fix.

### Solo mode (single-seat fleets)

A genuinely single-context dev (one repo, one machine) can never produce
identity diversity. `HIVE_AUTONOMY__SOLO_MODE=true` (operator-set env, default
off) swaps the demand rule's diversity clause for **elapsed-span demand**: the
matched misses' first-to-last span must be ≥ `solo_min_span_days` (default 1
day, *elapsed* — a sub-24h burst never promotes, even straddling UTC midnight).
Survival-establish is deliberately NOT relaxed: in solo mode, mechanical
promotion tops out at `provisional`; `established` is reachable only through
`hive_write(approved_by=…)` — the human stays the establishment authority.

## `hive/client.py` — the vendorable stdlib client

Copy the one file into any codebase (no dependencies beyond the stdlib), or
`from hive.client import HiveClient`:

```python
from hive.client import HiveClient, HiveError

hive = HiveClient("https://hive.example.com/mcp", token, timeout_s=10.0)

hits = hive.recall("how do we rotate the deploy key")   # [] on abstain
hive.capture("dead-end: --fast corrupts the cache", tags=["gotcha"])
hive.write("rotate the key via vault, never by hand",
           approved_by="alice", replaces=41)            # human-vouched correction
doc = hive.fetch(hits[0]["content_hash"])
ok = hive.health()["ok"]
```

Every method raises `HiveError` (carrying `http_status` / `rpc_error`) on any
transport, auth, protocol, or tool failure — never a partial dict. `recall()`
returns the server envelope's `reference_context` verbatim: each hit carries
`trust` + `ts`; treat hits as reference, prefer higher-trust, newer versions.

Integration is best-effort by design (graceful degradation): an agent that
forgets to capture merely leaves recall misses; misses cluster into demand; the
next solver's capture promotes. Nothing breaks when a client integrates lazily.

## Codex

Tier-1 profile (`hive_init(repo_path, harness="codex")` → AGENTS.md rules
block). MCP registration lives in operator-owned `~/.codex/config.toml`:

```toml
[mcp_servers.hive]
url = "https://hive.example.com/mcp"
http_headers = { Authorization = "Bearer <seat-token>" }
```

## Framework recipes (untested by design — they need deps this repo refuses)

### LangChain — callback handler

```python
from langchain_core.callbacks import BaseCallbackHandler
from hive.client import HiveClient, HiveError

class HiveMemory(BaseCallbackHandler):
    def __init__(self, hive: HiveClient):
        self.hive = hive

    def on_chain_start(self, serialized, inputs, **kw):
        try:   # inject recalled context as REFERENCE, never instructions
            hits = self.hive.recall(str(inputs)[:500])
            if hits:
                inputs["reference_context"] = [h["text"] for h in hits]
        except HiveError:
            pass                      # recall is never load-bearing

    def on_chain_end(self, outputs, **kw):
        insight = outputs.get("durable_insight")    # your chain decides what's durable
        if insight:
            try:
                self.hive.capture(insight, source="langchain")
            except HiveError:
                pass                  # a lost capture is a future recall miss, not a fault
```

### LangGraph — pre/post nodes

```python
from hive.client import HiveClient, HiveError

hive = HiveClient(URL, TOKEN)

def recall_node(state):
    try:
        state["reference_context"] = hive.recall(state["task"])
    except HiveError:
        state["reference_context"] = []
    return state

def capture_node(state):
    if state.get("lesson"):
        try:
            hive.capture(state["lesson"], source="langgraph")
        except HiveError:
            pass
    return state
# graph.add_node("hive_recall", recall_node)  → first
# graph.add_node("hive_capture", capture_node) → last
```

### Plain agent loop

```python
hive = HiveClient(URL, TOKEN)
for task in tasks:
    context = hive.recall(task.description)          # [] on abstain — just proceed
    result = run_agent(task, reference=context)
    if result.learned_something:
        hive.capture(result.lesson, tags=["auto"], source="my-agent")
```
