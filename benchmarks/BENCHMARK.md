# Benchmark — orchestrator-in-loop LongMemEval head-to-head (Phase 1)

The first runnable benchmark for this memory system: a single **orchestrator** agent curating a
fleet of **subagents'** writes into the trust-gated store, scored head-to-head against
[mem0](https://github.com/mem0ai/mem0) on [LongMemEval](https://github.com/xiaowu0162/LongMemEval).

Everything here lives under `hive/research/bench/` and is **dev-time only** — it is fenced out of
the server's import graph (the purity test enforces this) and ships no runtime dependency.

## Claim under test (ISOLATED headline)

Orchestrator-in-loop curation into the trust-gated store beats mem0 **on equal footing** — the
**same base embedding model** both sides, mem0 run as a raw store (`infer=False`, no LLM
consolidation) — so the number isolates the **store + the orchestrator gate**, not the embedder.
The store additionally has an honest-abstention capability (a normalized-entropy gate) that mem0
lacks. A result is reported as shipped only when its per-question paired bootstrap CI excludes 0.

## Topology

One shared store, distinct seats over an in-process driver (no socket/token hop):

- **Subagents** (≥4 seats) read the shared store (`recall`), then LLM-distill atomic facts from
  each session and **propose** them — a genuine write that lands un-served (the store quarantines
  it; mem0 holds it outside the store).
- **The orchestrator** (its own seat) is the human-approval gate: it **approves** a proposal into
  the servable store (`commit`) or denies it (never commits — it decays). It is the sole promotion
  authority (demand-promotion is disabled), so only what it approves is ever served.

## Arms (backend × gate)

|          | AllowAll (floor) | Orchestrator-LLM | Oracle (ceiling) |
|----------|------------------|------------------|------------------|
| Hivemind | A1               | **A2 (headline)**| A3               |
| mem0     | **B1 (baseline)**| B2               | B3               |

Primary contrast **A2 vs B1**. `AllowAll` approves everything (no curation). `Oracle` approves
only facts from gold-evidence sessions — a **labelled control / upper bound**, never a product
number. `Orchestrator-LLM` is the realistic gate: a Claude-subscription LLM judges keep/drop.

## LLM access — Claude subscription ONLY

Every model call (subagent extraction, the orchestrator gate, an optional judge) routes through one
adapter that shells `claude -p "<prompt>" --output-format json`. **No raw Anthropic/OpenAI API key,
no SDK.** mem0 runs fully offline (a local sentence-transformers embedder, a local Qdrant, an unused
keyless LLM slot, `infer=False`) and makes **zero API calls**. Preflight fails fast if the `claude`
CLI is absent or unauthenticated, or the dataset is missing — no silent defaults.

## Running it

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
zero-cost offline baseline (pair only with `allowall`/`oracle`).

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
no such gate, so this knob does not affect it.

### Offline smoke (no API, no download)

```bash
pytest tests/research/bench/test_e2e_smoke.py        # whole pipeline on the tiny fixture
pytest tests/research/bench/                          # the full bench suite
```

## Metrics

Gate aggressiveness is separated from retrieval quality:

- **hit@k** (any gold-source memory in the top-k) and **MRR** — over ALL answerable questions; an
  abstention scores 0, so a false abstention is counted as the real cost it is. hit@k is
  granularity-robust across backends (mem0 and Hivemind ingest at different units).
- **coverage** — answer-rate (1 − abstain-rate), so the abstention cost is visible.
- **recall@k (answered-only)** — retrieval quality *when the system commits to an answer*; secondary.
- **abstention AUROC** — each system's native confidence separating answerable from unanswerable
  (Hivemind: `1 − entropy_norm`; mem0: top score). Framed as native-separability, **not** a
  calibration claim — the proxies differ by backend, disclosed.
- **ship gate** — per-question paired bootstrap CI on `primary − baseline`; ships iff `lo > 0`,
  regresses iff `hi < 0`.

## Reproducibility & honesty contract

- The report is **provenance-stamped** (dataset hash, base-model name, extractor, LLM call-log
  digest, seeds, ks) and the generator **refuses** to emit a report with incomplete provenance.
- LLM calls are content-hash **cached** and **logged**; a re-run replays from the log — bit-for-bit
  at a fixed seed, no new calls.
- **Dev/test split** + frozen, un-tuned prompts: thresholds and prompts are never tuned on reported
  questions. The Oracle gate is a labelled control, never reported as a product number.

## Disclosed confounds (so the headline means what it claims)

- **Embedder geometry asymmetry** — both sides use the same base model (`BAAI/bge-small-en-v1.5`);
  Hivemind projects its native 384 dims through a frozen PCA head to 256, mem0 keeps native 384.
  The projection is Hivemind's own (lossy) store geometry — it counts for/against Hivemind, never a
  thumb on the scale.
- **Lost-evidence is a counted miss** — an answerable question whose evidence facts were never
  committed (the gate/extractor dropped them) is scored as a guaranteed miss, never silently
  skipped; otherwise an over-aggressive gate would be flattered by hiding what it curated away.
- **mem0 has no honest abstention** — it returns top-k whenever its store is non-empty, so its
  coverage is ~1.0 by construction; the abstention contrast is the point, not a defect to hide.
