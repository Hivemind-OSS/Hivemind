# M05 Admission path  (admit)

**One-line:** The one deep module that converts an agent's proposed insight into a recallable memory through a single irreversible-by-construction gauntlet: deterministic secret scan (refuse/redact before persistence) → content-hash-deduped pending staging → the sole pending→approved state machine that, and only that, indexes a row into the VectorIndex — so a pending or secret-bearing memory is structurally unrecallable, not merely policy-unrecallable.
**Port disposition:** BUILD-NEW for the two genuinely-absent pieces, PORT+FLIP/SIMPLIFY for the rest, per the §10 map:
- Secret scan (C6): BUILD-NEW. Verified ABSENT in the tree (grep for AKIA/sk-/ghp_/xox/PEM/entropy/redact returns zero non-test hits). ~0.2k: deterministic pattern set + Shannon-entropy high-token detector; refuse OR redact BEFORE staging. New file hive/domain/secret_scan.py (pure) behind a SecretScanner port (hive/ports/secret_scanner.py).
- Staging + status machine (C6): BUILD-NEW over the existing immediate write. Reference serving/service.py:1422 write_text (writes immediately, no status). The new AdmissionLedger replaces the immediate-write semantics; it REUSES three proven storage primitives verbatim rather than reinventing them: (1) blob_store.put (storage/blob_store.py:52) — content-hash dedup via INSERT…ON CONFLICT(content_hash) DO NOTHING, and sha256() at :19; (2) the update_cas rowcount idiom (storage/persistence.py:581, UPDATE…WHERE eid=? AND version=? → cur.rowcount>0) as the pending→approved compare-and-set — PORT the IDIOM, not a bespoke compare_and_set_status (per the resolved DECISION); (3) SqliteMeta UPSERT (persistence.py:1148) only if a cursor watermark is needed for hive_pending(since).
- MCP surface (C7): PORT+EXTEND serving/mcp_tools.py TOOL_DEFINITIONS + serving/mcp_server.py dispatch (_tool_write at :164). Flip hive_write from {episode_id} immediate to {pending_id, status:"pending", scan_verdict, render_block}; ADD hive_pending/hive_approve/hive_reject; the index-on-approve call replaces the inline write_text indexing (bm25/graph stripped — dropped per §10).
- Store/index seam: depends on the M03/M04 EpisodeStore + VectorIndex ports (Store-owns-and-drives-the-index synthesis); this module is a DRIVER of those ports, it does not own the episodes table DDL.
DROP from the reference: write_or_update_text dedup router (service.py:1478), subject_key/supersede/bm25/graph branches in write_text, the immediate-recallability of a freshly written row.

---

# M05 — Admission path: `hive_write` → secret-scan → staging → AdmissionLedger (C6)

> Source of truth: `HIVEMIND_VMIN_SPEC.md` §1 (capabilities), §2 C6/C7, §3 (episodes
> `status`/`proposed_by`/`approved_by`/`approved_ts`), §8.2 (native-chat approval trio),
> §9 (secret-scan floor + approved-only recall = the two hard substrate guarantees),
> §6.1 #5 (acceptance gate). Resolved DECISION (this module): B's information-hiding
> architecture (single `_RECALL_PREDICATE`, three-module knowledge partition, frozen
> self-asserting `ScanVerdict`) hardened by A's lower-risk mechanics (reuse the proven
> `update_cas` rowcount idiom; index-absence as the *primary* never-hallucinate teeth
> with the SELECT predicate as belt-and-suspenders; reject-as-deletion-by-default with a
> `keep_rejected` flag). Port reference: `cls_memory/` (verified 2026-06-04).

## 1. Responsibility (one deep module)

Admission is the single gauntlet every memory passes from "an agent proposed it" to
"it is recallable." It hides three dangerous, easy-to-get-wrong decisions behind one
narrow surface so no caller can do them piecemeal:

1. **Refuse/redact secrets before anything is persisted** — a deterministic credential
   scan runs *before* the blob is written, so the substrate never stores a raw secret
   (§9 "the way a DB rejects a malformed row"). REFUSE leaves zero rows and zero blobs.
2. **Stage, never publish** — a proposal lands `status='pending'`, content-hash-deduped,
   carrying `proposed_by`. It is *structurally* absent from the recall set because
   **nothing adds it to the VectorIndex at stage time** — not merely filtered out.
