"""Dev-time entropy-gate self-calibration (CV5) — the instrument that alone can
justify changing ``recall.H_frac_max`` / ``recall.softmax_beta``.

Labels are reconstructable from STORED DATA ONLY (no served-query vectors exist,
so "a later query succeeded" is unrecoverable): each stored miss VECTOR is
replayed against the servable rows **restricted to rows with ts < miss.ts** —
a strong top-1 (sim ≥ ``label_tau``) means the answer already existed when the
gate abstained ⇒ a false-abstain candidate; a weak field ⇒ a true abstain. A
miss with NO predating rows is skipped (the gate never had a field to judge).

Every sweep arm replays the SAME per-miss field through the real
``NormalizedEntropyGate``; per-query decision correctness (serve when a false
abstain, abstain when true) feeds the paired ``bootstrap_ci`` — **recommend a
change iff the best arm's CI over per-query correctness deltas vs the CURRENT
config has lo > 0** (the standing ship rule; a bare point gain never ships).
The config change itself stays operator-applied (both knobs are tier-B).

Dev-time only — the runtime never imports ``hive.research`` (P0.0 AST fence).
Degenerate label sets (all-hit / all-miss) RAISE, never mask (metrics_ir
convention): a calibration verdict on one-class data would be decorative.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from statistics import fmean
from typing import Optional, Sequence

import numpy as np

from hive.domain.lifecycle import _cosine
from hive.domain.recall import NormalizedEntropyGate
from hive.research.metrics_ir import abstention_auroc, bootstrap_ci, recall_at_k


@dataclass(frozen=True)
class LabeledMiss:
    """One replay-labeled miss: the query vector, its sims against the rows that
    PREDATED it (the field the gate judged), and the label. ``gold_id`` is the
    strongest predating row when false-abstain (what should have served)."""
    sims: tuple[float, ...]
    row_ids: tuple[int, ...]
    false_abstain: bool
    gold_id: Optional[int]


@dataclass(frozen=True)
class GateEvalSpec:
    """The frozen evaluation input. ``servable``: (episode_id, vector, ts) of
    today's servable rows; ``misses``: (vector, ts) of stored window misses;
    ``current``: the LIVE (h_frac_max, softmax_beta). ``extra_labeled`` appends
    pre-labeled cases (the existing labeled eval sets) after replay labeling."""
    servable: Sequence[tuple[int, "np.ndarray", int]]
    misses: Sequence[tuple["np.ndarray", int]]
    current: tuple[float, float]
    label_tau: float = 0.80
    k: int = 5
    extra_labeled: Sequence[LabeledMiss] = field(default_factory=tuple)


@dataclass(frozen=True)
class GateEvalResult:
    arms: dict                            # (h_frac_max, beta) → {auroc, false_abstain_rate, recall_at_k, correct_rate}
    best: tuple[float, float]
    best_vs_current_ci: tuple             # (point, lo, hi) — paired per-query correctness delta
    recommend: bool                       # ci.lo > 0, NEVER the point estimate


def label_misses(spec: GateEvalSpec) -> list[LabeledMiss]:
    """The replay labeler. For each stored miss: field = servable rows with
    ``row.ts < miss.ts`` (temporal credibility — a row written AFTER the miss
    cannot retro-label it); sims via true cosine (an undecidable row — NaN /
    zero-norm — never enters a field); empty field ⇒ the miss is skipped.
    false_abstain ⇔ top sim ≥ label_tau, gold = the argmax row.  // O(M·N·d)."""
    out: list[LabeledMiss] = []
    for vec, miss_ts in spec.misses:
        sims: list[float] = []
        ids: list[int] = []
        for eid, row_vec, row_ts in spec.servable:
            if int(row_ts) >= int(miss_ts):
                continue
            c = _cosine(vec, row_vec)
            if c is None:                 # undecidable ⇒ that row never labels
                continue
            sims.append(float(c))
            ids.append(int(eid))
        if not sims:
            continue                      # no field existed ⇒ the gate never ran
        top = max(range(len(sims)), key=lambda i: sims[i])
        fa = sims[top] >= float(spec.label_tau)
        out.append(LabeledMiss(sims=tuple(sims), row_ids=tuple(ids),
                               false_abstain=fa,
                               gold_id=ids[top] if fa else None))
    out.extend(spec.extra_labeled)
    return out


