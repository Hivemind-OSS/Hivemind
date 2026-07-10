# TODOs

Deferred work — not yet built — that still fills a real gap in the project. Each entry names the files it touches and the mutations that would prove it.

---

## TODO 7 — Qwen3 asymmetric query-instruction (deferred from the embedding swap)

**File:** `hive/adapters/embedding/local_st.py`, `hive/domain/ports.py`, `hive/domain/recall.py`, `hive/domain/admission.py`, `tests/fakes/_fakes.py`

Once the embedding model is `Qwen/Qwen3-Embedding-0.6B`, recall quality can be lifted by using the
model's intended **asymmetric** encoding: a document is embedded as raw text; a query is embedded
with an instruction prefix (`"Instruct: <task>\nQuery: <text>"`). The shipped `EmbeddingProvider.encode`
is a single symmetric chain used by both capture and recall, so this is deferred — it is NOT a free
swap:

- It **widens the port** (a query path distinct from the document path) and must update every adapter
  + the fake + both call sites (`recall.py` query side, `admission.py` document side), each conformance-
  tested (a port change that goes green on fakes alone is the smell).
- It **breaks "same text → same vector"**, which the trust lifecycle depends on: the recall miss-query
  vector is matched against candidate/servable *document* vectors by cosine (`demand_tau` = miss↔candidate,
  `competitor_tau` = candidate↔servable). Enabling asymmetry therefore requires **recalibrating
  `autonomy.demand_tau`/`competitor_tau`** (and re-checking the recall gate) on the asymmetric geometry —
  not just ranking.
- Land it OFF by default and byte-inert when off (a fixed/empty prompt ⇒ identical to symmetric); flip
  on only after the τ recalibration is measured.

**Mutations to verify:** with asymmetry ON, the same text as query vs document must differ; with it OFF
(default), query and document encodings must be byte-identical (the byte-inert guarantee); the
recalibrated `demand_tau` must still promote a genuine cross-identity demand and still withhold a
self-demanded one.

---

## TODO 8 — Non-Claude-Code permission auto-approve recipes (per-IDE allowlist)

**File:** `hive/app/onboard_ref.py` (`ONBOARDING_PROCEDURE`, `render_onboarding_payload`),
`tests/app/test_onboard_ref.py`, `tests/app/test_contract_fit.py`

Deferred from the contract-versioning work (resolved Claude-Code-first). The
contract-versioning feature serves an auto-approve step so the non-human-in-the-loop verbs
(`recall`/`capture`/`health`/`outcome`/`flag` — the schema-derived set whose `required[]` lacks
`approved_by`) don't prompt per call. But the *mechanism* is IDE-specific: v1 ships only Claude
Code's form (`.claude/settings.json` `permissions.allow`, `mcp__hive__*`). Other runtimes have
different (or no) permission-allowlist formats.

Extend `ONBOARDING_PROCEDURE` (served via `render_onboarding_payload()`'s `procedure` key) with a
per-runtime permission recipe **alongside** the existing per-runtime rules-file table (Cursor /
Windsurf / Cline / generic), each writing to the **project** scope (never global) and each keeping
the human-gated 3 (`write`/`supersede`/`prune`) prompted.

**Mutations / verification:** each IDE's permission schema is real-runtime-only (the BUG-007/011/012
class — an offline suite can't see whether the written allowlist actually suppresses that IDE's
prompt). Verify each recipe against the live IDE before serving it; a runtime with no allowlist
mechanism gets the rules block but keeps prompting (degrade-safe).

---

## TODO 9 — Full AGI-mode client-side contract + switching mechanism

**File:** `hive/app/onboard_ref.py`, `hive/app/mcp_server.py` (beacon), a new claude-code `SessionStart`
hook, `tests/app/test_onboard_ref.py`

Deferred from the contract-versioning work (open question #4, AGI descoped). The **domain**
half of AGI mode already exists (AGI-MODE landed: `HIVE_AGI__MODE` makes the boundary honor
`approved_by="AGI_OVERRIDE"`). The missing half is the **client-side** prompt-bypass + the switching
that propagates an off↔on flip into the installed contract (rules + allowlist + possibly hooks).

The hard constraint (why it is its own feature): AGI_MODE is a *server-side vouch* switch; the
*client-side* permission prompt lives on the client, and **a remote server cannot — and must not —
reach a client's `permissions.allow`** (a remote flag silently switching off local human control is a
footgun). So propagation is **local convergence on a server signal**, never server→client reach, and
the actuator must be an **operator-enabled** hook (conscious client-side opt-in).

Mechanism sketch (build on the contract-versioning beacon substrate):
- Beacon carries `agi_mode` as a **second axis** orthogonal to `contract_version`; the installed
  marker records the `(version, posture)` pair, so an off→on flip (text unchanged) reads as drift.