3. **The sole pending→approved transition** — `approve()` is the *only* code path that
   adds a row to the index, and it does so inside the *same write transaction* as the
   `status` CAS-flip. So "in the index" ⟺ "status=approved" is an enforced biconditional,
   not a discipline.

The depth: callers see four verbs (`stage`/`list_pending`/`approve`/`reject`) returning
frozen result types; behind them sit the secret-floor, the dedup, the CAS state machine,
the index-on-approve coupling, and the never-hallucinate boundary. The riskiest facts
(what a secret looks like; what "recallable" means; how status flips) are each sealed in
exactly one place. This is the §9 "Substrate / hard guarantee" row made into code.

**Why this is *one* module and not three layers:** the secret floor, the staging, and the
approve transition are a single irreversible-by-construction pipeline — a secret that
slips the scan, a pending row that reaches the index, or an approve that flips status
without indexing are all the *same* class of failure (a memory in a state it shouldn't
be). Co-locating them lets one set of invariants (and one mutation matrix) cover the whole
admission boundary. It depends on the M03 Store/M04 Index *ports* but owns the *policy*.

## 2. Public surface + ENFORCED contract

See `interface_block` for exact signatures. Key contracts and the precondition I
**design out** rather than document:

- **`ScanVerdict` cannot lie (frozen + `__post_init__`).** A `PASS`-with-findings, a
  `REDACT`-without-`redacted_text`, or a `REFUSE`-without-a-finding is *unconstructable*
  — the type raises at construction. This is the single most important "contract that
  can't lie" in the module: downstream code can branch on `action` and trust that the
  other fields are consistent, with no defensive re-checking.
- **`SecretFinding` never carries the matched secret.** It stores `rule` + `span` only.
  This is a *designed-out precondition*: there is no "remember not to log the finding"
  rule because the finding has no secret to log. Units: `span` is `[start, end)` char
  offsets into the *original* text.
- **`stage()` postcondition: the returned row is NOT in the index.** No embedding is
  computed or added at stage time. Enforced by M3 mutation test. Idempotent on
  `content_hash` (sha256 hex of the *staged* text — post-redaction if REDACT): a repeat
  stage of identical staged text by the same proposer returns the existing row
  (`deduped=True`), never a second row.
- **`stage()` raises `SecretRefused` iff `scan.action==REFUSE`** — and on that path
  *nothing is written* (no row, no blob). This is §6.1 #5a.
- **`approve()` postcondition (biconditional):** a row is in the VectorIndex **iff** its
  `status=='approved'`. `approve` is the only writer of either side, and it writes both in
  one tx. Stamps `approved_by` + `approved_ts=now()`. **Idempotent:** an already-approved
  id is a no-op (counted in `skipped_ids`, `indexed` not incremented) — replay-safe.
- **`reject()` never touches the index** (rejected rows were never indexed). Default
  deletes the pending row; `keep_rejected=True` retains it for audit. No-op on
  already-approved/unknown ids — designed so reject can't un-publish an approved memory.
- **`_RECALL_PREDICATE` is one constant** (owned by M03 Store, asserted here): the recall
  SELECT and the index-feed `scan_approved()` both read it. Admission never selects a
  recallable set by an inline `status='approved'` literal — it calls Store/Index verbs.
  This kills the verified `tombstoned=0` 4-site predicate-scatter (persistence.py
  :508/:543/:626/:654) before it can recur with the new status column.
- **Error semantics:** `stage` REFUSE → `SecretRefused` → surfaced as a JSON-RPC error on
  `hive_write` (the agent shows the user "refused: credential detected", **never** the
  secret). `approve`/`reject` never raise on bad ids — they report `skipped_ids`. CAS
  conflict on `approve` (concurrent flip) → the loser sees the id as already-approved
  (`skipped`), exactly one index entry results.

## 3. Swap seam

- **Port: `SecretScanner` (`hive/ports/secret_scanner.py`)** — `runtime_checkable
  Protocol` with one method `scan(text) -> ScanVerdict`, contractually deterministic +
  side-effect-free. **Default adapter:** `DefaultSecretScanner` wrapping the pure
  `hive/domain/secret_scan.scan` with configured thresholds. **A second adapter** (e.g. a
  vendor DLP service, or a stricter org ruleset) implements exactly `scan(text) ->
  ScanVerdict` and is selected by `secret_scan.provider` in config — **no core change**:
  `AdmissionLedger.__init__` takes the port, the domain state machine is provider-blind.
  The `ScanVerdict` frozen contract is the shared, enforced interface both adapters must
  satisfy (tested against both via the same parametrized suite).
