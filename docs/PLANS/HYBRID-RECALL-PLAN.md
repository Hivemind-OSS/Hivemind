# Hivemind — Hybrid Recall v2 (FTS5 + RRF, store-owned, minimum viable)

**Status:** LANDED 2026-06-11 (C1–C6 built, each chunk green + RULE-2'd; suite 691 passed
locally with the `[embed]`-extra tests deselected — no sentence-transformers in the dev env).
**Date:** 2026-06-11 (v1 2026-06-10, pre-autonomy; this rebases it onto the landed trust-lifecycle tree)
**Provenance:** v1 operationalized the `/socratic 5` adversarial review (hybrid lexical+dense → capped
rerank, labels not judges, graph falsified). v2 keeps those verdicts, rebases every contract onto the
autonomy-landed code (`is_servable`, store-owned index sync, `ExposureLedger`, 6-tool surface), and
cuts to minimum viable: **Stage 1 only** (FTS5 + RRF), with the cross-encoder rerank deferred wholesale
until Stage 1 has CI-significant evidence. Pressure-tested via /software-design-review (Mode B,
design-it-twice; decisions in §6). `CONTEXT/BUGS.md` checked — registry empty, no logged failure modes
to design around; relevant agent-memory lessons applied in §9.
**Supersedes in v1, with reasons:**
- v1 §4.4/AC6 (`recall_misses`) — **already landed** by the autonomy build (`ExposureLedger.record_miss`
  inside `RecallPipeline._note_non_answer`, three miss types, secret-scanned). Reused, not rebuilt.
- v1 §4.3 ctor sketch — stale; the real `RecallPipeline.__init__` carries `ledger/clock_now/scanner/
  provisional_ttl_s/lifecycle/autonomy_enabled` (`hive/domain/recall.py:162`).
- v1 "approved-only, written at `approve()`" — superseded by `is_servable` membership synced at four
  store sites (`complete`/`set_trust`/`supersede`/`sweep_decayed`).
- v1 `Fts5LexicalIndex` adapter + `MutableLexicalIndex` + registry seam — replaced by store-owned
  in-transaction FTS (§6 D-V1). Also fixes v1's latent bug: a `content=''` contentless FTS5 table
  cannot per-row DELETE before SQLite 3.43; supersede/demote/sweep need deletes.
- v1 Stage 2 (Reranker port, two adapters, `hive[rerank]` extra, 3 config fields) — **deferred
  wholesale** (§6 D-V2). `sentence-transformers` is only present under the `[embed]` extra anyway, so
  "no new package" was conditional.

---

## 0. Goal & acceptance criteria

Add the lexical (BM25/FTS5) channel fused with the existing dense channel by RRF — **off by default**
— plus the dev-time measurement (`channel_eval`) that alone can justify flipping it on. Nothing else.

| AC | Criterion |
|---|---|
| AC1 | With `recall.hybrid=False` (default), `RecallPipeline.recall(...)` is **byte-identical to the current autonomy-enabled path**: same `RecallResult`, same order, same abstains, **and the same ledger side effects** (exposure rows, miss rows, `lifecycle.on_miss` triggers). The lexical port is never called (a raising fake pins this). |
| AC2 | With `recall.hybrid=True`, a dense-miss / lexical-hit (exact identifier, error string) that dense ranked below `recall_top_n` can surface within the confident regime; fusion is RRF (rank-based, `k=60`). |
| AC3 | **Never-hallucinate unchanged:** the entropy gate runs on the dense cosine distribution only, *before* any lexical I/O — the lexical call sits inside the CONFIDENT branch, so abstain-no-resurrect stays structural. |
| AC4 | **Servable-only holds for the lexical channel** by construction: `episodes_fts` mirrors the servable set in the same transactions that change it (4 sites + boot rebuild), and the existing resolve belt (`is_servable` at RESOLVE, `recall.py:253`) + MCP belt-2 (`mcp_server.py:354`) remain the authoritative last word for TTL lapse. |
| AC5 | **Measured, outcome-bound:** `channel_eval` recommends activation **only** on a paired per-query `bootstrap_ci` with `lo > 0` on recall@k (labels, never an LLM judge). Flipping `recall.hybrid` is a human, tier-C (restart) config change. |
| AC6 | **Exposure credit stays well-formed:** per-hit margins remain non-negative and derived from the same gate masses (D-H7, §6 D-V3); a fused hit absent from the dense candidate map is dropped at resolve (fail-closed), never KeyErrors. |
| AC7 | **Zero new dependencies, zero new tools.** FTS5 is probed at store init; a stripped SQLite degrades gracefully when hybrid is off and fails fast at boot when hybrid is on. The 6-tool MCP surface and all envelopes are unchanged. |

