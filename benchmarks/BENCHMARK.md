# Benchmarks — evaluation portfolio for the memory system

This doc specifies every benchmark run against this memory system: what each one measures, its
arms, its metrics, and the single rule by which a result is allowed to ship. Each is **dev-time
only** — fenced out of the server's import graph (the runtime never imports `hive.research`; the
purity test enforces it), shipping no runtime dependency. Everything lives under `hive/research/`.

Status: **[runnable]** a harness exists today · **[planned]** the method is specified here, the
harness is not yet built.

## The headline (value proposition)  [planned]

Every benchmark below feeds **one operator-legible claim** — the reason a fleet pays the
cost of running a memory server at all:

> **A cheaper model *with* Hivemind matches or beats a frontier model *without* it — at a
> fraction of the cost.**

This is the cost-anchored, model×memory framing the memory-systems space is judged on (a
small model + good memory out-competing a large model on raw capability). Hivemind states
it as a **measured, paired contrast**, never a slogan:

- **Arms:** `H` = a cheap base model (e.g. Haiku) with Hivemind recall in context; `F` =
  a frontier base model (e.g. Opus) with **no** memory. Same task suite, same harness, the
  ONLY differences are model tier and memory-on/off.
- **Metrics:** task-success rate Δ (`H − F`) under the **same ship gate as every other
  benchmark** — the per-question paired bootstrap CI must exclude 0 (`lo > 0`) for "cheap
  + memory ≥ frontier alone" to ship — reported **alongside the cost ratio** `cost(F) /
  cost(H)` (deterministic from tokens × published price, not a CI quantity).
- **The sentence that ships:** *"{cheap}+Hivemind {matched|beat} {frontier} alone on
  {task}: success Δ = ⟨x⟩, CI [⟨lo⟩,⟨hi⟩], at ⟨N⟩× lower cost."* Every ⟨…⟩ is filled
  **from the run** and provenance-stamped (dataset hash, both base-model names, LLM
  call-log digest, seeds, ks); the generator refuses to emit on incomplete provenance.
  **No figure is ever hand-set** — a number with no surviving CI is reported as
  *inconclusive*, not as a headline.
- **Status [planned]:** the runner today fixes the model/extractor and varies the *backend*
  (Benchmark 1). The headline adds a **model-tier axis** (`H` vs `F`) as a sibling arm on
  the same `hive.research.bench` harness; until that arm runs, this claim has **no shippable
  number** and must not be quoted.

## Shared contract (every benchmark below)

- **Ship gate — one rule, everywhere.** A result ships only when a **per-question paired bootstrap
  CI** on `primary − baseline` excludes 0: ships iff `lo > 0`, regresses iff `hi < 0`, else
  inconclusive. A bare point gain never ships. (`hive/research/metrics_ir.py:bootstrap_ci`.)
- **Metric vocabulary** (`metrics_ir.py`): `hit@k` (any gold-source unit in top-k), `recall@k`,
  `precision@k`, `mrr`, `ndcg@k`, `jaccard@k`, `abstention_auroc`. A false abstention scores 0 — it
  is counted as the real cost it is, never silently skipped.
- **LLM access — Claude subscription ONLY.** When a benchmark makes model calls they route through
  one adapter that shells `claude -p "<prompt>" --output-format json` — no raw Anthropic/OpenAI key,
  no SDK. Calls are content-hash **cached + logged**; a re-run replays from the log bit-for-bit at a
  fixed seed.
- **Reproducibility & honesty.** Reports are provenance-stamped (dataset hash, base-model name,
  extractor, LLM call-log digest, seeds, ks) and the generator refuses to emit on incomplete
  provenance. Dev/test split, frozen un-tuned prompts and thresholds — never tuned on reported
  questions. A labelled control (an Oracle/ceiling arm) is never reported as a product number.

## 1. Store + orchestrator gate vs mem0 — comparability  [runnable]

An **orchestrator** agent curating a fleet of **subagents'** writes into the trust-gated store,
scored head-to-head against [mem0](https://github.com/mem0ai/mem0) on
[LongMemEval](https://github.com/xiaowu0162/LongMemEval).

**Claim under test (isolated):** orchestrator-in-loop curation into the trust-gated store beats
mem0 **on equal footing** — the **same base embedding model** both sides, mem0 run as a raw store
(`infer=False`, no LLM consolidation) — so the number isolates the **store + the orchestrator
gate**, not the embedder. The store additionally has an honest-abstention capability (a
normalized-entropy gate) that mem0 lacks.

**Topology** — one shared store, distinct seats over an in-process driver (no socket/token hop):

- **Subagents** (≥4 seats) read the shared store (`recall`), then LLM-distill atomic facts from each
  session and **propose** them — a genuine write that lands un-served (the store quarantines it;
  mem0 holds it outside the store).
- **The orchestrator** (its own seat) is the human-approval gate: it **approves** a proposal into
  the servable store (`commit`) or denies it (never commits — it decays). It is the sole promotion
  authority (demand-promotion is disabled), so only what it approves is ever served.

**Arms (backend × gate):**

|          | AllowAll (floor) | Orchestrator-LLM | Oracle (ceiling) |
|----------|------------------|------------------|------------------|
| Hivemind | A1               | **A2 (headline)**| A3               |
| mem0     | **B1 (baseline)**| B2               | B3               |

Primary contrast **A2 vs B1**. `AllowAll` approves everything (no curation). `Oracle` approves only
facts from gold-evidence sessions — a **labelled control / upper bound**, never a product number.
`Orchestrator-LLM` is the realistic gate: a Claude-subscription LLM judges keep/drop. mem0 runs
fully offline (a local sentence-transformers embedder, a local Qdrant, an unused keyless LLM slot,
`infer=False`) and makes **zero API calls**; preflight fails fast if the `claude` CLI is absent or
unauthenticated, or the dataset is missing — no silent defaults.

**Benchmark-specific metrics** (gate aggressiveness separated from retrieval quality):

- **hit@k** and **MRR** — over ALL answerable questions; an abstention scores 0. hit@k is
  granularity-robust across backends (mem0 and Hivemind ingest at different units).
- **coverage** — answer-rate (1 − abstain-rate), so the abstention cost is visible.
- **recall@k (answered-only)** — retrieval quality *when the system commits to an answer*; secondary.
- **abstention AUROC** — native answerable-vs-unanswerable separability (Hivemind: `1 −
  entropy_norm`; mem0: top score). Native-separability, **not** a calibration claim — the proxies
  differ by backend, disclosed.

### Running it

```bash
pip install -e ".[bench]"                         # mem0ai (pinned) + sentence-transformers + hf-hub
export HIVE_BENCH_LME_PATH=/path/to/longmemeval_s_cleaned.json

