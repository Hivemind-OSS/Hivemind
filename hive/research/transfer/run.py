"""The two-phase runner over the arms + the provenance-stamped report (Chunk 5).

For each arm it brings up a fresh daemon, runs the fleet over the transfer pairs in two phases —
Phase A (each pair's WRITER earns its fact by passing its own visible test and captures it), a
PROMOTION step (orchestrator vouch, or autonomous cross-identity demand), then Phase B (each pair's
READER recalls and applies it) — grades each downstream task against the external gold, and derives
the headline Δ(recall_on − recall_off) over the pairs the necessity gate certifies valid.

Arm policies are HARNESS SCHEDULE choices, not daemon knobs. recall_off vouches the fact servable
EXACTLY like recall_on but withholds the recall tool, so the only cross-arm difference on the
headline contrast is the recall STEP itself. The daemon, the agent turn, the vouch, and the
task-outcome gold are injection seams, so the whole pipeline runs offline with fakes and swaps in
real claude -p + a warm daemon + executed hidden tests for the staged real run.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

from hive.research.transfer import diagnostics, scoring
from hive.research.transfer.substrate import TransferPair, TransferWindow


@dataclass(frozen=True)
class Arm:
    """One arm's schedule. ``recall`` gates whether the downstream reader may call hive_recall (the
    headline lever — withheld for recall_off / oracle). ``oracle`` injects the true value into the
    reader's prompt (the labelled ceiling). ``promotion`` is how the captured fact becomes servable:
    ``"vouch"`` (orchestrator establishes it — reliable, the primary), ``"demand"`` (autonomous
    cross-identity recall-miss demand over a ``demand_fanout`` of readers), or ``"none"``."""

    name: str
    recall: bool = True
    oracle: bool = False
    promotion: str = "vouch"
    demand_fanout: int = 0


@dataclass(frozen=True)
class TransferTask:
    """One agent turn's task: which pair, which role, and the arm-derived levers the glue reads to
    build the prompt + tool scope (recall on/off, oracle value)."""

    pair: TransferPair
    role: str
    can_recall: bool = True
    oracle_value: Optional[str] = None


@dataclass(frozen=True)
class TransferTurnObs:
    """One turn's observation. Upstream populates ``captured_texts``; a downstream reader populates
    ``served_values`` / ``served_texts`` (what its recall surfaced, or the injected oracle value)."""

    seat: str
    captured_texts: tuple = ()
    served_values: tuple = ()
    served_texts: tuple = ()
    result_text: str = ""


@dataclass
class ArmClients:
    """The per-arm transport seam: a client per seat + the orchestrator, and teardown."""

    seat_client: Callable[[str], object]
    orchestrator_client: object
    teardown: Callable[[], None]


DEFAULT_ARMS = (
    Arm("recall_on", recall=True, promotion="vouch"),
    Arm("recall_off", recall=False, promotion="vouch"),
    Arm("oracle_ceiling", recall=False, oracle=True, promotion="vouch"),
    Arm("demand_promote", recall=True, promotion="demand", demand_fanout=4),
)


def label_transfer_gold(pair: TransferPair, *, served_values, arm=None) -> bool:
    """The deterministic offline gold: a downstream task succeeds iff the correct value reached the
    reader (recalled and served, or oracle-injected — both land in ``served_values``). The no-recall
    arm serves nothing and fails. The real run replaces this with the executed HIDDEN acceptance test
    on the reader's build dir. ``arm`` is accepted (the runner threads it) but ignored here."""
    return pair.fact.value in set(served_values)


def _run_arm(arm: Arm, *, window: TransferWindow, daemon_factory, agent_turn, vouch_fn,
             outcome_fn) -> dict:
    clients: ArmClients = daemon_factory(arm)
    try:
        captured: dict[str, list] = {}
        result_texts: list[str] = []
        for pair in window.pairs:
            wseat = f"writer-{pair.pair_id}"
            obs = agent_turn(wseat, TransferTask(pair, "upstream", can_recall=False),
                             clients.seat_client(wseat))
            captured[pair.pair_id] = list(obs.captured_texts)
            result_texts.append(obs.result_text)

        servable_texts: dict[str, list] = {}
        if arm.promotion == "vouch":
            vouched = vouch_fn(clients.orchestrator_client, captured) or {}
            servable_texts = {pid: ([t] if isinstance(t, str) else list(t))
                              for pid, t in vouched.items()}

        success_vec: list[int] = []
        served_texts_by_pair: dict[str, list] = {}
        demanders_failed = 0
        for pair in window.pairs:
            n_readers = (arm.demand_fanout if arm.promotion == "demand" and arm.demand_fanout > 0
                         else 1)
            last_ok = False
            last_obs = TransferTurnObs(seat="")
            for j in range(n_readers):
                rseat = f"reader-{pair.pair_id}-{j}"
                task = TransferTask(pair, "downstream", can_recall=arm.recall,
                                    oracle_value=(pair.fact.value if arm.oracle else None))
                obs = agent_turn(rseat, task, clients.seat_client(rseat))
                ok = bool(outcome_fn(pair, arm=arm, served_values=set(obs.served_values)))
                if j < n_readers - 1 and not ok:      # an early reader paid the demand miss
                    demanders_failed += 1
                last_ok, last_obs = ok, obs
                result_texts.append(obs.result_text)
            success_vec.append(1 if last_ok else 0)
            served_texts_by_pair[pair.pair_id] = list(last_obs.served_texts)

        # demand promotion leaves no vouch record; a served text implies it was servable
        if arm.promotion != "vouch":
            servable_texts = dict(served_texts_by_pair)

        return {
            "name": arm.name,
            "success_vec": success_vec,
            "success": scoring.transfer_success(success_vec),
            "demanders_failed": demanders_failed,
            "_captured": captured,
            "_servable_texts": servable_texts,
            "_served_texts": served_texts_by_pair,
            "_result_texts": result_texts,
        }
    finally:
        clients.teardown()