---

## 1. Principles (locked, carried from v1 where still true)

1. **Default-preserving, additive, reversible.** `hybrid=False` ⇒ byte-identical behavior including
   side effects. Flipping on is one config field, gated by CI evidence, reversible.
2. **Never-hallucinate is sacrosanct.** Gate input is the dense cosine list, evaluated before any
   lexical I/O; fusion only reorders the confident shortlist.
3. **Measure on labels, not a judge.** Reuse `metrics_ir.bootstrap_ci` + the content-hash label
   pattern from `eval_membrane.replay`; ship rule is `lo > 0`.
4. **One owner per fact.** All `episodes_fts` SQL lives in `SqliteEpisodeStore` (the store already owns
   dense-index sync and implements `EpisodeReader`/`ExposureLedger` for the pipeline — same idiom).
   The pipeline sees one read-only port method.
5. **Simplicity scales.** No new adapter files, no registry seam (one conceivable backend ≠ a swap
   axis), no new config group, `rrf_k=60` is a code constant (canonical, zero-tune), lexical search
   depth = `recall_top_n` (symmetric with dense; a measured knob later only if channel_eval says so).

---

## 2. Architecture

```
 hive_recall ─▶ RecallPipeline.recall(query, *, agent_id, agent_ctx)
                 │ encode → dense search (FULL servable set) → entropy gate   ── UNCHANGED
                 │      └─ ABSTAIN ⇒ record_miss → return            (lexical code unreachable)
                 │ ── CONFIDENT branch ─────────────────────────────────────────
                 │ [NEW, hybrid only]  lex = lexical.search_text(query, recall_top_n)   # port → store FTS
                 │                     shortlist = rrf_fuse([dense_ids, lex_ids])[:recall_top_n]
                 │                     (any lexical/fuse fault ⇒ shortlist := dense order, log, degrade)
                 │ resolve shortlist → is_servable belt → masses by eid → D-H7 margins   ── belt UNCHANGED
                 │ surfacer.order → RecallResult → record_exposure                       ── UNCHANGED
                 ▼
 WRITE SIDE (store-owned, in-tx, mirrors the dense index's 4 sync sites + boot rebuild):
   complete(trust servable)   → fts upsert      set_trust(enter/leave servable) → fts upsert/delete
   supersede(target)          → fts delete      sweep_decayed(lapsed provisional) → fts delete
   Container.build_index()    → store.rebuild_fts(now, ttl)   (self-heal, after the dense rebuild)
```

Load-bearing boundary facts:
- **FTS is durable and transactional** (same SQLite file), unlike the in-RAM dense warm cache — it
  cannot diverge from the rows it was written with; the boot rebuild is defense-in-depth, and the
  resolve belt covers the one staleness mode neither sync can see (TTL lapse by clock, no transition).
- The lexical channel reuses the **store as the port implementation** (`lexical_index=store`), exactly
  as `reader=store` and `ledger=store` already do in `build_container` (`container.py:233-239`).
- A divergence between FTS and the dense candidate list (possible only via the dense cache's
  best-effort sync failing) resolves fail-closed: a fused id with no dense mass is dropped at resolve.

---

## 3. Contracts (exact signatures — written before code)

### 3.1 `hive/domain/fusion.py` (NEW — pure, stdlib only; auto-covered by the test_purity fence)

