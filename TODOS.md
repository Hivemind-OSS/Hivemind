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

---

## TODO 12 — Polish `docs/PLANS/MATRIX-SUBGRAPH-FINGERPRINT-PLAN.md` before implementation

**File:** `docs/PLANS/MATRIX-SUBGRAPH-FINGERPRINT-PLAN.md`; eventually
`../hive-edge/hive_edge/cli.py`, `../hive-edge/hive_edge/hooks.py`, `hive/app/onboard_ref.py`

The plan (shape-only v1 blast-radius fingerprint: `matrix.blast_radius` member set + `combdrift`'s
existing per-symbol shape token, flat `sha256(sorted(tokens))`, landing as a fourth shared core in
`hive_edge/cli.py` beside `_mint_core`/`_verify_core`/`_wd_state`) is design-reviewed and drafted but
carries open items that should be resolved with evidence before chunk 1 starts, not discovered
mid-implementation:

1. **Depth-bound constant** (`_UNBOUNDED_DEPTH = 10_000`) is an unmeasured placeholder. Needs a real
   read against actual repo fan-out / blast-radius-walk performance before locking in — or an
   explicit decision to make it configurable instead of a hardcoded constant.
2. **Chunk 3's cross-repo `CONTRACT_VERSION` bump** is named but not spec'd to the level BUG-023
   used (exact byte diff of the served text, keystone-regen mechanics, which fit/conservation tests
   need updating). Write that level of detail into the plan before chunk 3 is picked up.
3. **combdrift's actual per-language coverage** (which of matrix's 10 languages
   `combdrift.fingerprint_anchor` can resolve) was never empirically confirmed against combdrift's
   source — the identity-fallback path in `_member_token` was designed to be safe either way, but the
   true coverage should be read and recorded in the plan, not assumed.
4. **The three deliberately-out-of-scope follow-ons** (wiring the composite fp into `_verify_core`'s
   verdict; replacing census's fuzzy `match_anchors` with an exact fp compare; body-content-hash v2)
   are named but not designed — each deserves its own `/software-design-review` pass before becoming
   its own plan.

**Verification:** the plan is polished when each item above is either resolved with cited evidence
(a file:line reading, a measurement, or a completed sub-review) and folded into the plan document,
or explicitly re-deferred with a named reason — not when it merely reads as finished.

---

## TODO 13 — Formally fix BUG-018, not the workaround

**File:** `hive/domain/secret_scan.py`, `tests/domain/test_secret_scan.py`

BUG-018 (`CONTEXT/BUGS.md`, UNSOLVED — the entropy catch-all false-positives on sufficiently
distinct path/module/symbol strings in capture/write body text) is currently only mitigated by a
documented workaround (don't name the path as one contiguous token). That shifts the burden onto
every writer and will keep firing on any sufficiently distinct name. A formal fix to the scan
itself is owed — not specified here, not another workaround.

**2026-07-12 evidence — the workaround is insufficient:** a legitimate `hive_capture` of the
combdrift `langs/` extension convention was refused twice by the entropy arm. It kept firing *after*
the contiguous path token was removed — an ordinary camelCase type name (`LangSpec`) and a
comma-separated run of short method names (`find_symbols`, `extract_interface`, `indirect_detail`)
each still tripped it; only fully plain prose with no code identifiers passed. So the false-positive
surface is wider than "one contiguous path token": it includes ordinary camelCase type names and
short identifier lists, and the documented "reword it" workaround does not reliably avoid the refusal
— every agent capturing a code-anchored lesson pays this tax. A principled fix (e.g. an
identifier-shape allowlist, or named-prefix detection augmenting the raw entropy catch-all so a
real prefixed secret still refuses while identifier-shaped tokens pass — never lowering the floor) is owed.

**Verification:** closed only when both of BUG-018's own named examples pass through `scan()`
unmodified in body text (no rewording needed) AND the `LangSpec` / method-name-list body above passes
unmodified AND the entropy arm's true positives (real prefix-less high-entropy secrets) still refuse.