# the real head-to-head (downloads the base embedder once; makes Claude-subscription calls)
python -m hive.research.bench.run \
  --backend hivemind --gate llm \
  --baseline-backend mem0 --baseline-gate allowall \
  --extractor claude --recall-hfrac 0.9 \
  --dataset "$HIVE_BENCH_LME_PATH" --n 50 --out report.json
```

Flags: `--backend/--gate` (primary arm), `--baseline-backend/--baseline-gate` (default
`mem0/allowall`), `--extractor {claude,verbatim}`, `--recall-hfrac` (the recall gate threshold —
see below), `--n` (seeded subsample, default all), `--seeds` (comma-separated), `--out`.
`--extractor verbatim` swaps LLM extraction for a deterministic "echo the user turns" extractor — a
zero-cost offline baseline (pair only with `allowall`/`oracle`). Offline smoke (no API, no
download): `pytest tests/research/bench/`.

### Calibrating the recall gate (required for Hivemind)

Hivemind abstains via a normalized-entropy gate: it suppresses the whole result when the candidate
similarities are flat (no clear winner). LongMemEval's multi-evidence / aggregation questions ("how
many times…", "total cost of X and Y") spread the mass across several comparably-relevant facts, so
at the **production default `H_frac_max=0.5` Hivemind abstains on essentially every question** —
coverage and hit@k collapse to ~0 even though the store retrieves the right facts (verified: hit@10
recovers to 3/3 on a 3-case sample as the threshold is relaxed). This is a calibration property, not
a retrieval failure. `--recall-hfrac` exposes the threshold; calibrate it on the **dev slice** (the
value that maximizes dev hit@k / coverage) and report the held-out test slice with that value. The
threshold used is stamped in the report's provenance, so a flattering hand-pick is visible. mem0 has
no such gate, so this knob does not affect it. (The *production* threshold is itself chosen by
Benchmark 3, from store-grounded replay — not hand-set.)

### Disclosed confounds (so the headline means what it claims)

- **Embedder geometry asymmetry** — both sides use the same base model (`BAAI/bge-small-en-v1.5`);
  Hivemind projects its native 384 dims through a frozen PCA head to 256, mem0 keeps native 384. The
  projection is Hivemind's own (lossy) store geometry — it counts for/against Hivemind, never a
  thumb on the scale.
- **Lost-evidence is a counted miss** — an answerable question whose evidence facts were never
  committed (the gate/extractor dropped them) is scored as a guaranteed miss, never silently
  skipped; otherwise an over-aggressive gate would be flattered by hiding what it curated away.
- **mem0 has no honest abstention** — it returns top-k whenever its store is non-empty, so its
  coverage is ~1.0 by construction; the abstention contrast is the point, not a defect to hide.

## 2. Recall-channel flips — in-domain, sliced  [planned]

Each gated-off serve-path switch — `recall.hybrid` (the FTS5+RRF lexical channel), and likewise
`recall.shadow` and `recall.drafts` — is flipped on **only** by an in-domain benchmark whose query
distribution matches production. Benchmark 1 runs these OFF and does not decide them. The BM25/hybrid
flip is the worked instance below; `shadow` and `drafts` use the same set and method.

**Why a dedicated in-domain set (and why NOT LongMemEval):** the conditions under which a lexical
channel helps are settled in IR, not in question — exact-token / rare-term queries (identifiers,
error strings, paths, commit SHAs, versions), an embedder-OOD domain (code tokens vs a general-web
base model), high query↔target lexical overlap. Real recall traffic sits heavily in that region.
LongMemEval/LoCoMo are paraphrastic conversational queries — the dense-favoring regime — so they
UNDER-credit lexical and must not decide the flip. Only an in-domain set decides it.

**The set:**

- **Corpus** = real captured hive memories (the servable units, not passages).
- **Queries** = real recall traffic / `recall_misses`, or human-authored — sourced INDEPENDENTLY of
  the target memory's text. Deriving a query from the memory's own tokens bakes in lexical overlap
  (a reward-hack: BM25 looks great and never transfers). Independent sourcing is mandatory.
- **Gold** = the target memory id(s) per query.

**Slices** (reported per-slice, never one aggregate, so the flip is *characterized* not averaged
away): query type (exact-identifier / error-string / path vs paraphrase); query↔gold
lexical-overlap bucket (none / partial / high); gold-token rarity; corpus-size sweep (seeded
subsample N) to locate the crossover where the channel starts winning.

**Metrics (dual-axis — recall alone is insufficient):**

- **recall@k / hit@k / MRR / precision@1 lift** (channel-on − channel-off), paired CI.
- **intra-confident mis-ranking harm** — the cost here is NOT false-abstention. The entropy gate
  decides suppress/answer on the DENSE distribution BEFORE any lexical I/O, and a fused id with no
  dense mass drops fail-closed, so **abstention is invariant to the flag** (its delta ≈ 0 by
  construction — not a metric to chase). The measurable harm is a low-dense-mass, lexically-plausible
  memory outranking the true one inside CONFIDENT: track precision@1 / MRR regression and a
  wrong-memory-promoted rate.

**Ship gate:** flip the channel on iff, on the slices that dominate real traffic, the recall-lift CI
has `lo > 0` AND the mis-ranking-harm CI does not regress (`hi < 0` on a harm metric kills the
flip). An aggregate win that hides a harmed dominant slice never ships.

**Generalization guards (NOT deciders):** BEIR (the literature yardstick for
BM25-vs-dense-vs-hybrid; its per-task gaps map the condition curve), LongMemEval-M, and LoCoMo run
only to confirm the channel does not REGRESS the paraphrastic regime. They never decide the flip —
the in-domain set does.

## 3. Entropy-gate calibration  [runnable]

The instrument that alone can justify changing the gate's production thresholds
(`recall.H_frac_max`, `recall.softmax_beta`) — `hive/research/gate_eval.py`. (Benchmark 1's
`--recall-hfrac` is a per-run dev knob for one comparison; this chooses the value that ships.)

**Method:** labels are reconstructable from STORED DATA ONLY (no served-query vectors are retained).
Each stored miss vector is replayed against the servable rows **restricted to `ts < miss.ts`** — a
strong top-1 (sim ≥ `label_tau`) means the answer already existed when the gate abstained ⇒ a
**false-abstain** candidate; a weak field ⇒ a **true abstain**; a miss with no predating rows is
skipped (the gate had no field to judge). Every sweep arm `(H_frac_max, softmax_beta)` replays the
SAME per-miss field through the real `NormalizedEntropyGate`; per-query decision correctness (serve
when false-abstain, abstain when true) feeds the paired CI.

**Ship gate:** recommend a change iff the best arm's CI over per-query correctness deltas vs the
CURRENT config has `lo > 0`. The change itself stays **operator-applied** (both knobs are
operator-authority). Degenerate label sets (all-hit / all-miss) RAISE, never mask — a verdict on
one-class data would be decorative.

API: `spec_from_store(store, …)` → `run_gate_eval(spec, sweep=[(H, β), …])` → `GateEvalResult`.
Exercised by `tests/research/test_gate_eval.py`.