```python
def rrf_fuse(rankings: "Sequence[Sequence[int]]", *, k: int = 60) -> list[int]:
    """Reciprocal Rank Fusion over N rank-ordered id lists.
    score(id) = Σ_r 1/(k + rank_r(id)), rank 0-indexed; absent from a ranking ⇒ contributes 0.
    Returns ids fused-score-descending; ties keep first-seen order (stable, deterministic).
    Duplicate ids within one ranking: first occurrence counts. Empty input ⇒ [].  // O(Σ|rankings|)."""
```

### 3.2 `hive/domain/ports.py` (ADD — one read-only port, one method)

```python
@runtime_checkable
class LexicalIndex(Protocol):
    """Exact lexical (BM25) search over the SERVABLE episode text set. Implemented by the
    episode store (FTS5 mirrors servable membership in the store's own transactions); the
    pipeline depends only on this method. May serve a TTL-lapsed row — the resolve belt
    (is_servable at RESOLVE) is the authoritative filter, same as for the dense index."""
    def search_text(self, query: str, k: int) -> list[tuple[int, float]]: ...  # (episode_id, score) desc
```

No `MutableLexicalIndex`: the write side is store-internal SQL, not a port.

### 3.3 `hive/adapters/store_sqlite.py` (MODIFY — FTS schema probe + 4 in-tx sync sites + 2 methods)

```python
# __init__ (signature UNCHANGED): after executescript(_SCHEMA), probe FTS5:
#   try: conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS episodes_fts USING fts5(text, tokenize='porter unicode61')")
#        self.fts_enabled = True
#   except sqlite3.OperationalError: self.fts_enabled = False   # stripped build — log once, degrade
# (plain FTS5 table, NOT contentless: per-row DELETE must work on every SQLite that has FTS5)

# In-tx maintenance (each guarded by `if self.fts_enabled:`), mirroring the dense sync sites:
#   complete():  servable trust ⇒ _fts_upsert(episode_id)           (quarantined ⇒ absent, like the dense index)
#   set_trust(): enter servable ⇒ _fts_upsert; leave servable ⇒ _fts_delete
#   supersede(): _fts_delete(target_id)
#   sweep_decayed(): _fts_delete(eid) per lapsed PROVISIONAL flip   (in the same tx as the trust flip)
# _fts_upsert = DELETE rowid + INSERT INTO episodes_fts(rowid, text) SELECT id, text FROM episodes WHERE id=?
#   (idempotent delete-then-insert; INSERT-SELECT so text never round-trips through Python)

def search_text(self, query: str, k: int) -> list[tuple[int, float]]:
    """LexicalIndex port. Tokenize the raw query (re.findall(r"\\w+"), cap 64 tokens), build an
    OR-of-quoted-tokens MATCH (quoting defines FTS5-syntax injection/errors out of existence),
    ORDER BY bm25(episodes_fts) LIMIT k; return (id, -bm25) score-descending.
    No tokens / fts disabled / OperationalError ⇒ [] (fail-open to the dense channel)."""

def rebuild_fts(self, *, now: int, provisional_ttl_s: int) -> None:
    """Boot self-heal, sibling of rebuild_index_from_store: delete-all, re-insert the servable
    set (same is_servable per-row filter as scan_servable). No-op when fts disabled."""
```

### 3.4 `hive/domain/recall.py` (MODIFY — two optional ctor kwargs + the fusion step + D-H7 margins)

