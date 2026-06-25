"""S5 — the runner over the arms + the provenance-stamped report.

Composes S0–S4 into the single-system demonstration: for each arm it brings up a fresh store,
seeds the 3-class substrate servable, runs the fleet over the ordered task sequence (each seat
recalls / captures / reports outcomes), reviews + retires at checkpoints (per the arm's policy),
derives the store's-own-verdict diagnostics, and grades against the external gold. The headline is
Δ(maintenance-ON − OFF): per-task paired bootstrap CIs on success / density / bad-serve, point
deltas on the corpus-level prune recall + over-prune (false-prune) axes, and the ``clean_win``
conjunction.

Honesty rails carried verbatim in spirit from the comparative bench's report: the Pareto set is
never a scalar; a degenerate prune-everything arm is a labelled bound, never a product number; and
``build_selfmaint_report`` RAISES on incomplete provenance or a missing success vector — an
unreproducible number is worse than no number.

The arm policies (retire on/off, agi stamp, prune-everything, no-memory) are HARNESS SCHEDULE
choices, not daemon knobs — ON and OFF exercise the byte-identical shipped build. The agent turn,
the orchestrator LLM, the daemon, and the task-outcome gold are injection seams, so the whole
pipeline runs offline with fakes (the no-API smoke) and swaps in real ``claude -p`` + a warm
``hive up`` + executed tests for the staged real run.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Callable, Sequence

from hive.research.selfmaint import diagnostics
from hive.research.selfmaint.orchestrator import run_orchestrator_review
from hive.research.selfmaint.scoring import (
    bad_serve_rate, clean_win, density_curve, paired_delta_ci, prune_precision, prune_recall,
    task_success,
)
from hive.research.selfmaint.substrate import build_selfmaint_substrate, seed_store


@dataclass(frozen=True)
class Arm:
    """One arm's retirement policy — a harness SCHEDULE, never a daemon knob. ``retire`` gates the
    checkpoint review; ``agi_mode`` flips the orchestrator's approver stamp; ``prune_everything`` is
    the degenerate anti-gaming control (retire every servable seed — a labelled bound, never a
    product number); ``no_memory`` skips seeding (the §12 optional floor)."""
    name: str
    retire: bool = True
    agi_mode: bool = False
    prune_everything: bool = False
    no_memory: bool = False


@dataclass
class ArmClients:
    """The per-arm transport seam: a fresh store, a client per seat + the orchestrator, teardown."""
    seat_client: Callable[[str], object]
    orchestrator_client: object
    teardown: Callable[[], None]


# The default arm set: the headline ON↔OFF contrast, the secondary AGI arm, and the prune-everything
# anti-gaming control. ``no_memory`` is omitted by default (added only if a smoke shows OFF ≈ floor).
DEFAULT_ARMS = (
    Arm("maintenance_on", retire=True),
    Arm("maintenance_off", retire=False),
    Arm("agi_autonomous", retire=True, agi_mode=True),
    Arm("prune_everything", retire=True, prune_everything=True),
)


def label_outcome_gold(task, *, served_sources: set, retired_sources: set, servable: set) -> bool:
    """The deterministic substrate-label gold for the no-API tier: a task FAILS iff a labeled-bad
    fact it is falsified_by entered served context (acting on a wrong value), OR a keep-valuable
    fact it needs was wrongly retired (no longer servable). It reads the SUBSTRATE LABELS + the
    served/servable observations — never the store's deprecation verdict — so it stays gold-side.
    A real run replaces this with executed tests on the windowed repo."""
    poisoned = bool(task.falsified_by & served_sources)
    needed_lost = bool(task.needs_valuable - servable)
    return not poisoned and not needed_lost


def _prune_everything(client, servable_now: set, mapping: dict, approver: str) -> list[dict]:
    """The degenerate control: retire EVERY currently-servable seed (bad AND keep). Returns the raw
    result dicts for the diagnostic retired-set derivation. NOT the orchestrator — a labelled
    mechanical control, reported as a bound, never the headline (it visibly tanks precision)."""
    out = []
    for source_id in servable_now:
        eid = mapping.get(source_id)
        if eid is not None:
            out.append(client.prune(eid, approved_by=approver))
    return out


def _retire_result_dicts(obs) -> list[dict]:
    """Orchestrator retirements → the diagnostic result-dict shape (episode_id covers a prune; the
    same id as ``loser`` covers a supersede), so ``diagnostics.retired_sources`` reads both."""
    return [{"status": r.status, "episode_id": r.episode_id, "loser": r.episode_id}
            for r in obs.retired]


def _run_arm(arm: Arm, *, specs, tasks, valuable, keep, bad, task_gold, daemon_factory,
             agent_turn, llm_factory, outcome_fn, checkpoint_every, approver, seats) -> dict:
    clients: ArmClients = daemon_factory(arm)
    try:
        mapping = ({} if arm.no_memory
                   else seed_store(clients.orchestrator_client, specs, approver=approver))
        source_by_eid = {eid: src for src, eid in mapping.items()}
        seeded = set(mapping)
        retire_results: list[dict] = []
        served_by_task: list[set] = []
        servable_by_step: list[set] = []
        success_vec: list[int] = []
        bad_serve_vec: list[int] = []
        pending_hurts: list[int] = []
        for i, task in enumerate(tasks):
            served: set = set()
            for seat in seats:
                obs = agent_turn(seat, task, clients.seat_client(seat))
                served |= {source_by_eid[e] for e in obs.served_ids if e in source_by_eid}
                pending_hurts.extend(int(h) for h in obs.flagged_hurt)
            retired_now = diagnostics.retired_sources(retire_results, source_by_eid)
            servable_now = seeded - retired_now
            served_by_task.append(served)
            servable_by_step.append(servable_now)
            success_vec.append(1 if outcome_fn(task, served_sources=served,
                                               retired_sources=retired_now,
                                               servable=servable_now) else 0)
            bad_serve_vec.append(1 if (bad & served) else 0)
            if (i + 1) % checkpoint_every == 0 and arm.retire:
                if arm.prune_everything:
                    retire_results.extend(
                        _prune_everything(clients.orchestrator_client, servable_now, mapping, approver))
                else:
                    obs = run_orchestrator_review(
                        client=clients.orchestrator_client, llm=llm_factory(),
                        agi_mode=arm.agi_mode, approver=approver,
                        surfaced_hurts=tuple(pending_hurts))
                    retire_results.extend(_retire_result_dicts(obs))
                pending_hurts = []

        retired = diagnostics.retired_sources(retire_results, source_by_eid)
        density_vec = density_curve(servable_by_step, valuable)
        result = {
            "name": arm.name, "agi_mode": arm.agi_mode,
            "success": task_success(success_vec),
            "success_vec": success_vec,
            "density_vec": density_vec,
            "bad_serve_vec": bad_serve_vec,
            "bad_serve_rate": bad_serve_rate(served_by_task, bad) if bad else 0.0,
            "store_size_curve": diagnostics.store_size_curve(servable_by_step),
            "retired_sources": sorted(retired),
        }
        if bad and task_gold:                       # prune metrics need a confirmed-bad denominator
            result["prune_recall"] = prune_recall(retired, bad, task_gold=task_gold)
            precision, false_prune = prune_precision(retired, keep, task_gold=task_gold)
            result["prune_precision"] = precision
            result["false_prune_rate"] = false_prune
        return result
    finally:
        clients.teardown()


def _collapsed_ci(delta: float) -> tuple[float, float, float]:
    """A corpus-level scalar Δ (prune recall / over-prune) as a degenerate CI ``(Δ,Δ,Δ)`` — it has
    no per-task variance, so the ``verdict`` reads it as improve iff Δ>0 / regress iff Δ<0. The
    per-task axes (success / density / bad-serve) carry REAL bootstrap CIs."""
    return (delta, delta, delta)


def _value_map_hash(specs) -> str:
    canon = json.dumps(sorted((s.source_id, s.regime, s.true_value, s.false_value) for s in specs))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _repo_window_hash(repo_window) -> str:
    canon = json.dumps(sorted(
        (f.source_id, f.regime, f.true_value, f.carrier_text, f.kind) for f in repo_window.facts))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


REQUIRED_PROVENANCE = (
    "repo_window_hash", "seed", "k_passhat", "model", "llm_digest",
    "agi_mode_by_arm", "seed_value_map_hash")


def build_selfmaint_report(*, arms_results: Sequence[dict], deltas: dict, provenance: dict) -> dict:
    """Assemble the provenance-stamped report. RAISES on incomplete provenance (a missing required
    key) or any arm missing a success vector — an unreproducible number is refused, not masked
    (the build_poison_report pattern). The Pareto axes + the ``clean_win`` conjunction are reported
    per-axis; a net-negative / inconclusive regime is surfaced via the verdicts, never hidden."""
    missing = [k for k in REQUIRED_PROVENANCE if not provenance.get(k) and provenance.get(k) != 0]
    if missing:
        raise ValueError(f"selfmaint report refuses to emit on incomplete provenance: {missing}")
    for arm in arms_results:
        if not arm.get("success"):
            raise ValueError(f"arm {arm.get('name')!r} has no success vector — refusing to emit")
    return {"arms": list(arms_results), "deltas": deltas, "provenance": provenance}


def run_selfmaint(*, arms: Sequence[Arm], repo_window, seed: int, daemon_factory,
                  agent_turn, llm_factory, outcome_fn=label_outcome_gold,
                  model: str = "unknown", k_passhat: int = 1, llm_digest: str = "",
                  checkpoint_every: int = 1, approver: str = "orchestrator",
                  seats: Sequence[str] = ("seat-1", "seat-2")) -> dict:
    """Run every arm over the ordered sequence, score, and emit the provenance-stamped report. The
    headline Δ is ``maintenance_on − maintenance_off`` (both arms must be present for it). ``model``
    / ``llm_digest`` stamp the agent substrate (Claude subscription only — no raw API)."""
    specs, tasks = build_selfmaint_substrate(repo_window, seed=seed)
    valuable = {s.source_id for s in specs if s.regime == "keep_valuable"}
    keep = {s.source_id for s in specs if s.regime in ("keep_valuable", "keep_neutral")}
    bad = {s.source_id for s in specs if s.regime == "prunable_bad"}
    task_gold = frozenset().union(*[t.falsified_by for t in tasks]) if tasks else frozenset()

    results = {arm.name: _run_arm(
        arm, specs=specs, tasks=tasks, valuable=valuable, keep=keep, bad=bad, task_gold=task_gold,
        daemon_factory=daemon_factory, agent_turn=agent_turn, llm_factory=llm_factory,
        outcome_fn=outcome_fn, checkpoint_every=checkpoint_every, approver=approver, seats=seats)
        for arm in arms}

    deltas: dict = {}
    on, off = results.get("maintenance_on"), results.get("maintenance_off")
    if on and off:
        density_ci = paired_delta_ci(on["density_vec"], off["density_vec"], seed=seed)
        bad_serve_ci = paired_delta_ci(on["bad_serve_vec"], off["bad_serve_vec"], seed=seed)
        success_ci = paired_delta_ci(on["success_vec"], off["success_vec"], seed=seed)
        recall_delta = on.get("prune_recall", 0.0) - off.get("prune_recall", 0.0)
        # over-prune axis as higher-better (1 − false_prune) so OFF's zero retirements read 1.0,
        # not a vacuous precision; ON over-pruning a keep drives it below OFF → a real regression.
        precision_delta = ((1.0 - on.get("false_prune_rate", 0.0))
                           - (1.0 - off.get("false_prune_rate", 0.0)))
        verdict = clean_win(
            density_ci=density_ci, bad_serve_ci=bad_serve_ci,
            prune_recall_ci=_collapsed_ci(recall_delta),
            precision_ci=_collapsed_ci(precision_delta), success_ci=success_ci)
        deltas = {"density_ci": density_ci, "bad_serve_ci": bad_serve_ci,
                  "success_ci": success_ci, "prune_recall_delta": recall_delta,
                  "over_prune_delta": precision_delta, **verdict}

    provenance = {
        "repo_window_hash": _repo_window_hash(repo_window),
        "seed": seed, "k_passhat": k_passhat, "model": model, "llm_digest": llm_digest,
        "agi_mode_by_arm": {a.name: a.agi_mode for a in arms},
        "seed_value_map_hash": _value_map_hash(specs)}
    return build_selfmaint_report(arms_results=list(results.values()), deltas=deltas,
                                  provenance=provenance)
