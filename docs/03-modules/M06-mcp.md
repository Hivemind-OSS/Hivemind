# M06 MCP surface  (mcp)

**One-line:** The single MCP/JSON-RPC trust boundary: a thin, schema-enforced translation layer that maps eight hive_* tool calls onto the domain ports, frames recalled memories as neutral reference context, and enforces approved-only recall as a structural belt-and-suspenders alongside the store query.
**Port disposition:** PORT+EXTEND of serving/mcp_server.py + serving/mcp_tools.py (§10 row "MCP surface (C7)"). KEEP the JSON-RPC-2.0-over-stdio loop, the MCPRequest/MCPResponse/_err envelope helpers, the dispatch-table pattern (self._tool_handlers), the tools/list = TOOL_DEFINITIONS static-schema split, and the {content:[{type:text,text:json}],isError} tool-result framing. EXTEND with three admission tools (hive_pending / hive_approve / hive_reject) and hive_init. DROP four tools verbatim: hive_consolidate, hive_schemas, hive_recall_cold, hive_restore_cold (their §10 "drop" rows: consolidation/GMM, cold tier). DROP hive_reconsolidate (reconsolidate is internal to C9, never agent-facing in v-min — the loop credits from git outcomes, not agent self-report; spec invariant "verifiable-credit-only"). DROP hive_audit (audit table is fleet/compliance tier, §10 dropped). DROP hive_outcome (the L0 self-reported signal is explicitly being replaced by the C10 producer; keeping it would reintroduce the gameable path the spec deletes — §8.1 L0). Net tool count: 8 (hive_write, hive_recall, hive_fetch, hive_pending, hive_approve, hive_reject, hive_init, hive_health), down from 11. Reference files: serving/mcp_server.py (dispatch + stdio loop + envelopes), serving/mcp_tools.py (TOOL_DEFINITIONS static schemas), types.py:459 HealthSnapshot TypedDict (extended additively with linked/link fields per the onboarding DECISION).

---

# M06 — MCP Integration Surface (C7)

> The `hive_*` tool contracts with enforced JSON schemas, the server-as-trust-boundary
> enforcement of approved-only recall + neutral reference framing, and the `hive-init`
> handshake. PORT+EXTEND of `serving/mcp_server.py` + `serving/mcp_tools.py`.

---

## 1. Responsibility (one deep module behind a narrow surface)

M06 is the **single trust boundary** of Hivemind v-min: the one component identical
under every MCP-speaking harness (spec §9 "the server is the trust boundary"). Its
narrow surface is **eight JSON-RPC verbs**; the rich, hidden work behind them is:

1. **Protocol mechanics** — JSON-RPC 2.0 over stdio: `initialize` / `tools/list` /
   `tools/call` / `ping`, newline-delimited framing, the `{content:[{type:text,
   text:json}], isError}` tool-result envelope, and the `_err(code,msg)` JSON-RPC
   error object. (PORT verbatim from `mcp_server.py`.)
2. **Schema enforcement** — every `tools/call` is validated against
   `TOOL_DEFINITIONS` (`required[]`, types, enums) **before** any domain port is
   touched; a malformed call never reaches the store.
3. **The two structural product guarantees, enforced HERE** (belt) in addition to
   the store query (suspenders):
   - **Approved-only recall** — M06 assembles recall candidates from the
     approved set only; even if a future ranker stage forgot the filter, the
     boundary re-filters. (§1 "the server enforces approved-only recall".)
   - **Neutral reference framing** — recalled text is returned under the key
     `reference_context`, never `instructions`/`command`, structurally distinct
     from the agent's instruction stream (§9 "Recall framing").
4. **Secret-floor invocation on write** — `hive_write` runs the deterministic
   M05 scan and stages **pending**; a raw secret is refused/redacted **before**
   staging. M06 owns the *wiring* of that floor into the tool, never the scanner
   itself.