```python
class RecallPipeline:
    def __init__(self, *, embedder, index, gate, surfacer, reader, utility_store,
                 recall_top_n, ledger, clock_now, scanner, provisional_ttl_s,
                 lifecycle=None, autonomy_enabled=True,
                 lexical_index: "Optional[LexicalIndex]" = None,   # NEW
                 hybrid_enabled: bool = False) -> None: ...        # NEW (recall.hybrid)
    # recall(): UNCHANGED through encode/search/gate/abstain. In the CONFIDENT branch:
    #   1. shortlist ids: if hybrid_enabled and lexical_index — own try/except:
    #          lex_ids = [eid for eid, _ in lexical_index.search_text(query, self.recall_top_n)]
    #          shortlist = rrf_fuse([dense_ids, lex_ids], k=60)[: self.recall_top_n]
    #      any raise ⇒ shortlist := dense_ids[: self.recall_top_n]  (degrade to dense, log, NEVER to EMPTY)
    #      else: shortlist := dense_ids[: self.recall_top_n]        (identity — AC1)
    #   2. masses: mass_by_eid = {eid: full_masses[i] for i,(eid,_) in enumerate(candidates)}
    #      resolve each shortlist eid via reader + is_servable belt (unchanged);
    #      eid missing from mass_by_eid ⇒ drop at resolve (fail-closed divergence guard, AC6).
    #      RecallHit.sim / Scored.sim stay the DENSE cosine (honest, calibrated; available for
    #      every servable id because the dense search spans the full index).
    #   3. margins (D-H7): compute D1 margins over the RESOLVED set's masses in MASS-DESCENDING
    #      order (sort masses desc → consecutive diffs → map back by eid). Reduces byte-identically
    #      to today's computation when the shortlist is dense-ordered; non-negative under fusion.
    #   4. surface + exposure: unchanged (exposure records the fused, surfaced set — a lexical
    #      resurfacing IS a serve, so its liveness refresh is correct).
```

### 3.5 `hive/app/config.py` (MODIFY — one field, one tier entry)

```python
# RecallConfig: hybrid: bool = False    (no validation needed — bool)
# RELOAD_TIER:  "recall.hybrid": "C"    (changes index wiring + read path ⇒ restart)
```

Activation path (operational): the switch is the env layer `Config.load` already reads —
`HIVE_RECALL__HYBRID=true` + restart. In the compose deployment that means naming it in
`compose.yaml`'s `environment:` block (compose does NOT forward arbitrary `.env` vars; add a
passthrough `HIVE_RECALL__HYBRID: "${HIVE_RECALL__HYBRID:-false}"` if the flip should live in `.env`).
No code default changes: hybrid ships off by construction, autonomy remains on by construction.

### 3.6 `hive/app/container.py` (MODIFY — wiring + fail-fast + boot rebuild)

```python
# build_container(): after store construction —
#   if cfg.recall.hybrid and not store.fts_enabled:
#       raise RuntimeError("recall.hybrid=true but this SQLite build lacks FTS5 …")   # fail fast at boot
#   lexical = store if cfg.recall.hybrid else None
#   RecallPipeline(..., lexical_index=lexical, hybrid_enabled=cfg.recall.hybrid)
# Container.build_index(): after rebuild_index_from_store(...) —
#   self.store.rebuild_fts(now=..., provisional_ttl_s=...)     # no-op when fts disabled
# _REQUIRED_TABLES: UNCHANGED (episodes_fts is optional-by-environment; the hybrid fail-fast covers it)
```

### 3.7 `hive/research/channel_eval.py` (NEW — dev-time, behind the existing AST research fence)

```python
@dataclass(frozen=True)
class ChannelEvalResult:
    arms: dict                              # 'dense' | 'hybrid' → {'recall@k': float, 'ndcg@k': float}
    hybrid_vs_dense_ci: tuple[float, float, float]   # metrics_ir.bootstrap_ci on per-query recall@k deltas
    recommend_hybrid: bool                  # hybrid_vs_dense_ci[1] > 0   (lo > 0 — the §6.2 ship rule)

def run_channel_eval(labeled: "Sequence[tuple[str, set[str]]]",   # (query, relevant content-hashes)
                     *, k: int = 5, n_boot: int = 10_000, seed: int = 0,
                     store_fixture=None) -> ChannelEvalResult: ...
# Two pipelines over the SAME hermetic store (HashEmbedder + fixture pattern from eval_membrane),
# differing only in hybrid_enabled; scored per query with metrics_ir.recall_at_k/ndcg_at_k over
# content-hash labels (the replay() convention); paired deltas → bootstrap_ci. Labeled sets come
# from the LOCOMO/LongMemEval loaders and (over time) labeled recall_misses rows.
```

