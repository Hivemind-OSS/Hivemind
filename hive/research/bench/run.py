"""B6 — the benchmark runner: the per-case eval loop, preflight, provenance report, and CLI.

This chunk (B6a) is the analytical core, ``evaluate_arm`` — one arm (backend × gate) over the
dataset. For each question it builds a FRESH store (each LongMemEval question has its own haystack),
has subagents LLM-extract facts per session, drives the orchestrator-in-loop ingestion, asks the
question, and collects two scored streams:

  * retrieval — ANSWERABLE questions only, each paired with its gold-relevant memory ids;
  * abstention — EVERY question paired with ``is_unanswerable`` (both classes, for AUROC).

The fidelity rule that keeps the headline honest: an answerable question whose evidence facts were
never committed (the gate or extractor dropped them) has EMPTY gold. That is a real retrieval
failure, so it is scored as a guaranteed MISS via an unretrievable sentinel id — never silently
dropped the way a genuinely unanswerable question is. Skipping it would flatter an over-aggressive
gate by hiding the questions it curated away.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

from hive.research.bench.backends import MemoryBackend, RecallObs
from hive.research.bench.dataset import Case, gold_relevant, load_longmemeval
from hive.research.bench.llm import LLM, zero_served_usage, zero_usage
from hive.research.bench.orchestrator import (
    AllowAllGate, GatePolicy, LLMOrchestratorGate, OracleEvidenceGate,
    extract_facts, run_ingestion, run_queries,
)
from hive.research.bench.scoring import paired_delta_ci, score_abstention, score_retrieval

EMBEDDER_MODEL = "Qwen/Qwen3-Embedding-0.6B"  # the base model pinned for BOTH backends (isolated)
_SEATS = ("sub-a", "sub-b", "sub-c", "sub-d")  # the N≥4 subagent fleet
_KS = (5, 10)


@dataclass(frozen=True)
class ArmObservations:
    """One arm's raw observations, ready for ``scoring``. ``retrieval`` is answerable-only with
    non-empty gold (a sentinel stands in for lost evidence ⇒ a counted miss); ``abstention`` is
    every question with its unanswerable label."""
    retrieval: list[tuple[RecallObs, set[str]]]
    abstention: list[tuple[RecallObs, bool]]


def evaluate_arm(backend: MemoryBackend, gate: GatePolicy, cases: Sequence[Case], *,
                 llm: LLM, seats: Sequence[str], reader_seat: str = "reader",
                 max_facts: int = 8) -> ArmObservations:
    """Run one (backend, gate) arm over ``cases`` — a fresh store per question."""
    retrieval: list[tuple[RecallObs, set[str]]] = []
    abstention: list[tuple[RecallObs, bool]] = []
    for case in cases:
        backend.reset()
        sessions_facts = [(s.session_id, extract_facts(llm, s, max_facts=max_facts))
                          for s in case.sessions]
        trace = run_ingestion(backend, gate, sessions_facts, seats)
        obs = run_queries(backend, [case.question.text], seat=reader_seat)[0]
        abstention.append((obs, case.question.is_unanswerable))
        if not case.question.is_unanswerable:
            gold = gold_relevant(case, trace.mem_source)
            if not gold:
                # evidence was proposed but never committed ⇒ unretrievable ⇒ a counted miss,
                # NOT a skip (skipping would hide the questions an aggressive gate curated away).
                gold = {f"__unretrievable__:{case.question.question_id}"}
            retrieval.append((obs, gold))
    return ArmObservations(retrieval=retrieval, abstention=abstention)


# ── offline deterministic extractor (no-API smoke + a baseline ingestion) ──────

class VerbatimLLM:
    """An offline, deterministic stand-in for the extractor: it echoes each user turn from the
    prompt as a fact. Lets the whole harness run with ZERO subscription cost (pair only with the
    allow-all / oracle gates — it is an extractor, not a judge), and serves as a deterministic
    ingestion baseline against the LLM-extracted arm."""

    def complete(self, prompt: str, *, system: Optional[str] = None) -> str:
        return "\n".join(line[len("user: "):] for line in prompt.splitlines()
                         if line.startswith("user: "))

    def digest(self) -> str:
        return "verbatim"

    def usage_totals(self) -> dict:
        return zero_usage()                          # offline, costless — the agent-loop plumbing

    def served_usage_totals(self) -> dict:
        return zero_served_usage()                   # offline, costless


# ── scoring one arm + comparing two ────────────────────────────────────────────

def score_arm(backend: MemoryBackend, gate: GatePolicy, cases: Sequence[Case], *,
              llm: LLM, seats: Sequence[str], ks: tuple[int, ...] = _KS) -> dict:
    """Evaluate one arm and reduce to a JSON-ready score dict (means + per-question vectors for the
    paired CI). ``abstention_auroc`` is None when the split has only one class — undefined, not
    masked."""
    arm = evaluate_arm(backend, gate, cases, llm=llm, seats=seats)
    rs = score_retrieval(arm.retrieval, ks=ks)
    try:
        auroc: Optional[float] = score_abstention(arm.abstention)
    except ValueError:
        auroc = None
    return {
        "retrieval": {
            "n": rs.n, "n_answered": rs.n_answered, "coverage": rs.coverage,
            "hit_at_k": {str(k): rs.hit_at_k[k] for k in rs.ks}, "mrr": rs.mrr,
            "recall_at_k_answered": {str(k): rs.recall_at_k_answered[k] for k in rs.ks},
        },
        "abstention_auroc": auroc,
        "hit_at_k_per_q": {str(k): rs.hit_at_k_per_q[k] for k in rs.ks},
        "mrr_per_q": rs.mrr_per_q,
    }


def compare_arms(primary: dict, baseline: dict, *, ks: tuple[int, ...] = _KS,
                 seed: int = 0) -> list[dict]:
    """Per-question paired bootstrap CIs on (primary − baseline). An improvement SHIPS iff the CI
    excludes 0 from below (lo > 0); it regresses iff hi < 0. Same question order in both arms by
    construction (answerable set is gate-independent)."""
    out: list[dict] = []
    for k in ks:
        point, lo, hi = paired_delta_ci(primary["hit_at_k_per_q"][str(k)],
                                        baseline["hit_at_k_per_q"][str(k)], seed=seed)
        out.append({"metric": f"hit_at_{k}", "point": point, "lo": lo, "hi": hi,
                    "ships": lo > 0.0, "regresses": hi < 0.0})
    point, lo, hi = paired_delta_ci(primary["mrr_per_q"], baseline["mrr_per_q"], seed=seed)
    out.append({"metric": "mrr", "point": point, "lo": lo, "hi": hi,
                "ships": lo > 0.0, "regresses": hi < 0.0})
    return out


# ── preflight + provenance-stamped report ──────────────────────────────────────

def preflight(*, dataset_path: str, llm) -> None:
    """Fail fast BEFORE any expensive work: the dataset must exist and (for the real CLI) the
    Claude subscription must be authenticated. No silent defaults."""
    if not Path(dataset_path).is_file():
        raise FileNotFoundError(
            f"LongMemEval dataset not found at {dataset_path!r} — set HIVE_BENCH_LME_PATH")
    if hasattr(llm, "preflight"):
        llm.preflight()


_REQUIRED_PROVENANCE = (
    "dataset_hash", "n_cases", "seeds", "embedder_model", "extractor", "llm_digest", "ks",
    "recall_hfrac")


def build_report(*, arms: dict, comparisons: list, provenance: dict) -> dict:
    """Assemble the report, REFUSING to emit one whose provenance is incomplete (an unreproducible
    number is worse than no number)."""
    missing = [k for k in _REQUIRED_PROVENANCE if provenance.get(k) in (None, "")]
    if missing:
        raise ValueError(f"refusing to emit a report missing provenance: {missing}")
    return {"provenance": dict(provenance), "arms": arms, "comparisons": list(comparisons)}


def _public_arm(sc: dict) -> dict:
    return {"retrieval": sc["retrieval"], "abstention_auroc": sc["abstention_auroc"]}


def _file_sha256(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ── backend / gate / llm builders (real defaults; injectable for tests) ────────

def _build_llm(extractor: str):
    if extractor == "verbatim":
        return VerbatimLLM()
    if extractor == "claude":
        from hive.research.bench.llm import ClaudeSubscriptionLLM
        return ClaudeSubscriptionLLM(log_path=os.environ.get("HIVE_BENCH_LLM_LOG"),
                                     model=os.environ.get("HIVE_BENCH_MODEL"))
    raise ValueError(f"unknown extractor {extractor!r} (use 'claude' or 'verbatim')")


def _hivemind_server_factory(h_frac_max: float = 0.9, *, autonomy=None,
                             embedder=None) -> Callable[[], object]:
    """A factory booting a real Qwen3-backed in-process server. The embedder is warmed ONCE and
    shared across every per-case rebuild (so Qwen3 loads a single time, not once per question);
    an ephemeral ``:memory:`` store guarantees the bench never touches a persistent operator DB.

    ``h_frac_max`` is the recall entropy-gate threshold: the production default (0.5) abstains on
    LongMemEval's multi-evidence/aggregation questions (the relevant facts spread the probability
    mass ⇒ high entropy), collapsing coverage to ~0. It is exposed so it can be calibrated on a dev
    slice and stamped into the report — never silently tuned to a flattering value.

    ``autonomy`` defaults to ``{"demand_m": 10**9}`` — the orchestrator commit is the SOLE promotion
    authority (demand never fires) for the headline arms; pass an ``AutonomyConfig`` or a dict to
    tune the demand knobs for the anti-gaming probe. ``embedder`` defaults to a freshly warmed Qwen3;
    pass a pre-built embedder (e.g. a scripted-geometry provider) to skip the registry build."""
    from hive.app.config import AutonomyConfig, Config
    from hive.app.container import build_container
    if autonomy is None:
        autonomy = {"demand_m": 10**9}
    elif isinstance(autonomy, AutonomyConfig):
        autonomy = {f.name: getattr(autonomy, f.name)
                    for f in dataclasses.fields(AutonomyConfig)}
    # a passed embedder fixes the store geometry to ITS projection dim (the index/store dim must
    # agree with the embedder); the default Qwen3 path keeps geometry.d at its config default.
    overrides = {"autonomy": autonomy, "recall": {"H_frac_max": h_frac_max}}
    if embedder is not None and hasattr(embedder, "d"):
        overrides["geometry"] = {"d": int(embedder.d)}
    cfg = Config.load(db_path=":memory:", **overrides)
    if embedder is None:
        from hive.app import registry
        embedder = registry.build_embedder(cfg)
        embedder.load()                                # warm Qwen3 + verify native dim ONCE

    def factory():
        c = build_container(cfg, tenant_id="default", agent_id="bench-orchestrator",
                            embedder=embedder)
        c.migrate()
        c.build_index()
        c.warm_embedder()                              # idempotent on the shared warmed embedder
        return c.make_server()
    return factory


def _build_backend(name: str, *, recall_hfrac: float = 0.9) -> MemoryBackend:
    if name == "mem0":
        from hive.research.bench.mem0_backend import Mem0Backend, RealMem0Client
        return Mem0Backend(RealMem0Client(model=EMBEDDER_MODEL, dims=1024))
    if name == "hivemind":
        from hive.research.bench.hivemind_backend import HivemindBackend
        return HivemindBackend(_hivemind_server_factory(recall_hfrac))
    raise ValueError(f"unknown backend {name!r} (use 'hivemind' or 'mem0')")


def _build_gate(name: str, cases: Sequence[Case], llm) -> GatePolicy:
    if name == "allowall":
        return AllowAllGate()
    if name == "oracle":
        evidence: set[str] = set()
        for c in cases:                     # union is safe: LongMemEval session ids are unique per Q
            evidence |= set(c.question.evidence_session_ids)
        return OracleEvidenceGate(evidence)
    if name == "llm":
        return LLMOrchestratorGate(llm)
    raise ValueError(f"unknown gate {name!r} (use 'allowall', 'oracle', or 'llm')")


def _parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="hive-bench",
                                description="Orchestrator-in-loop LongMemEval head-to-head.")
    p.add_argument("--backend", default="hivemind")
    p.add_argument("--gate", default="llm")
    p.add_argument("--baseline-backend", dest="baseline_backend", default="mem0")
    p.add_argument("--baseline-gate", dest="baseline_gate", default="allowall")
    p.add_argument("--extractor", default="claude", choices=["claude", "verbatim"])
    p.add_argument("--recall-hfrac", dest="recall_hfrac", type=float, default=0.9,
                   help="Hivemind recall entropy-gate threshold H_frac_max (prod default 0.5 "
                        "abstains on multi-evidence queries; calibrate on a dev slice)")
    p.add_argument("--dataset", default=os.environ.get("HIVE_BENCH_LME_PATH"))
    p.add_argument("--n", type=int, default=None)
    p.add_argument("--seeds", default="0")
    p.add_argument("--out", required=True)
    a = p.parse_args(argv)
    if not a.dataset:
        p.error("no dataset: pass --dataset or set HIVE_BENCH_LME_PATH")
    a.seeds = [int(s) for s in str(a.seeds).split(",") if s != ""]
    return a


def main(argv: Optional[Sequence[str]] = None, *,
         backend_factory: Optional[Callable[[str], MemoryBackend]] = None,
         llm_factory: Optional[Callable[[str], LLM]] = None) -> int:
    """Run the primary arm vs the baseline arm over one dataset slice and write a provenance-stamped
    JSON report. ``backend_factory`` / ``llm_factory`` are injection seams (tests run fully offline);
    the defaults build the real Qwen3 backends and the subscription LLM."""
    cfg = _parse_args(argv)
    llm = (llm_factory or _build_llm)(cfg.extractor)
    preflight(dataset_path=cfg.dataset, llm=llm)     # verbatim has no preflight() ⇒ dataset-only
    cases = load_longmemeval(cfg.dataset, n=cfg.n, seed=cfg.seeds[0])
    make_backend = backend_factory or (lambda name: _build_backend(name, recall_hfrac=cfg.recall_hfrac))

    primary = score_arm(make_backend(cfg.backend), _build_gate(cfg.gate, cases, llm),
                        cases, llm=llm, seats=list(_SEATS), ks=_KS)
    baseline = score_arm(make_backend(cfg.baseline_backend), _build_gate(cfg.baseline_gate, cases, llm),
                         cases, llm=llm, seats=list(_SEATS), ks=_KS)
    comparisons = compare_arms(primary, baseline, ks=_KS, seed=cfg.seeds[0])

    arms = {f"{cfg.backend}/{cfg.gate}": _public_arm(primary),
            f"{cfg.baseline_backend}/{cfg.baseline_gate}": _public_arm(baseline)}
    provenance = {
        "dataset_hash": _file_sha256(cfg.dataset), "n_cases": len(cases), "seeds": cfg.seeds,
        "embedder_model": EMBEDDER_MODEL, "extractor": cfg.extractor,
        "llm_digest": getattr(llm, "digest", lambda: "n/a")(), "ks": list(_KS),
        "recall_hfrac": cfg.recall_hfrac,
        "primary": f"{cfg.backend}/{cfg.gate}", "baseline": f"{cfg.baseline_backend}/{cfg.baseline_gate}",
    }
    report = build_report(arms=arms, comparisons=comparisons, provenance=provenance)
    Path(cfg.out).write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return 0


if __name__ == "__main__":                          # pragma: no cover
    raise SystemExit(main())