def run_gate_eval(spec: GateEvalSpec, *, sweep: Sequence[tuple[float, float]],
                  n_boot: int = 10_000, seed: int = 0) -> GateEvalResult:
    """Sweep (h_frac_max, beta) arms over the replay-labeled miss set. Per-arm:
    decision correct_rate, AUROC of the confidence proxy (1 − entropy_norm) at
    separating true abstains, false_abstain_rate (labeled-FA queries the arm
    still suppresses), recall@k of the gold row when serving. ``best`` is the
    highest correct_rate arm (sweep order breaks ties; current is always an
    arm); ``recommend`` rides the paired bootstrap CI alone. Deterministic for
    a fixed seed. Raises on an empty or one-class label set (refused, not
    masked).  // O(arms · M·N·d + n_boot·M)."""
    labeled = label_misses(spec)
    if not labeled:
        raise ValueError("gate_eval has no labelable misses (empty replay set)")
    arms_list = [tuple(map(float, a)) for a in sweep]
    current = tuple(map(float, spec.current))
    if current not in arms_list:
        arms_list.append(current)

    correctness: dict[tuple[float, float], list[int]] = {}
    arms_report: dict = {}
    for arm in arms_list:
        h, beta = arm
        gate = NormalizedEntropyGate(h, beta)
        correct: list[int] = []
        confidences: list[float] = []
        fa_total = fa_suppressed = 0
        fa_recalls: list[float] = []
        for case in labeled:
            suppress, entropy_norm, _margin = gate.evaluate(list(case.sims))
            confidences.append(1.0 - float(entropy_norm))
            correct.append(int(suppress == (not case.false_abstain)))
            if case.false_abstain:
                fa_total += 1
                if suppress:
                    fa_suppressed += 1
                    fa_recalls.append(0.0)   # abstained ⇒ the existing answer unserved
                else:
                    order = sorted(range(len(case.sims)),
                                   key=lambda i: -case.sims[i])
                    retrieved = [str(case.row_ids[i]) for i in order[:spec.k]]
                    fa_recalls.append(recall_at_k(retrieved,
                                                  {str(case.gold_id)}, spec.k))
        correctness[arm] = correct
        # one-class label sets raise HERE (abstention_auroc refuses degenerate)
        auroc = abstention_auroc(confidences,
                                 [not c.false_abstain for c in labeled])
        arms_report[arm] = {
            "auroc": auroc,
            "false_abstain_rate": (fa_suppressed / fa_total) if fa_total else 0.0,
            "recall_at_k": fmean(fa_recalls) if fa_recalls else 0.0,
            "correct_rate": fmean(correct)}

    # ties prefer the CURRENT config — equal evidence never names a change
    best = max(arms_list, key=lambda a: (arms_report[a]["correct_rate"],
                                         a == current))
    deltas = [b - c for b, c in zip(correctness[best], correctness[current])]
    ci = bootstrap_ci(deltas, n_boot=n_boot, seed=seed)
    return GateEvalResult(arms=arms_report, best=best, best_vs_current_ci=ci,
                          recommend=ci[1] > 0.0)


def spec_from_store(store, *, now: int, provisional_ttl_s: int,
                    current: tuple[float, float], window_s: int,
                    label_tau: float = 0.80, k: int = 5) -> GateEvalSpec:
    """Build a spec from a REAL store: today's servable rows (with their
    creation ts, for the temporal restriction) + the window's vector-bearing
    misses. Read-only; dev-time."""
    servable_ids = {eid for eid, _v in store.scan_servable(
        now=int(now), provisional_ttl_s=int(provisional_ttl_s))}
    servable = [(int(r["id"]), np.frombuffer(r["value"], dtype=np.float32).copy(),
                 int(r["ts"]))
                for r in store.conn.execute(
                    "SELECT id, value, ts FROM episodes WHERE value IS NOT NULL")
                if int(r["id"]) in servable_ids]
    misses = [(m.vector, int(m.ts))
              for m in store.misses_window(int(now) - int(window_s))]
    return GateEvalSpec(servable=servable, misses=misses,
                        current=(float(current[0]), float(current[1])),
                        label_tau=float(label_tau), k=int(k))