- Re-onboard on posture drift rewrites **only** the project allowlist 5↔8 verbs (under AGI-on the
  privileged 3 self-authorize, so the prompt is the only thing left blocking autonomy) and restamps
  the marker. The rules-block text is unchanged (it already states the AGI_OVERRIDE conditional).
- Needs the **hook-enforced** enforcement variant (the teeth) — model-driven re-onboard is too weak
  for an autonomy promise. Likely depends on **TODO 8** (per-IDE permission recipes), since the
  bypass is also per-IDE.

**Honest limits to design around (carried from the plan's AGI discussion):**
- Behavioral unless hook-enforced; absent teeth it degrades to *more* prompting, never a silent
  unauthorized write (the domain gate is the real authority — Law 6).
- One-session latency: `permissions.allow` is read at session start, so a mid-session rewrite applies
  next session. Operator fast-path: write the 8-verb allowlist at config-flip time, with
  beacon/re-onboard as the convergence backstop.

**Mutations to verify:** with AGI on, the beacon's `agi_mode` flips and an `agi=off`-marked install
reads as drift → re-onboard; with AGI off, the allowlist contracts to 5 and the privileged 3
re-prompt + require a human `approved_by`; the server never writes a client permission file (the
actuator is always local).

---

## TODO 10 — Continuous integration (run the suite on every push / PR)

**Where:** a new `.github/workflows/ci.yml` (plus a CI status badge in `README.md`).

Deferred only because GitHub Actions is not available on the current private-org plan — add this
once the repo is public or Actions is enabled. Today the test suite runs only on a contributor's
machine, so a regression or a broken build can land unnoticed and a fresh clone has no signal that
the default branch is green. This is the last standard open-source convention still missing.

Add a workflow that, on `push` and `pull_request`:
1. Sets up Python 3.11 and 3.12 (matrix).
2. Installs the package: `pip install -e ".[dev]"`.
3. Runs the fast tier: `pytest -m "not embed"` — the default, no model download.
4. Builds and validates the wheel (`python -m build` + `twine check dist/*`) so packaging
   metadata (`license` / `license-files` / `readme`) cannot silently regress.
5. Runs the `embed` tier (`pip install -e ".[embed,dev]"` then `pytest -m embed`) on a **schedule
   or manual dispatch only** — it pulls the ~1.2 GB Qwen3 model, too heavy for the per-PR path.

Keep it provider-portable: the steps only shell `pip` / `pytest` / `build`, so the workflow maps
onto any runner if the project ever moves off GitHub Actions.

**Verification:** open a trivial PR and confirm the check runs; then push a deliberately failing
test and confirm the PR check goes red — CI is only real once a red is proven to block.

---

## TODO 11 — Deterministic auto-re-onboard on contract_version skew (hook-enforced)

**File:** a new claude-code `SessionStart` hook under `.claude/hooks/`, `hive/app/mcp_server.py` (the
`_tool_result` beacon that stamps `contract_version`), `hive/app/onboard_ref.py` (`ONBOARDING_REFERENCE`),
`tests/app/test_onboard_ref.py`

The contract is **served, not shipped-frozen in the client**: the server is the single source of truth,
and each client holds only a version-keyed cache (the rules block + `.claude/` hooks + allowlist) that is
a *projection of server state*, not independently shipped software. So a contract update propagates by the
operator updating **one** artifact — the server image (`pull` + `hive up`) — and every client re-deriving
its cache on next connect. Nobody edits a client by hand.

The last mile is not yet deterministic. Today re-onboard is an **instruction to the agent** (the
HIVEMIND-RULES block says "RE-ONBOARD when a hive_* result's `contract_version` differs"), so it fires only
if the agent notices the skew and complies. Close it: a served `SessionStart` hook reads the beacon's
`contract_version`, compares it to the installed marker, and on drift **silently re-fetches and reinstalls**
the block + hooks + allowlist — the same hook-enforced pattern the Stop/capture and UserPromptSubmit/recall
hooks already use to make the discipline deterministic instead of advisory. That flips propagation from
"self-heals if the agent cooperates" to "self-heals, period." Shares the beacon/marker substrate with
TODO 9's AGI-posture axis (the marker becomes the `(version, posture)` pair).

**Mutations to verify:** with the installed marker behind the server's `contract_version`, the hook
re-onboards exactly once and restamps the marker (a second session is a no-op — no reinstall loop); with
the marker current, the hook is byte-inert (no rewrite); a server the hook cannot reach degrades safe
(keeps the last-good cache, never wipes it). The server must NEVER write a client file directly — the
actuator is always local (the no-server→client-reach law carried from TODO 9).
