"""C4 — the three-arm abstain-token runner: preflight → substrate → preload-per-arm → agent loop →
score → provenance-stamped report. A SIBLING entrypoint to ``run.py`` (which scores retrieval quality
in a different unit) — keeping each benchmark single-purpose rather than bolting a second metric onto
one runner.

The three arms isolate the gate, not just A-vs-C:
  * A ``mem0``        — top-k always (the competitor).
  * B ``abstain-off`` — Hivemind at the max legal threshold ``H_frac_max=1.0`` (a clamped
                        ``entropy_norm`` can never exceed it ⇒ no suppression); SAME store/embedder/
                        geometry as C. A config of the shipped gate — no kernel change.
  * C ``abstain-on``  — the production gate (``H_frac_max=0.5``).
Deltas, each a task-paired bootstrap CI: (C−B) is the gate's PURE contribution, (B−A) the
store difference (mem0 and Hivemind now share the native 1024 geometry), (C−A) the total. Tokens are a Pareto co-axis — reported ALONGSIDE
success (and per-regime), never folded into a scalar; a token saving counts only at
equal-or-better success.

Dev-time only (under ``hive.research``). Every arm gets its OWN LLM instance so one arm's content-
hash cache can never zero another arm's genuine token cost (honest per-arm attribution). Tests run
fully offline via the ``backend_factory`` / ``llm_factory`` injection seams.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Callable, Optional, Sequence

from hive.research.bench.backends import MemoryBackend
from hive.research.bench.dataset import load_longmemeval
from hive.research.bench.llm import LLM
from hive.research.bench.run import (
    EMBEDDER_DIMS, EMBEDDER_MODEL, _file_sha256, preflight as run_preflight)
from hive.research.bench.scoring import paired_delta_ci
from hive.research.bench.substrate import assert_model_parity, build_substrate, preload
from hive.research.bench.task_agent import (
    SUCCESS_VERSION, TASK_PROMPT_VERSION, ArmTokenObs, run_arm, tokenizer_name,
)

ARM_MEM0 = "mem0"
ARM_OFF = "abstain-off"
ARM_ON = "abstain-on"
_ARMS = (ARM_MEM0, ARM_OFF, ARM_ON)               # A, B, C
_REGIMES = ("single-answer", "broad-relevant", "no-relevant")

_READER_SEAT = "reader"
_APPROVER = "orchestrator"
_TOP_K = 10                                        # == RecallConfig.recall_top_n default (equal footing)
_EXTRACTOR = "substrate-raw-turns"                 # memories are raw haystack turns, no LLM distillation


# ── scoring: per-arm/per-regime summaries + the three paired deltas ────────────

def _arm_summary(obs: ArmTokenObs) -> dict:
    """Per-arm token + success aggregate WITH a per-regime breakdown (AC10), so a win driven only by
    the no-relevant slice — or a loss driven by broad-relevant over-abstention — is visible, never
    hidden in the aggregate."""
    tasks = obs.tasks
    by_regime: dict[str, dict] = {}
    for r in _REGIMES:
        rt = [t for t in tasks if t.regime == r]
        by_regime[r] = {
            "n": len(rt),
            "served_tokens": sum(t.served_tokens for t in rt),
            "served_text_count": sum(t.served_text_count for t in rt),
            "served_gold": sum(1 for t in rt if t.served_gold),
            "success_rate": (sum(1 for t in rt if t.success) / len(rt)) if rt else None,
            "abstained_on_answerable": sum(1 for t in rt if t.abstained_on_answerable),
        }
    return {
        "n": len(tasks),
        "served_tokens_total": sum(t.served_tokens for t in tasks),
        "served_text_count": sum(t.served_text_count for t in tasks),
        "served_gold": sum(1 for t in tasks if t.served_gold),
        "success_rate": (sum(1 for t in tasks if t.success) / len(tasks)) if tasks else None,
        "abstained_on_answerable": sum(1 for t in tasks if t.abstained_on_answerable),
        # the ACTUAL claude usage — a labelled, cache-order-confounded cost DIAGNOSTIC, NOT the
        # headline token axis (BUG-004: input_tokens is defeated by prompt caching + base prompt).
        "actual_claude_usage": dict(obs.usage_total),
        "by_regime": by_regime,
    }


def _delta(x: ArmTokenObs, y: ArmTokenObs, *, seed: int) -> dict:
    """Task-paired bootstrap CI of (x − y) on BOTH axes. Tokens: lower is better ⇒ a saving iff the
    CI lies below 0 (``hi < 0``). Success: a regression iff the CI lies below 0 (``hi < 0``), an
    improvement iff above (``lo > 0``)."""
    tp, tlo, thi = paired_delta_ci([t.served_tokens for t in x.tasks],
                                   [t.served_tokens for t in y.tasks], seed=seed)
    sp, slo, shi = paired_delta_ci([1.0 if t.success else 0.0 for t in x.tasks],
                                   [1.0 if t.success else 0.0 for t in y.tasks], seed=seed)
    return {
        "tokens": {"point": tp, "lo": tlo, "hi": thi,
                   "saves": thi < 0.0, "costs_more": tlo > 0.0},
        "success": {"point": sp, "lo": slo, "hi": shi,
                    "improves": slo > 0.0, "regresses": shi < 0.0},
    }


def score_token_arms(arm_obs: dict[str, ArmTokenObs], *, seed: int = 0) -> dict:
    """Reduce the three arms to per-arm/per-regime summaries + the three task-paired deltas. The
    arms MUST be task-aligned (same regime order) — a misaligned pairing would make the CI
    meaningless, so it RAISES rather than silently comparing unrelated tasks."""
    for a in _ARMS:
        if a not in arm_obs:
            raise ValueError(f"score_token_arms requires the {a!r} arm")
    a, b, c = arm_obs[ARM_MEM0], arm_obs[ARM_OFF], arm_obs[ARM_ON]
    regimes = [t.regime for t in a.tasks]
    for other in (b, c):
        if [t.regime for t in other.tasks] != regimes:
            raise ValueError("arms are not task-aligned (a paired CI requires identical task order)")
    deltas = {"C-B": _delta(c, b, seed=seed),       # the abstain gate's PURE contribution
              "B-A": _delta(b, a, seed=seed),       # store difference (shared native geometry)
              "C-A": _delta(c, a, seed=seed)}       # total
    arms = {name: _arm_summary(obs) for name, obs in arm_obs.items()}
    token_totals = {name: arms[name]["served_tokens_total"] for name in arm_obs}
    return {"arms": arms, "deltas": deltas, "token_totals": token_totals}


# ── provenance-stamped report (refuse on incomplete) ───────────────────────────

_REQUIRED_PROVENANCE = (
    "token_totals", "served_tokenizer", "h_frac_on", "h_frac_off", "dataset_hash",
    "embedder_model", "extractor", "llm_digest", "seeds", "regime_mix")


def build_token_report(*, arms: dict, deltas: dict, provenance: dict) -> dict:
    """Assemble the report, REFUSING to emit one missing token totals, the per-arm success vector,
    or any required provenance (an unreproducible number is worse than no number — mirrors
    ``run.py:build_report``)."""
    missing = [k for k in _REQUIRED_PROVENANCE if provenance.get(k) in (None, "")]
    if missing:
        raise ValueError(f"refusing to emit a token report missing provenance: {missing}")
    if not arms:
        raise ValueError("refusing to emit a token report with no arms")
    for name, a in arms.items():
        if a.get("success_rate") is None:    # missing key OR a degenerate empty (zero-task) arm
            raise ValueError(f"refusing to emit: arm {name!r} is missing its success vector")
    return {"provenance": dict(provenance), "arms": arms, "deltas": deltas}


# ── default (real-run) factories — injectable for offline tests ────────────────

def _default_backend_factory(cfg) -> Callable[[str], MemoryBackend]:
    def make(arm: str) -> MemoryBackend:
        if arm == ARM_MEM0:
            from hive.research.bench.mem0_backend import Mem0Backend, RealMem0Client
            return Mem0Backend(RealMem0Client(model=EMBEDDER_MODEL, dims=EMBEDDER_DIMS), top_k=_TOP_K)
        from hive.research.bench.hivemind_backend import HivemindBackend
        from hive.research.bench.run import _hivemind_server_factory
        h = cfg.h_frac_off if arm == ARM_OFF else cfg.h_frac_on   # B vs C — the ONLY difference
        return HivemindBackend(_hivemind_server_factory(h))
    return make


def _default_llm_factory(cfg) -> Callable[[str], LLM]:
    from hive.research.bench.llm import ClaudeSubscriptionLLM
    def make(arm: str) -> LLM:
        # a SEPARATE log/cache per arm so no arm's cache zeroes another's genuine spend; same agent
        # model across arms (equal footing). The preflight probe shares the mem0 arm's instance —
        # its spend is excluded from that arm's total by run_arm's post-preflight `before` snapshot.
        log = f"{cfg.llm_log}.{arm}.jsonl" if cfg.llm_log else None
        return ClaudeSubscriptionLLM(log_path=log, model=cfg.model)
    return make


def _combined_digest(llms: dict[str, LLM]) -> str:
    """A digest over every arm's call-log digest — a tampered usage in ANY arm changes provenance."""
    import hashlib
    parts = sorted(f"{arm}:{getattr(llm, 'digest', lambda: 'n/a')()}" for arm, llm in llms.items())
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