def _window_hash(window: TransferWindow) -> str:
    canon = json.dumps(sorted(
        (p.pair_id, p.fact.anchor, "|".join(sorted(p.fact.alternatives)), p.fact.carrier_text)
        for p in window.pairs))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _value_map_hash(window: TransferWindow) -> str:
    canon = json.dumps(sorted(
        (p.pair_id, p.fact.value, p.default_value) for p in window.pairs))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


REQUIRED_PROVENANCE = (
    "transfer_window_hash", "seed", "model", "llm_digest", "fact_value_map_hash",
    "demand_m", "demand_fanout", "chance")


def build_transfer_report(*, arms_results: Sequence[dict], deltas: dict, necessity: dict,
                          provenance: dict) -> dict:
    """Assemble the provenance-stamped report. RAISES on incomplete provenance (a missing required
    key — a value of 0 is allowed) or any arm missing a success vector — an unreproducible number is
    refused, not masked."""
    missing = [k for k in REQUIRED_PROVENANCE
               if not provenance.get(k) and provenance.get(k) != 0]
    if missing:
        raise ValueError(f"transfer report refuses to emit on incomplete provenance: {missing}")
    for arm in arms_results:
        if not arm.get("success"):
            raise ValueError(f"arm {arm.get('name')!r} has no success vector — refusing to emit")
    return {"arms": list(arms_results), "deltas": deltas, "necessity": necessity,
            "provenance": provenance}


def _valid_subvec(vec: Sequence[int], valid_mask: Sequence[bool]) -> list[int]:
    return [v for v, keep in zip(vec, valid_mask) if keep]


def run_transfer(*, arms: Sequence[Arm], transfer_window: TransferWindow, seed: int,
                 daemon_factory, agent_turn, vouch_fn, outcome_fn=label_transfer_gold,
                 model: str = "unknown", llm_digest: str = "", chance: float = 0.34,
                 demand_m: int = 2) -> dict:
    """Run every arm over the transfer pairs, certify pair validity with the necessity gate, and emit
    the provenance-stamped report. The headline Δ is ``recall_on − recall_off`` over the VALID pairs
    only (both arms required for it); ``demand_promote − recall_off`` is the secondary autonomous
    contrast. ``model`` / ``llm_digest`` stamp the agent substrate (Claude subscription only)."""
    window = transfer_window
    value_by_pair = {p.pair_id: p.fact.value for p in window.pairs}
    results = {arm.name: _run_arm(
        arm, window=window, daemon_factory=daemon_factory, agent_turn=agent_turn,
        vouch_fn=vouch_fn, outcome_fn=outcome_fn) for arm in arms}

    necessity: dict = {}
    off, oracle = results.get("recall_off"), results.get("oracle_ceiling")
    if off and oracle:
        necessity = scoring.necessity_gate(
            off_success=off["success_vec"], oracle_success=oracle["success_vec"], chance=chance)

    deltas: dict = {}
    on = results.get("recall_on")
    if on and off and necessity.get("valid_mask"):
        mask = necessity["valid_mask"]
        on_v, off_v = _valid_subvec(on["success_vec"], mask), _valid_subvec(off["success_vec"], mask)
        if on_v:
            success_ci = scoring.paired_delta_ci(on_v, off_v, seed=seed)
            deltas["success_ci"] = success_ci
            deltas["success"] = scoring.transfer_verdict(success_ci)
            dem = results.get("demand_promote")
            if dem:
                dem_v = _valid_subvec(dem["success_vec"], mask)
                deltas["demand_vs_off_ci"] = scoring.paired_delta_ci(dem_v, off_v, seed=seed)
        else:
            deltas["note"] = "no valid pairs after the necessity gate"

    # chain diagnostics for the headline arm (off the gold path)
    chain = {}
    if on:
        chain = diagnostics.chain_decomposition(
            captured=on["_captured"], servable=on["_servable_texts"],
            served=on["_served_texts"], value_by_pair=value_by_pair)

    # transfer's claude -p build turns are LIVE (non-replayable), so the honest reproducibility
    # fingerprint is a digest of the actual agent outputs this run produced — used when the caller
    # does not supply one. It stamps what the agents DID, not a replayable transcript.
    all_texts = [t for r in results.values() for t in r.get("_result_texts", [])]
    auto_digest = hashlib.sha256(json.dumps(all_texts).encode("utf-8")).hexdigest()
    demand_fanout = next((a.demand_fanout for a in arms if a.promotion == "demand"), 0)
    provenance = {
        "transfer_window_hash": _window_hash(window),
        "seed": seed, "model": model, "llm_digest": llm_digest or auto_digest,
        "fact_value_map_hash": _value_map_hash(window),
        "demand_m": demand_m, "demand_fanout": demand_fanout, "chance": chance}

    arms_results = [{k: v for k, v in r.items() if not k.startswith("_")} for r in results.values()]
    if chain:
        for r in arms_results:
            if r["name"] == "recall_on":
                r["chain_diagnostics"] = chain
    return build_transfer_report(arms_results=arms_results, deltas=deltas, necessity=necessity,
                                 provenance=provenance)