---

## 4. Files touched (complete list)

| File | Change |
|---|---|
| `hive/domain/fusion.py` | **ADD** `rrf_fuse` (pure; the existing purity fence auto-covers it — no test_purity edit) |
| `hive/domain/ports.py` | **ADD** `LexicalIndex` (one method) |
| `hive/adapters/store_sqlite.py` | **MODIFY** FTS probe in `__init__`; in-tx sync at `complete`/`set_trust`/`supersede`/`sweep_decayed`; `search_text`; `rebuild_fts` |
| `hive/domain/recall.py` | **MODIFY** two optional kwargs; fusion step; `mass_by_eid`; D-H7 margins |
| `hive/app/config.py` | **MODIFY** `RecallConfig.hybrid`; `RELOAD_TIER["recall.hybrid"]="C"` |
| `hive/app/container.py` | **MODIFY** hybrid fail-fast; `lexical_index` wiring; `rebuild_fts` in `build_index()` |
| `hive/research/channel_eval.py` | **ADD** dev-time arm comparison |
| `tests/domain/test_fusion.py`, `tests/store/test_store_fts.py`, `tests/domain/test_recall_hybrid.py`, `tests/container/` (extend), `tests/config/` (extend), `tests/research/test_channel_eval.py` | **ADD/EXTEND** per §5 |
| `docs/01-DECISIONS.md`, `docs/00-PROBLEM.md`, `docs/06-DESIGN-DOC.md` | **MODIFY** record D-G1/D-V1/D-V2/D-V3; flip the hybrid deferral to "implemented, gated-off pending channel_eval"; rerank stays deferred |

**Not touched:** `mcp_server.py`, `tool_defs.py` (6-tool surface + envelopes unchanged), `registry.py`
(no new seam), `models.py`, `pyproject.toml` (zero deps), `surfacer.py`, the gate, `lifecycle.py`.

## 5. Tests (written first — the enforced contracts; fakes in ms, sqlite tests via the prod `connect()` factory)

| Test | Contract |
|---|---|
| `test_fusion.py::test_rrf_is_rank_based_and_stable` | order depends only on ranks; ties first-seen; empty ⇒ []; duplicate-id-in-one-ranking counts once |
| `test_store_fts.py::test_fts_mirrors_servable_membership` | quarantined complete ⇒ absent; established/provisional complete ⇒ present; promote ⇒ present; demote/supersede/sweep ⇒ absent — asserted via `search_text` after each transition (AC4) |
| `test_store_fts.py::test_rebuild_fts_self_heals` | manual FTS corruption (raw SQL delete) then `rebuild_fts` ⇒ membership restored |
| `test_store_fts.py::test_search_text_hostile_query_safe` | FTS5-syntax queries (`"a AND ("`, `-x`, quotes) return results or [] — never raise (AC7) |
| `test_store_fts.py::test_store_satisfies_lexical_port` | `isinstance(store, LexicalIndex)` + behavioral conformance (the protocol-widening lesson: conformance-test the REAL adapter, not just a fake) |
| `test_recall_hybrid.py::test_hybrid_off_is_byte_identical_and_lexical_untouched` | raising-fake lexical + `hybrid_enabled=False` ⇒ result AND ledger side effects equal a pipeline built without the feature (AC1) |
| `…::test_gate_abstains_before_lexical_io` | a forced-abstain query with hybrid on ⇒ abstain identical, lexical fake never called (AC3) |
| `…::test_lexical_only_hit_surfaces_confident` | dense ranks E below top_n, lexical ranks it #1 ⇒ surfaced with hybrid on (AC2) |
| `…::test_lexical_fault_degrades_to_dense` | lexical raises ⇒ dense-only result, CONFIDENT, never EMPTY |
| `…::test_fused_id_without_dense_mass_dropped` | id in lexical but absent from dense candidates ⇒ dropped at resolve, no KeyError (AC6) |
| `…::test_margins_nonnegative_under_fusion` | fused (non-monotone-mass) order ⇒ every exposure margin ≥ 0; hybrid off ⇒ margins byte-equal current (AC6/D-V3) |
| config/container extends | `hybrid` default False; tier C entry present; `hybrid=True` + `fts_enabled=False` ⇒ boot RuntimeError; hybrid wires `lexical_index is store` |
| `test_channel_eval.py::test_recommend_only_on_ci_lo_gt_0` | positive point delta with `lo <= 0` ⇒ `recommend_hybrid=False` (AC5) |