# ── CLI ────────────────────────────────────────────────────────────────────────

def _parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="hive-token-bench",
        description="Abstain-gate read-side token savings — agent loop vs mem0 (three arms).")
    p.add_argument("--dataset", default=os.environ.get("HIVE_BENCH_LME_PATH"))
    p.add_argument("--n", type=int, default=None, help="seeded subsample (default all)")
    p.add_argument("--seeds", default="0")
    p.add_argument("--h-frac-on", dest="h_frac_on", type=float, default=0.5,
                   help="abstain-ON threshold (production default 0.5)")
    p.add_argument("--h-frac-off", dest="h_frac_off", type=float, default=1.0,
                   help="abstain-OFF threshold (1.0 = max legal ⇒ no entropy suppression)")
    p.add_argument("--model", default=os.environ.get("HIVE_BENCH_MODEL"),
                   help="the agent model id (Haiku tier); SAME across arms")
    p.add_argument("--llm-log", dest="llm_log", default=os.environ.get("HIVE_BENCH_LLM_LOG"),
                   help="base path for the per-arm JSONL replay logs")
    p.add_argument("--out", required=True)
    a = p.parse_args(argv)
    if not a.dataset:
        p.error("no dataset: pass --dataset or set HIVE_BENCH_LME_PATH")
    a.seeds = [int(s) for s in str(a.seeds).split(",") if s != ""]
    return a