- **Admission itself is deliberately NOT swappable.** The `pending→approved` boundary and
  "approved-only recall" are the *one* structural guarantee (§1, §9) — keeping them a hard
  literal (an allowlist: any future status is non-recallable by default) is the fail-safe
  direction and is a *feature*, per the resolved DECISION ("the synthesis preserves that
  non-swappability"). The Store/Index *ports* it drives are swappable (M03/M04); the
  admission *policy* is not.

## 4. Data owned

Admission owns **no new tables** — it writes columns the M03 episodes DDL already
defines: `status TEXT`, `proposed_by TEXT`, `approved_by TEXT NULL`, `approved_ts INTEGER
NULL`, plus the existing `content_hash`, `text`, `value` (NULL until approve? no — value
is computed at *approve* time, see below), `weight`, `ts`, `source`, `tags`. It reuses:
- **blob store** (`storage/blob_store.py`): `put(staged_text.encode())` → content-hash
  dedup via `INSERT…ON CONFLICT(content_hash) DO NOTHING`. The staged (post-redaction)
  text is the blob.
- **`SqliteMeta`** (`persistence.py:1148`) *only if* `list_pending(since=)` needs a
  durable cursor — otherwise `since` filters on `ts` directly. No new table either way.

**Embedding timing decision:** the `value` BLOB (the d-dim PCA vector) is computed at
**approve** time, not stage time, so a pending row carries no vector and *cannot* be
ranked even if a future query path forgot the predicate. (Stage stores text+hash+status
only.) This makes index-absence and value-absence two independent fail-closed defenses.

**Config keys read** (from frozen M-config; tier A unless noted):
`secret_scan.provider` (default `"default"`), `secret_scan.entropy_min_len`=20,
`secret_scan.entropy_bits_floor`=4.0, `secret_scan.redact_mode` ∈ {`refuse`,`redact`}
default **`refuse`** (fail-closed), `admission.keep_rejected`=False.

## 5. Dependencies + the boundary it must NOT cross

- **Depends on:** `SecretScanner` port; `EpisodeStore` port (M03) for `next_id`, the row
  insert, the `update_cas`-idiom status flip, `scan_approved`, and `index_on_approve`
  (which performs the value-embed + `VectorIndex.add` inside the same tx — the
  Store-owns-and-drives-the-index synthesis); an injected `now: () -> int` clock; the
  Embedder port (M02) is invoked *via the Store's `index_on_approve`*, not directly, so
  admission never holds the embedder.
- **Must NOT know about:** the **ranker (C3)**, the **abstention gate (C4)**, the
  **utility/attribution loop (C9)**, or the **producer (C10)**. Admission is strictly
  *upstream* of the loop (§8.2 "Admission is upstream of the loop — the utility loop only
  ever credits already-admitted memories"). It writes no `exposure`/`task_outcomes`/
  `utility` rows, reads no `family_scope`, and emits no producer config. Boundary name:
  **the credit boundary** — admission decides *what becomes recallable*; the loop decides
  *what recallable memories are worth*. Crossing it would couple a Phase-1 substrate
  concern to a Phase-2 gated component (the exact coupling the hive_init DECISION rejected
  for producer enrollment).

## 6. Failure-mode logging (structured JSON; secrets NEVER logged)

| Boundary | Level | Context logged (NO secret text, NO secret substring) |
|---|---|---|
| secret REFUSE | **warn** | `event=admission.refused`, `rules=[finding.rule…]`, `proposed_by`, `text_len`, `request_id`. Never the matched span content. |
| secret REDACT | **info** | `event=admission.redacted`, `rules`, `n_spans`, `proposed_by`, `request_id`. |
| stage OK | **info** | `event=admission.staged`, `pending_id`, `content_hash`(hex), `deduped`, `proposed_by`. |
| stage dedup hit | **debug** | `event=admission.dedup`, `pending_id`, `content_hash`. |
| approve OK | **info** | `event=admission.approved`, `approved_ids`, `indexed`, `approver`. |
| CAS conflict on approve | **warn** | `event=admission.cas_conflict`, `pending_id`, `expected_version` — recovery: treated as already-approved, reported in `skipped_ids`. |
| index.add failure inside approve | **error** | `event=admission.index_fail`, `pending_id`, `error`, `stack` — recovery: tx rolled back, status stays pending, surfaced to caller. |
| blob put failure (stage) | **error** | `event=admission.blob_fail`, `content_hash`, `error`, `stack` — recovery: stage aborts, no row written. |
| reject | **info** | `event=admission.rejected`, `rejected_ids`, `kept` flag. |
| unknown id on approve/reject | **debug** | `event=admission.skipped`, `ids`, `reason=not_found_or_already_approved`. |

Per the global standard: every catch block logs error+context+recovery; the secret-scan
boundary logs the *rule names and counts*, **never** the credential. `request_id` rides
every log for trace correlation.

## 7. Port disposition vs §10 map

See `port_disposition`. Summary: **BUILD-NEW** for `secret_scan` (verified ABSENT — zero
non-test hits for AKIA/sk-/ghp_/PEM/entropy/redact in the tree) and for the
staging+status state machine (over `service.py:1422 write_text`, which writes
immediately). **PORT the proven idioms** — `update_cas` rowcount-CAS (persistence.py:581),
`blob_store.put` content-hash dedup (blob_store.py:52, sha256 at :19), `SqliteMeta` UPSERT
(persistence.py:1148) — rather than reinventing them. **PORT+EXTEND** the MCP surface
(mcp_tools.py `TOOL_DEFINITIONS` + mcp_server.py `_tool_write` at :164): flip `hive_write`
to return a pending id, add the `hive_pending`/`hive_approve`/`hive_reject` trio, drop
consolidate/schemas/recall_cold/restore_cold. **DROP** `write_or_update_text`
(service.py:1478), `subject_key`/`supersede`/`bm25`/`graph` branches, and the immediate
recallability of a fresh write.

## 8. Test contract

See `test_contract` for the full enumerated list (files, names, exact assertions, the
failure each catches, gate mapping). It covers: every `ScanAction` branch + the
verdict-cannot-lie invariants (`test_secret_scan.py`); every state transition incl. dedup,
replay, CAS race, and tx-rollback (`test_admission_stage.py` / `test_admission_approve.py`);
both directions of the never-hallucinate boundary — leak AND false-negative — plus the
single-predicate guard (`test_admission_recall_boundary.py`).

**Mutation matrix (RULE 2) — each fault maps to a named red test, restore→green:**
- **M1 (secret floor):** delete the `aws_akia` regex → `test_aws_akia_refused` RED.
- **M2 (recall teeth):** in `approve`, flip status but skip `index_on_approve` →
  `test_approve_flips_status_and_indexes` (indexed==0) AND `test_approved_is_recallable`
  RED. Proves index-on-approve is the load-bearing never-hallucinate enforcement.
- **M3 (stage leak):** make `stage` also index the pending row →
  `test_pending_never_in_candidates` + `test_stage_creates_pending_not_approved` RED.
  Proves stage is index-free.

**Acceptance-gate ownership (§6.1 #5):** this module owns #5a (secret scan refuses a
planted credential before staging) → `test_stage_refuses_on_secret` + M1; and #5b (a
pending write is never recallable) → `test_pending_never_in_candidates` +
`test_recall_returns_nothing_when_only_pending` + M2/M3. #5c (neutral framing) is the MCP
render layer's concern, not admission's. A reviewer can assert *no functional path is
untested*: every action branch, every transition, both boundary directions, and the
secret-never-logged invariant each have a named test.

---

## Design review (independent pass)

**Verdict:** CONDITIONAL — strong information-hiding design with a genuinely deep module boundary, but NOT build-ready as written. The prose architecture is excellent (single recall predicate, self-asserting ScanVerdict, secret-floor-before-blob, index-on-approve coupling), and the spec's three load-bearing claims about the reference impl all verify (update_cas rowcount-CAS at persistence.py:581-606, blob_store ON CONFLICT dedup at blob_store.py:52-60, secret-scan genuinely ABSENT, tombstoned predicate scatter at :508/:543/:626). Sign-off is blocked by ONE foundational design hole and several test-contract gaps. The headline 'same write transaction' biconditional (in-index ⟺ status=approved) is NOT achievable with the ported index: ExhaustiveVectorIndex (vector_index.py:22-78) is an in-memory numpy matrix with zero persistence and zero participation in SQLite tx() — so the durable truth is status='approved' in SQLite and the index is a DERIVED projection that MUST be rebuilt from scan_approved() at boot. The contract the whole never-hallucinate story rests on is therefore a boot-rebuild invariant the spec neither names nor tests. Additionally the cited interface_block / test_contract / port_disposition artifacts DO NOT EXIST in the repo — the enforced contract and the full test enumeration are deferred to companion blocks that were not supplied, so the 'contracts that can't lie' claim is currently prose-only.

**Scores (1–10):**
- design_complexity: 4
- cognitive_load: 4
- information_leakage: 3
- extensibility_fit: 7
- agent_navigability: 5
- contract_enforcement: 6
- test_coverage: 5

**Red flags:**
- Prose-Only Contract on Tricky Semantics @ M05 §2/§3/§8 ('See interface_block', 'See test_contract', 'See port_disposition') — the enforced ScanVerdict __post_init__, the SecretScanner Protocol, and the entire test enumeration with assertions are referenced but the artifacts do not exist in the repo — root: obscurity → the 'contract that cannot lie' is currently unverifiable prose; an agent or human cannot branch on or test what is not written.
- Nonobvious Code / Implicit Wiring @ §1/§2 'inside the same write transaction … in the index ⟺ status=approved is an enforced biconditional' — the ported index (vector_index.py:22-78) is in-memory numpy that cannot join a SQLite tx, so the stated atomicity is impossible and the real invariant (boot-rebuild from scan_approved) is unstated — root: obscurity → a reader trusts an atomicity guarantee the substrate cannot provide; crash-recovery skew is an unknown-unknown.
- Special-General Mixture / Overexposure @ §2 reject() 'keep_rejected=True' flag — a rarely-used audit-retention option is exposed on the common reject() surface; per APOSD #5 the common case (drop) must not be taxed by the rare case (audit retain). Minor: a separate purge/audit verb would keep reject() narrow — root: obscurity → small interface-surface tax on the common path.
- Hard to Describe @ §2 approve() postcondition — the contract requires four clauses (biconditional + only-writer + idempotent-skip + CAS-loser-sees-already-approved) to be complete; APOSD flags long required documentation as a signal the thing is doing more than one nameable thing — here it is defensible (it IS the state machine) but it means approve() cannot be safely used from the type signature alone, so the enforced test contract must carry the full weight — root: obscurity → cognitive load concentrated on one verb.
- Scattered Truth (agent-native) @ the content_hash definition — §2 says sha256 of the STAGED text (post-redaction if REDACT), §4 says 'sha256(text) for fetch + dedup', and the episodes DDL comment (spec §3:193) says 'sha256(text)'. Three statements of the same fact with a subtle post-redaction qualifier in only one — root: dependency → an agent reading the DDL would hash the ORIGINAL text and silently break redact-path dedup + leak the original into the hash.

**Test gaps:**
- No boot/crash-recovery test: approve N → drop in-memory index → rebuild from scan_approved() must yield exactly the approved set (the real never-hallucinate enforcement once the in-tx claim is corrected). The entire M2/M3 matrix only exercises steady state.
- Secret-scan rule coverage is a single example (M1 aws_akia). No named test per §9 rule family: ghp_, sk-, xox, JWT, PEM, connection-string, and the entropy floor (entropy_min_len=20 / entropy_bits_floor=4.0 boundary pair). A deleted non-AKIA regex ships silently.
- The redact_mode='redact' branch is entirely untested: no test that staged text == redacted text, that blob/content_hash are over redacted bytes, that the original secret reaches no row/blob/log, and that dedup keys on the redacted hash. ScanVerdict.REDACT is a constructable action with a live stage() path and zero coverage.
- The 'secret never logged' invariant (§6) has no test. Need a test that plants a credential and asserts the secret substring appears in no field of any structured log record on the REFUSE and REDACT paths.
- The ScanVerdict-cannot-lie invariants are claimed ('PASS-with-findings / REDACT-without-redacted_text / REFUSE-without-finding is unconstructable') but only referenced via the missing test_contract — no named test asserts each illegal construction raises. These are the module's central enforced contract and must each have a pytest.raises test.
- index.add failure INSIDE approve (§6 error row: 'tx rolled back, status stays pending') has no named test. With an in-memory index that can't truly roll back with SQLite, this path needs an explicit test of the actual recovery (status reverts OR is reconciled on next boot-rebuild) — currently the failure-mode log is specified but the behavior is untested and, given the durability hole, possibly wrong.
- Idempotency/replay on stage() dedup across a DIFFERENT proposer is unspecified+untested: §2 says 'identical staged text by the SAME proposer returns the existing row' — what happens on identical text by a DIFFERENT proposer (second row? dedup? provenance overwrite?) is neither specified nor tested, and content_hash dedup (blob ON CONFLICT) would silently collapse them.
- CAS-conflict-on-approve concurrency test (§6 'exactly one index entry results') is named in M2 mapping but the concurrent-double-approve race itself (two approvers, same id, same tick) has no explicit named test asserting indexed==1 and the loser sees skipped — only the single-approver idempotent no-op is covered.
- The §10 reuse claim that admission writes NO exposure/task_outcomes/utility rows (the credit boundary, §5) has no negative/boundary test. A test that asserts stage()/approve()/reject() touch zero loop tables would lock the boundary the design names as load-bearing.
- value-BLOB-computed-at-approve (the second fail-closed defense, §4) has no test asserting a PENDING row's value column is NULL and an APPROVED row's value is non-NULL — the spec sells this as 'two independent fail-closed defenses' but only index-absence is tested (M3), not value-absence.

**Must-fix:**
- RESOLVE THE BICONDITIONAL/DURABILITY HOLE (design): §1/§2 claim approve() adds to the VectorIndex 'inside the same write transaction' as the status CAS-flip, making 'in the index ⟺ status=approved' an enforced biconditional. The ported ExhaustiveVectorIndex (vector_index.py:22-78) is in-memory-only numpy with NO persistence and NO SQLite-tx participation — a process can commit status=approved then crash before .add(), or the reverse, breaking the biconditional across restart. The actual enforceable invariant is 'SQLite status=approved is the durable source of truth; the in-memory index is rebuilt deterministically from Store.scan_approved() at boot.' Rewrite §1/§2/§4 to state THAT, demote the in-tx .add() to a best-effort warm-cache update, and add the boot-rebuild as a first-class named invariant.
- ADD THE BOOT-REBUILD TEST (test): there is NO test that approving N rows, discarding the in-memory index, and re-deriving it from scan_approved() reproduces exactly the approved set and nothing pending. Without it, M2/M3 mutation tests only prove the steady-state path; a crash-recovery skew (pending row surfaced after restart, or approved row missing) is untested. Name it test_index_rebuilds_from_approved_only and make it a §6.1 #5b owner alongside test_pending_never_in_candidates.
- SUPPLY THE interface_block / test_contract / port_disposition ARTIFACTS (design+test): the spec repeatedly says 'See interface_block for exact signatures' and 'See test_contract for the full enumerated list' but NONE of these exist in /home/null/Desktop/work/hivemind/. Per the agent-native rubric a contract that lives only in prose ('Prose-Only Contract on Tricky Semantics') cannot be evaluated or enforced and silently drifts. The exact frozen-dataclass __post_init__ assertions, the Protocol signature for SecretScanner, and the named test list with exact assertions must be present as real artifacts before sign-off — they are the 'contracts that cannot lie' the design's whole depth claim depends on.
- ENUMERATE THE SECRET-SCAN RULE COVERAGE AS A TABLE (test): §9 names sk-/AKIA/ghp_/xox/JWT/PEM/connection-strings + entropy, but the mutation matrix tests only M1 (aws_akia). One green test per rule family is the floor — a deleted ghp_ or PEM or JWT or connection-string regex, or a broken entropy threshold, would ship silently. Add test_<rule>_refused for EACH named family plus an entropy boundary pair (just-below entropy_bits_floor=4.0 passes / just-above refuses at entropy_min_len=20).
- PIN THE redact_mode=redact PATH (design+test): §4 says redact_mode default is 'refuse' (fail-closed) but the alt is 'redact', and ScanVerdict has a REDACT action whose postcondition is content_hash = sha256(POST-redaction text). There is no test that (a) a REDACT proposal stages the redacted text (not the original), (b) the blob and content_hash are over the redacted bytes, (c) the original secret reaches NO row/blob/log, and (d) dedup keys on the redacted hash. This is a secret-safe invariant with a live code path and zero named coverage — the redact branch could leak the original into the blob and no test would catch it.
- TEST THE never-LOGGED-secret INVARIANT (test): §6 mandates the secret text/substring is NEVER logged (warn on REFUSE logs rule names + text_len only). This is an explicit invariant with NO named test. Add test_refuse_log_contains_no_secret_substring that plants a known credential, captures the structured log records, and asserts the secret string does not appear in any field — this is a mutation-testable secret-safe guarantee currently only asserted in prose.