## 6. Design decisions (design-it-twice — chosen vs rejected)

**D-V1 — Store-owned, in-transaction FTS vs a separate `Fts5LexicalIndex` adapter (v1's design).**
*Rejected (v1):* a Python-side adapter synced best-effort from the store, like the dense warm cache —
two modules know `episodes_fts` (the adapter reads, the store triggers), a second handle threads
through container/store, sync is post-commit best-effort though the table is durable, and v1's
contentless table can't per-row DELETE. *Chosen:* the store owns every byte of FTS SQL in the same
transactions that move trust states (atomic, can't diverge), and exposes one read-only port method —
the exact `reader=store` / `ledger=store` idiom already in the tree. Fewer files, stronger consistency,
one owner. *Trade accepted:* the EpisodeStore god-port grows by two methods (resolution B5 already
accepts this shape; the method group is cohesive).

**D-V2 — Defer the cross-encoder rerank wholesale (v1 shipped its production scaffolding off-by-default).**
Sequential evidence: rerank gains stack on a hybrid baseline, so its eval is only meaningful after
hybrid is measured; building a port + two adapters + an extra + three config fields ahead of any
evidence is scaffolding-before-measurement. Cutting it removes ~40% of v1's surface. *Add-back:* a
`Reranker` port + `NoopReranker`/`CrossEncoderReranker` behind `hive[rerank]` (declaring
`sentence-transformers` explicitly), capped at a fused-shortlist `rerank_top_k` with the
`recall_top_n <= rerank_top_k` load-time guard — exactly v1 §4.2, when `channel_eval` (extended with a
rerank arm) justifies it.

**D-V3 (the v1 gap, D-H7) — Exposure margins under reordering.** *Rejected:* RRF-score-based margins
(uncalibrated, breaks the D1 "same masses the gate computed" invariant); skipping exposure for
lexical-resurfaced hits (breaks liveness refresh — the exposure-resurrection lesson says serve ⇒
refresh). *Chosen:* per-hit margins over the resolved set's **dense masses in mass-descending order**
— same helper, same masses, non-negative by construction, byte-identical reduction when hybrid is off.
A lexical-resurrected hit gets a small (own-mass-floored) credit weight: conservative, and currently
**zero live impact** because the surfacer is observed-not-applied (`enabled=False`); revisit only with
the keystone/utility work.

**D-V4 — FTS maintained always (when available) + boot rebuild, vs flag-gated maintenance.**
Flag-gated writes would make `recall.hybrid` a *data* migration (backfill state machine) instead of a
read-path switch. Always-maintain keeps the flag pure read-path, costs one indexed insert per lifecycle
transition, and `rebuild_fts` at boot self-heals pre-feature stores and any drift. A stripped SQLite
(no FTS5) degrades to `fts_enabled=False`: silent and harmless with hybrid off, fail-fast at boot with
hybrid on.

**D-V5 — No registry seam, no `lexical_backend`/`rrf_k` config.** One conceivable backend is not a
swap axis (the §10 mandate covers real seams); `rrf_k=60` is the canonical zero-tune constant — a
config field for a constant nobody tunes is overexposure. Lexical depth = `recall_top_n` (symmetric);
if channel_eval ever shows deeper lexical lists matter, that's one measured knob later.

**D-G1 — Knowledge-graph layer formally dropped** (carried verbatim from v1: vector beats GraphRAG on
local-factual recall, ~3× cost, reference branches already stripped). Add-back: a separate
global-sensemaking tool, never the recall hot path. Recorded in `docs/01-DECISIONS.md`.

## 7. Dependencies

None. FTS5 ships inside CPython's bundled SQLite (probed at store init, never assumed); fusion is
stdlib; channel_eval uses the already-present numpy + research substrate.

## 8. Implementation order (each chunk green before the next; all additive)

1. **`fusion.py` + `LexicalIndex` port + tests** — pure/zero-wiring; purity fence auto-covers.
2. **Store FTS** (probe, 4 in-tx sites, `search_text`, `rebuild_fts`) + store/conformance tests —
   unused by recall ⇒ suite stays green; FTS table appears and mirrors membership.
3. **Config field + tier entry** + tests — inert default.
4. **Recall fusion path + container wiring + boot pieces** + the AC1/AC3 identity tests — default-off
   ⇒ byte-identical; the only chunk that touches the hot path.
5. **`channel_eval.py`** + tests — the instrument that decides whether `recall.hybrid` ever flips.
6. **Docs**: decisions recorded, deferrals flipped, v1 plan superseded by this file.

Safe because 1–3 are dead code until chunk 4, chunk 4 is flag-inert by default with identity pinned by
tests, and 5–6 are dev-time/docs only.

## 9. Mutation protocol (RULE-2, per chunk; foreground + `timeout`, Edit-based restores with unique
multi-token anchors, grep-all-occurrences after every restore, clear `__pycache__` on same-size
restores, sqlite test conns ONLY via the prod `connect()` factory)

| Mutation | Test that must go red |
|---|---|
| `rrf_fuse`: `1/(k+rank)` → `1/(k+1)` | `test_rrf_is_rank_based_and_stable` |
| `complete()`: drop the servable-trust guard on `_fts_upsert` (index quarantined too) | `test_fts_mirrors_servable_membership` |
| `set_trust()` demote: skip `_fts_delete` | `test_fts_mirrors_servable_membership` |
| `recall()`: extend the gate's `sims` with lexical scores (gate input contaminated) | `test_gate_abstains_before_lexical_io` |
| `recall()`: skip the mass-descending sort in D-H7 margins | `test_margins_nonnegative_under_fusion` |
| `recall()`: drop the `mass_by_eid` miss-drop guard | `test_fused_id_without_dense_mass_dropped` |
| `recall()`: ignore `hybrid_enabled` (always fuse) | `test_hybrid_off_is_byte_identical_and_lexical_untouched` (raising fake) |
| `channel_eval`: `lo > 0` → `point > 0` | `test_recommend_only_on_ci_lo_gt_0` |

## 10. Open questions (to settle before flipping the flag live, not before merging)

- **Labeled-set size/provenance for the first real channel_eval run** — LOCOMO/LongMemEval loaders vs
  hand-labeled `recall_misses`; `bootstrap_ci` is exact about its inputs (raises on empty) but a tiny
  n yields wide CIs — expect "not recommended" until the set is real (that is the gate working).
- **Lexical-under-dense-ambiguity** (carried from v1 §10): does gating on dense suppress exact-match
  lexical wins when the dense field is ambiguous? Measure the false-abstain rate from labeled misses
  before considering any lexical-confidence path (deferred, add-back only on evidence).

## 11. Deferred / add-back table

| Deferred | Add-back |
|---|---|
| Cross-encoder rerank (Stage 2, all of it) | v1 §4.2 design behind `hive[rerank]`, after channel_eval(+rerank arm) shows `lo > 0` on top of hybrid |
| Knowledge-graph / GraphRAG channel | separate global-sensemaking tool if that requirement ever appears (D-G1) |
| Lexical-confidence gate | only if the §10 false-abstain measurement justifies it |
| Lexical depth / `rrf_k` as config | one measured knob, only if channel_eval shows sensitivity |
| ColBERT / multi-vector, ANN scale-out | unchanged from v1 §12 |

---

**This plan ships exactly one new capability — a servable-mirrored FTS5 lexical channel fused by RRF
inside the confident regime — implemented as ~6 small, separately-green chunks with zero new
dependencies, zero new tools, zero new adapter files, one new config bit, and the measurement that
alone can turn it on.**