def main(argv: Optional[Sequence[str]] = None, *,
         backend_factory: Optional[Callable[[str], MemoryBackend]] = None,
         llm_factory: Optional[Callable[[str], LLM]] = None) -> int:
    """Run the three arms over one dataset slice (fixed thresholds 0.5 / 1.0, both stamped) and write
    a provenance-stamped JSON token report. Factories are injection seams (tests run fully offline);
    the defaults build the real Qwen3 backends + the subscription LLM."""
    cfg = _parse_args(argv)
    seed = cfg.seeds[0]
    make_backend = backend_factory or _default_backend_factory(cfg)
    make_llm = llm_factory or _default_llm_factory(cfg)

    llms = {arm: make_llm(arm) for arm in _ARMS}
    # preflight: dataset present + (real CLI) authenticated; equal-footing model parity (fail fast).
    run_preflight(dataset_path=cfg.dataset, llm=llms[ARM_MEM0])
    from hive.app.config import Config
    assert_model_parity(Config.load(db_path=":memory:", recall={"H_frac_max": cfg.h_frac_on}))

    # Fixed thresholds need no held-out split (no tuning ⇒ no leakage); the dev/test split
    # (dataset.dev_test_split) is the seam for a future dev-tuned ON value, reported on test only.
    cases = load_longmemeval(cfg.dataset, n=cfg.n, seed=seed)
    memories, task_cases = build_substrate(cases, seed=seed)

    arm_obs: dict[str, ArmTokenObs] = {}
    for arm in _ARMS:
        backend = make_backend(arm)
        backend.reset()
        pre = preload(backend, memories, approver=_APPROVER)
        arm_obs[arm] = run_arm(backend, llms[arm], task_cases, seat=_READER_SEAT,
                               served_text=pre.mem_text, mem_source=pre.mem_source, arm=arm)

    scored = score_token_arms(arm_obs, seed=seed)
    provenance = {
        "token_totals": scored["token_totals"],
        "h_frac_on": cfg.h_frac_on, "h_frac_off": cfg.h_frac_off,
        "dataset_hash": _file_sha256(cfg.dataset), "n_cases": len(cases),
        "embedder_model": EMBEDDER_MODEL, "extractor": _EXTRACTOR,
        "llm_digest": _combined_digest(llms), "seeds": cfg.seeds,
        "regime_mix": {r: sum(1 for c in task_cases if c.regime == r) for r in _REGIMES},
        "served_tokenizer": tokenizer_name(),
        "task_prompt_version": TASK_PROMPT_VERSION, "success_version": SUCCESS_VERSION,
        "agent_model": cfg.model or "default", "top_k": _TOP_K, "arms": list(_ARMS),
    }
    report = build_token_report(arms=scored["arms"], deltas=scored["deltas"], provenance=provenance)
    Path(cfg.out).write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return 0


if __name__ == "__main__":                          # pragma: no cover
    raise SystemExit(main())