5. **The admission relay** — `hive_pending`/`hive_approve`/`hive_reject` are the
   *only* `pending→approved` path; `hive_write` returns the pending `id` so the
   agent can surface "save these N insights?" in native chat (§8.2).
6. **The `trace_id` join seam** — every `hive_recall` envelope carries a
   `trace_id` (on hits **and** on abstain) — the §11 commit↔trace join key the
   C10 producer reads. Dropping it on abstain would silently kill move #6.

The depth is real: callers see `recall(query) -> hits|EMPTY`, but behind it sit the
encode chain, the gate, the approved filter, the framing, and the trace ledger —
none of which leak into the surface.

---

## 2. Public surface + ENFORCED contract

One process == one `(tenant_id, agent_id)` identity (`tenant_id` is a constant
label, never a query filter — §1 single-tenant). Eight tools.

### `hive_write(text, source?, tags?, proposed_by?) -> WriteResult`
- **Schema:** `required:["text"]`; `source?:str`, `tags?:str[]`, `proposed_by?:str`.
- **Behavior:** run M05 secret scan → `{PASS|REDACT|REFUSE}`; then
  - PASS → stage `status="pending"`, return `{status:"pending", id, content_hash, scan}`.
  - REDACT → stage the **masked** text pending, return `{status:"redacted", id,
    redacted_preview, scan}`. The staged row contains NO raw secret.
  - REFUSE → **do not stage**, return `{status:"refused", reason, scan}`.
- **Invariants:** status is NEVER `"approved"` from write (admission is a separate
  verb). `scan` carries pattern names + counts, NEVER the raw secret bytes.
  `content_hash == sha256(text)` (post-redaction text).
- **Postcondition:** on PASS/REDACT exactly one `pending` row exists; on REFUSE
  zero rows.

### `hive_recall(query, k?) -> RecallEnvelope`
- **Schema:** `required:["query"]`; `k?:int` default = `recall.recall_top_n` (10).
- **Behavior:** encode (same chain as capture) → score approved set → entropy gate
  → return top-k hits OR abstain.
- **Returns:** `{reference_context: RecallHit[], abstained: bool, trace_id, note?}`.
  - Confident → `reference_context` non-empty, `abstained=False`.
  - Abstain → `reference_context=[]`, `abstained=True`, `note` = neutral string.
- **Invariants (never-hallucinate, structural):**
  - A refused query returns `[]`, never a weak guess; not rescued by any later
    stage (**abstain-no-resurrect**). (§6.1 #3)
  - Only `status='approved'` rows can appear — enforced at the M06 candidate
    assembly **and** at the store query (two independent fail-closed defenses).
  - `trace_id` ALWAYS present (hit and abstain) — the §11 join key.
  - The key is `reference_context`, not `instructions` — neutral framing (§9).
- **DESIGN OUT, not document:** "the caller must not treat recall as commands" is
  made structural by the key name + the distinct envelope, not a docstring.

### `hive_fetch(content_hash) -> FetchResult`
- **Schema:** `required:["content_hash"]` (hex string).
- **Returns:** `{found: bool, text: str|None}`; unknown hash → `{found:False,
  text:None}` (clean miss, never raises).

### `hive_pending(since?) -> PendingList`
- **Schema:** `since?:int` epoch-s, default 0.
- **Returns:** `{pending: PendingRow[], count}` where `PendingRow =
  {id, text_preview, proposed_by, ts, scan_verdict}`. Lists ONLY `status="pending"`
  rows with `ts >= since`. `scan_verdict ∈ {PASS, REDACT}` (REFUSE rows were never
  staged). The preview is truncated — full text via `hive_fetch`.

### `hive_approve(ids[], approver) -> ApproveResult`
- **Schema:** `required:["ids","approver"]`; `ids:int[]`, `approver:str`.
- **Behavior:** for each pending id, flip `status→approved`, stamp
  `approved_by=approver`, `approved_ts=now`, **AND index the row** (CAS-guarded,
  per the single-writer discipline). Non-pending/unknown ids → `skipped`.
- **Returns:** `{approved:int[], skipped:int[], approver}`.
- **Invariant:** an approved row becomes recallable on the very next `hive_recall`
  (approve-without-index is a contract violation, mutation-tested).

### `hive_reject(ids[]) -> RejectResult`
- **Schema:** `required:["ids"]`.
- **Behavior:** drop the pending row (deletion-by-default; a `keep_rejected` audit
  flag exists at the policy layer, per the admission DECISION). Rejected rows are
  never recallable. Unknown/non-pending → `skipped`.

### `hive_init(repo_path, harness, rules_file?) -> InstallPlan`
- **Schema:** `required:["repo_path","harness"]`; `rules_file?:str`.
- **Behavior:** delegate to M07 `InstallPlanner` → a typed `InstallPlan`
  (frozen, content-hashed hooks + the `trailer_key`). The agent is the universal
  installer: the plan TEACHES the `Hive-Trace` commit-trailer convention so
  move #6 works the moment a producer is configured.
- **Invariants:** `InstallPlan.trailer_key == producer.stamp_trailer` (single
  source — prevents the §11 CONFIG_DRIFT silent-join-failure). M06 itself writes
  NO producer config (the onboarding DECISION's load-bearing cut: `hive_init`
  sits above the three ports and does not enroll the producer's watch set). An
  unsupported harness still yields the everywhere-baseline (git post-commit +
  agent-initiated `hive_write`).

### `hive_health() -> HealthSnapshot`
- **Schema:** `{}`. Cheap, safe to poll. Returns the `total=False` snapshot:
  `{ok, tenant_id, db_path, db_size_bytes, n_episodes, n_pending, embedder,
  embedder_loaded, embedder_projection, W_version, d, uptime_s, linked,
  trailer_key}`. On a store/embedder error → fail-closed subset `{ok:False,
  error, db_path}` ONLY. `embedder_loaded` gates the container HEALTHCHECK so
  "healthy" is not declared before the model is resident (onboarding DECISION).
- **Invariant:** snapshot contains no secret-shaped substrings.

### Error semantics (PORT)
- Protocol errors (unknown method, parse error) → JSON-RPC `error` object
  (`-32601` method, `-32602` unknown tool / bad params, `-32700` parse).
- Tool failures → NOT a JSON-RPC error; `result.isError=True` with a
  `content[0].text` string. The stdio loop never crashes on a tool exception
  (logged with context; stack never returned to the agent).

---

## 3. Swap seam

M06 is a **driving adapter** (per the hexagonal DECISION), not itself a swap port.
It depends on the three mandated ports + two helper ports but owns no adapter
selection. It DOES enforce one non-swappable boundary deliberately: the
`status='approved'` recall predicate is a **hard literal**, not a config key — an
allowlist so any future status is non-recallable by default (the fail-safe
direction; admission DECISION). The transport itself is swappable orthogonally:
the stdio JSON-RPC loop can be replaced by the official `mcp` SDK with no change to
the tool handlers (the reference already notes this; the `_tool_handlers` table is
the stable seam). Proof the swap needs no core change: handlers take `dict args ->
dict result`; the SDK substitution only rewrites `run_stdio` + `to_json`.

---

## 4. Data owned

**None.** M06 owns no SQLite tables and no blobs. It READS config keys:
`recall.recall_top_n` (default `k`), `observability.log_level`/`log_file`
(structured JSON logging at the boundary), and the process identity
(`tenant_id`, `agent_id`). It owns one in-memory static value: `TOOL_DEFINITIONS`
(the tools/list schema table) — a pure module constant, no runtime state. It owns
the additive extension of the `HealthSnapshot` TypedDict (`n_pending`,
`embedder_loaded`, `linked`, `trailer_key`).

---

## 5. Dependencies (and the boundaries it must NOT cross)

| Depends on | For | Boundary M06 must NOT know about |
|---|---|---|
| M03 RecallService (port) | `recall(query,k)->hits|EMPTY`, `fetch(hash)` | the embedder/index internals; the encode chain |
| M02 EpisodeStore (port) | stage pending, list pending, approve+index, reject | row codec, vector index backend, CAS internals |
| M04 ApprovalPolicy (port) | the pending→approved state machine + the secret-scan gate | the secret pattern set (lives in M05) |
| M05 SecretScanner (via M04/M03 write path) | the PASS/REDACT/REFUSE verdict | nothing — M06 only forwards the verdict, never the raw secret |
| M07 InstallPlanner (port) | the typed `InstallPlan` for `hive_init` | hook file contents / harness specifics |

M06 **must NOT** know: the embedder transport, the vector index backend, the
producer's watch config (the onboarding DECISION's cut — `hive_init` never mutates
it), the utility posterior, or any consolidation/schema machinery (dropped). It
must NOT reach past a port into a concrete adapter (enforced by the AST
import-linter folded in from the architecture DECISION).

---

## 6. Failure-mode logging (structured, secrets never logged)

| Boundary | Level | Context (no secrets) | Recovery |
|---|---|---|---|
| Parse error on a stdin line | `warn` | offending line length, `json` error | reply `-32700`, continue loop |
| Unknown tool name | `warn` | tool name, agent_id | reply `isError`, continue |
| Schema validation fail (missing required / bad enum) | `info` | tool name, missing field | reply `isError` |
| Tool handler raises | `error` | tool name, agent_id, exception type + message + stack | reply `isError`, loop survives |
| `hive_write` REFUSE (secret) | `warn` | pattern NAME(s) + count, `proposed_by`, NEVER the secret | refuse, 0 rows |
| `hive_write` REDACT | `info` | pattern names + count, pending id | stage masked |
| `hive_recall` abstain | `info` | trace_id, normalized entropy, N_eff | return `[]` |
| Store/embedder error in `health` | `error` | exception type, db_path | fail-closed `{ok:False}` |
| `hive_approve` index failure (CAS) | `error` | id, version conflict | skip id, report in `skipped` |
| `hive_init` trailer_key drift (assert) | `error` | configured vs producer key | fail fast (CONFIG_DRIFT guard) |

All logs JSON-structured (`timestamp, level, context, message, error, stack`) with
the `trace_id`/`request_id` on recall/write flows. **stderr stays clean** so
stdout-bound JSON-RPC is unpolluted (PORT the reference's stderr discipline).
Secret bytes are never written to the store OR the logs (§6.4).

---

## 7. Port disposition vs §10 map

**PORT+EXTEND** of `serving/mcp_server.py` + `serving/mcp_tools.py` (§10 "MCP
surface (C7)" row). KEEP: stdio JSON-RPC loop, `MCPRequest/MCPResponse/_err`
envelopes, dispatch-table pattern, `tools/list = TOOL_DEFINITIONS` split,
`{content,isError}` framing, stderr-clean logging. ADD: `hive_pending`,
`hive_approve`, `hive_reject`, `hive_init`. DROP (per §10 drop column): `hive_
consolidate`, `hive_schemas`, `hive_recall_cold`, `hive_restore_cold`
(consolidation/cold tier dropped); `hive_reconsolidate` + `hive_outcome` (the L0
self-reported credit path is replaced by the C10 verifiable producer — §8.1; keeping
them reintroduces the gameable signal the spec deletes); `hive_audit` (fleet/
compliance tier). Net **11 → 8** tools. `HealthSnapshot` (types.py:459) PORT+EXTEND
additively (`n_pending`, `embedder_loaded`, `linked`, `trailer_key`).

---

## 8. TEST CONTRACT (test-first; full functional coverage)

See `test_contract` for the compact list. Mapping to gates/invariants:

- **§6.1 #3 never-hallucinate** → `test_mcp_recall_boundary.py::test_recall_abstain_
  returns_empty_list_with_trace_id` + `::test_recall_abstain_no_resurrect`.
- **§6.1 #5a secret-refused-pre-stage** → `test_mcp_write_scan_stage.py::test_write_
  planted_secret_refused_before_stage` (asserts `store.stage` called 0 times).
- **§6.1 #5b pending-never-recallable** → `test_mcp_recall_boundary.py::test_recall_
  filters_to_approved_only`.
- **§6.1 #5c reference-framing** → `test_mcp_recall_boundary.py::test_recall_framed_
  as_reference_context_not_instructions`.
- **§11 join-key survival** → `test_recall_trace_id_present_on_hit_and_abstain` +
  `test_mcp_init.py::test_init_trailer_key_sourced_from_producer` (CONFIG_DRIFT).

**MUTATION TESTS (RULE 2):**
1. `test_mutation_recall_predicate_drop` — DELETE the `status=='approved'` filter at
   the M06 candidate-assembly site → `test_recall_filters_to_approved_only` goes RED
   → restore → GREEN. Proves the boundary enforcement is load-bearing **independent
   of** the store-query filter (belt-and-suspenders both have teeth).
2. `test_mutation_approve_skips_indexing` — REMOVE the `index.add` inside
   `hive_approve` → `test_approve_flips_status_and_indexes` goes RED (the row flips
   approved but never becomes recallable) → restore → GREEN. Proves indexing is part
   of the approve contract, not incidental.

A reviewer can assert: happy path (write→pending→approve→recall→fetch), every §6
failure mode, every §2 invariant, and the four §6.1 acceptance sub-gates this module
owns are each covered by a named test, with two mutation tests pinning the two
load-bearing enforcement points. No functional path is untested.

---

## Design review (independent pass)

**Verdict:** STRONG DESIGN, TEST CONTRACT NOT BUILD-READY. M06 is a genuinely deep module behind a narrow surface — eight JSON-RPC verbs hiding the encode chain, gate, approved-filter, framing, secret-floor wiring, admission relay, and trace ledger, none of which leak into the surface (APOSD #4, #6). The hexagonal driving-adapter framing is correct: M06 owns no data, no adapter selection, and reaches the store/recall/policy/install ONLY through ports (clean §5 dependency table, low leakage). The two structural product guarantees are designed OUT not documented — `reference_context` key name + distinct envelope make 'recall is not commands' a structural fact, and the approved-only predicate is a hard literal allowlist (fail-safe direction). This is exemplary agent-native contract-that-cannot-lie work. The DESIGN scores 1-3 across complexity/leakage. WHAT BLOCKS SIGN-OFF IS THE TEST CONTRACT, NOT THE DESIGN. The contract is referenced as an external artifact ('See test_contract for the compact list') but only ~10 tests are named inline; the prose claim 'no functional path is untested' is not yet backed by named tests for the majority of the eight verbs. Schema-enforcement (the explicit §1.2 'malformed call never reaches the store' guarantee) has NO named test and NO mutation test despite being a load-bearing belt. hive_pending/hive_reject/hive_fetch/hive_health have zero named happy-path-or-failure tests. The secret-floor REDACT path (staged row contains NO raw secret + content_hash over post-redaction text) is asserted as an invariant but only the REFUSE path is tested. Fix the test gaps below and this is build-ready; the design itself needs only minor must-fix clarifications.

**Scores (1–10):**
- design_complexity: 3
- cognitive_load: 4
- information_leakage: 3
- extensibility_fit: 8
- agent_navigability: 8
- contract_enforcement: 7
- test_coverage: 5

**Red flags:**
- Prose-Only Contract @ §8 'See test_contract for the compact list' — the Test Contract (the user's hard first-class mandate) is referenced as an external artifact but only ~10 tests are named inline while the claim is 'no functional path is untested'; the coverage assertion is prose, not an enumerable file::test list — root: obscurity → silent drift, agent cannot verify the claim or self-check an edit (Missing Feedback Signal on the untested verbs).
- Special-General Mixture / contract conflict @ §6 logging table row 'hive_approve index failure (CAS) → skip id' vs §2 invariant 'approve-without-index is a contract violation' — the failure-mode table sanctions exactly the approved-but-unindexed state the §2 contract forbids and the mutation test pins; the atomicity of status-flip+index is unspecified — root: dependency (two specs of the same state machine disagree) → a real bug-shaped gap where a CAS-failed approve could leave a permanently-unrecallable approved row.
- Information Leakage (mild) @ §2 hive_recall + §3 swap seam — JSON-RPC error codes -32601/-32602/-32700 and the {content,isError} envelope are described as handler-layer concerns, but §3 claims the transport (which owns wire-level error encoding) swaps with 'no change to the tool handlers'; the boundary between 'handler owns isError tool-result' and 'transport owns JSON-RPC error object' is stated but the swap-invariance is asserted not enforced — root: dependency → the SDK-swap 'no core change' claim is unproven and could amplify on the transport swap.
- Hard-to-Describe (latent) @ §2 hive_write — three-branch verdict (PASS/REDACT/REFUSE) each with distinct status string, distinct return shape, distinct postcondition (1 row / 1 masked row / 0 rows) and a content_hash-over-post-redaction-text rule, but the Test Contract names a test for only ONE branch (REFUSE) — root: obscurity → the multi-clause contract is the kind that needs enforcement-by-test precisely because it is hard to hold in the head; one untested branch (REDACT) is the dangerous one because it persists.
- Vague provenance of the swap-seam proof @ §3 'handlers take dict args -> dict result; the SDK substitution only rewrites run_stdio + to_json' — this is a correct and good claim, but it is asserted with no named test pinning the `_tool_handlers` table as the stable seam; an agent porting to the SDK has a prose promise, not a Missing-Feedback-Signal-free contract — root: obscurity → the one swappability claim in a swappability-mandated system is the only port in M06 without a contract test.

**Test gaps:**
- Schema enforcement (§1.2 'malformed call never reaches the store') — NO named test and NO mutation test; this is a load-bearing belt with zero coverage.
- hive_write REDACT branch — only PASS and REFUSE are testable from the named contract; the REDACT invariants (masked text staged, NO raw secret bytes in the row, content_hash over post-redaction text, status=='redacted') are untested. REDACT is the persisting path, so this is the highest-risk gap.
- hive_pending — no named test for the ts>=since filter, the status=='pending'-only filter, the preview truncation, or the exclusion of REFUSE rows (which were never staged).
- hive_reject — no named test for deletion-by-default, the keep_rejected audit flag, unknown/non-pending → skipped, or 'rejected rows are never recallable'.
- hive_fetch — no named test for the clean-miss contract (unknown hash → {found:False, text:None}, never raises) — the explicit 'define errors out of existence' guarantee.
- hive_health — no named test for the fail-closed `{ok:False, error, db_path}`-ONLY subset on store/embedder error, the embedder_loaded HEALTHCHECK gate, or the §2 'snapshot contains no secret-shaped substrings' invariant.
- request_id→trace_id key rename — the port renames the reference's `request_id` to `trace_id` in the RecallEnvelope; no test pins this mapping, so a silent reversion to the old key would kill the §11 join while passing every existing test.
- Tool-handler-raises path (§6 'loop survives, stack never returned to agent') — the stderr-clean / loop-survival / stack-not-leaked contract has no named test; only the abstain and refuse paths are covered.
- hive_init unsupported-harness path — the contract guarantees 'an unsupported harness still yields the everywhere-baseline (git post-commit + agent hive_write)' but only the trailer_key-sourced-from-producer test is named; the baseline-fallback branch is untested.
- SDK-swap invariance (§3) — no test asserts the `_tool_handlers` dict-args→dict-result table is transport-agnostic, leaving the one swappability claim in M06 unenforced.
- approve→recall happy-path round-trip is named (test_approve_flips_status_and_indexes) but the CAS-failure-during-approve branch (status must NOT flip if index fails) is untested — the atomicity gap from must_fix #5.

**Must-fix:**
- TEST: Add a named test for the §1.2 schema-enforcement guarantee — `test_mcp_schema_enforcement.py::test_malformed_call_rejected_before_port_touched` asserting that a tools/call missing a required field (e.g. hive_write with no `text`, hive_approve with no `approver`) returns isError WITHOUT any store/policy port method being invoked (mock the ports, assert call_count==0). This is the explicit 'a malformed call never reaches the store' contract and currently has NO test. It also needs a mutation test (RULE 2): delete the pre-dispatch validation step → the test must go RED.
- TEST: Add the REDACT-path coverage that the §2 hive_write invariant demands — `test_mcp_write_scan_stage.py::test_write_redact_stages_masked_text_no_raw_secret` asserting (a) exactly one pending row exists, (b) the staged text contains NONE of the raw secret bytes, (c) `content_hash == sha256(post-redaction text)`, (d) status=='redacted' not 'approved'. Only REFUSE is currently tested; REDACT is the more dangerous path because it persists.
- TEST: Add happy-path + failure tests for the four currently-untested verbs: hive_pending (lists ONLY status=='pending' with ts>=since; preview truncated; REFUSE rows absent), hive_reject (drops pending row, unknown/non-pending → skipped, rejected row never recallable), hive_fetch (unknown hash → {found:False, text:None} never raises — the 'define errors out of existence' contract), hive_health (fail-closed `{ok:False, error, db_path}` ONLY subset on store/embedder error + the no-secret-substring invariant). The contract claims full functional coverage but names none of these.
- TEST: Pin the request_id→trace_id rename with a test. The reference returns the join key as `request_id`; M06 renames it to `trace_id` in the RecallEnvelope. Add `test_recall_envelope_uses_trace_id_key_mapped_from_request_id` so the §11 join key cannot silently revert to the old name during the port — a rename bug here silently kills move #6 and would pass every other test.
- DESIGN/TEST: The §6 failure-mode table lists 'hive_approve index failure (CAS) → skip id, report in skipped' but the §2 hive_approve contract says 'an approved row becomes recallable on the very next hive_recall (approve-without-index is a contract violation)'. These conflict: if indexing fails after status flips, the row is approved-but-unrecallable — the exact violation the mutation test test_mutation_approve_skips_indexing exists to catch. Specify the atomicity: is status-flip + index one CAS transaction (so a CAS failure leaves status pending and the id lands in `skipped`), or can they diverge? Add `test_approve_index_failure_leaves_row_not_approved` to nail it. As written the contract permits the silent-unrecallable state it claims to forbid.
- DESIGN: Resolve the §3 transport-swap-seam claim against the surface. §3 says the stdio loop 'can be replaced by the official mcp SDK with no change to the tool handlers... the SDK substitution only rewrites run_stdio + to_json'. But the error-semantics section couples JSON-RPC error CODES (-32601/-32602/-32700) into the handler/dispatch layer, and the SDK owns its own error encoding. State explicitly which layer owns JSON-RPC error mapping so the 'no core change' swap claim is true, or the seam leaks. Add a test that the `_tool_handlers` table (dict args -> dict result) is SDK-agnostic.
- TEST: Make the 'test_contract' artifact real and complete, or inline the full list into §8. The design references it as authoritative ('See test_contract for the compact list... No functional path is untested') but it is not present in the spec under review — a prose-only claim of coverage with no enumerable artifact. Per the user mandate (tests are a first-class contract, written BEFORE implementation), the full file::test-name + exact-assertion + failure-caught list for all eight verbs must exist for build-ready sign-off.
