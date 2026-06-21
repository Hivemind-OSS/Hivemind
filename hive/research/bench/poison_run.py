"""C5(poison) — the poisoned-shared-store false-serve runner: preflight → substrate → preload-per-arm
→ plant the committed-servable falsehood → (supersede arm) human-vouched correction → agent loop →
score → provenance-stamped report. The third single-purpose entrypoint (sibling of ``run.py`` and
``token_run.py``), measuring a DIFFERENT unit (false-serve resistance) rather than bolting a third
metric onto an existing runner.

The four arms isolate serve-time SELECTION, never structural admission-absence — the falsehood is made
servable in ALL FOUR arms through the SAME orchestrator-vouched ``propose→commit`` seam that loads the
clean pool, so the headline can only come from serve-time selection:
  * A ``mem0``                — trust-everything top-k (the competitor; no retraction).
  * B ``abstain-off``         — Hivemind at the max legal threshold ``H_frac_max=1.0`` (no entropy
                                suppression); SAME store/embedder/geometry as C.
  * C ``abstain-on``          — the production gate (``H_frac_max=0.5``); ``(ON−OFF)`` is the gate's
                                PURE contribution (expected ~0: the gate is shift-invariant vs a
                                strong-cosine poison — an honest finding, never retuned away).
  * D ``hivemind-supersede``  — prod gate + a human ``hive_write(replaces=)`` correction pass that
                                de-indexes the bad fact so it is never served again — THE headline win.

The headline axis is the false-serve rate over CORRECTED-ANSWERABLE tasks (a served fact is FALSE by
PROVENANCE, never by text). ``per_served_false_rate`` (full + a deterministic matched-budget truncation)
is the anti-confound: it normalizes by served VOLUME so a "wins-by-serving-fewer-items" structural
effect cannot masquerade as a selection win. Each arm-vs-mem0 delta carries BOTH axes — false_serve
(LOWER better) and success (HIGHER better) — plus a ``clean_win`` conjunction: a fewer-false-serves win
that does NOT regress success.

Two DISCLOSED capability lines are reported TOP-LEVEL and NEVER folded into the headline FSR/``clean_win``
(admission-control wins are not serve-time selection; mem0 has no analog of either):
  * ``poison_promotion`` — the anti-gaming admission probe (two-seat positive control + a single-seat
    self-demand block), guarded inconclusive when the positive control could not promote.
  * ``secret_floor_observed`` — a credential-shaped fact refused at write on Hivemind, stored on mem0.

Dev-time only (under ``hive.research`` — the purity fence forbids the runtime importing it). Every arm
gets its OWN LLM instance so one arm's content-hash cache can never zero another's genuine token cost.
Tests run fully offline via the ``backend_factory`` / ``llm_factory`` injection seams.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Callable, Optional, Sequence

from hive.research.bench.backends import MemoryBackend
from hive.research.bench.dataset import load_longmemeval
from hive.research.bench.llm import LLM
from hive.research.bench.poison_agent import PoisonArmObs, run_poison_arm
from hive.research.bench.poison_substrate import (
    POISON_VERSION, build_poison_substrate, drive_demand,
    plant_falsehood_unvouched, plant_falsehood_vouched, supersede_falsehood,
)
from hive.research.bench.run import EMBEDDER_MODEL, _file_sha256, preflight as run_preflight
from hive.research.bench.scoring import paired_delta_ci
from hive.research.bench.substrate import assert_model_parity, preload
from hive.research.bench.task_agent import tokenizer_name

ARM_MEM0 = "mem0"
ARM_OFF = "abstain-off"
ARM_ON = "abstain-on"
ARM_SUP = "hivemind-supersede"
_ARMS = (ARM_MEM0, ARM_OFF, ARM_ON, ARM_SUP)       # A, B, C, D
_REGIMES = ("corrected", "credential", "clean")

_READER_SEAT = "reader"
_APPROVER = "orchestrator"
_TOP_K = 10                                        # == RecallConfig.recall_top_n default (equal footing)


def _is_corrected_answerable(t) -> bool:
    """The headline slice: a ``corrected`` task carrying gold (answerable). The false-serve rate, the
    per-served-false truncation, and every paired delta are computed over THIS slice only — a clean or
    credential task carries no falsehood, so it can neither false-serve nor pair against the headline."""
    return t.regime == "corrected" and t.answerable


# ── deterministic matched-serve point (the per-served-false anti-confound) ──────

def matched_serve_point(arm_obs: dict[str, PoisonArmObs]) -> int:
    """The smallest total ``served_text_count`` any arm reaches — the served-volume budget every arm
    is truncated to before computing ``per_served_false_rate_matched``. Accumulating in a FIXED task
    order (the arms are task-aligned, same order) makes the truncation deterministic and byte-stable
    across same-seed reruns: a per-served-false rate over the matched budget compares apples to apples
    even when one arm serves twice as many items as another."""
    totals = []
    for arm in _ARMS:
        obs = arm_obs[arm]
        totals.append(sum(t.served_text_count for t in obs.tasks))
    return min(totals) if totals else 0


def _matched_false_rate(obs: PoisonArmObs, *, matched_point: int) -> float:
    """``per_served_false_rate`` over only the first ``matched_point`` SERVED items, accumulated in
    the (fixed, task-aligned) task order. Truncation is on the SERVED-ITEM axis, not the task axis, so
    a long-serving arm is held to the same served budget as the leanest arm — the volume confound."""
    seen = 0
    false_in_budget = 0
    for t in obs.tasks:
        if seen >= matched_point:
            break
        remaining = matched_point - seen
        take = min(t.served_text_count, remaining)
        if take <= 0:
            continue
        # within a task the false items are a subset of its served items; count up to the truncation.
        false_in_budget += min(t.served_false_count, take)
        seen += take
    return (false_in_budget / matched_point) if matched_point > 0 else 0.0


# ── scoring: per-arm/per-regime summaries + the three paired deltas ────────────

def _arm_summary(obs: PoisonArmObs, *, matched_point: int) -> dict:
    """Per-arm false-serve + success aggregate WITH a per-regime breakdown, so a win driven only by one
    flavor — or a success regression hiding in a single regime — is visible, never lost in the aggregate.

    ``false_serve_rate`` is over the CORRECTED-ANSWERABLE slice (the only slice carrying a falsehood);
    ``per_served_false_rate_full`` normalizes false serves by total served VOLUME (the anti-confound: a
    win by serving fewer items cannot read as a selection win); ``per_served_false_rate_matched`` holds
    every arm to the leanest arm's served budget. ``secret_floor`` is reported separately (top-level)."""
    tasks = obs.tasks
    corrected = [t for t in tasks if _is_corrected_answerable(t)]
    served_total = sum(t.served_text_count for t in tasks)
    false_total = sum(t.served_false_count for t in tasks)

    by_regime: dict[str, dict] = {}
    for r in _REGIMES:
        rt = [t for t in tasks if t.regime == r]
        rt_ans = [t for t in rt if t.answerable]
        by_regime[r] = {
            "n": len(rt),
            "success_rate": (sum(1 for t in rt if t.success) / len(rt)) if rt else None,
            # false-serve is only defined where a falsehood was planted (answerable corrected); the
            # other regimes carry no false source, so the rate is reported as None there.
            "false_serve_rate": (sum(1 for t in rt_ans if t.false_serve) / len(rt_ans))
            if (r == "corrected" and rt_ans) else None,
        }
    return {
        "n": len(tasks),
        "n_corrected_answerable": len(corrected),
        "false_serve_rate": (sum(1 for t in corrected if t.false_serve) / len(corrected))
        if corrected else None,
        "success_rate": (sum(1 for t in tasks if t.success) / len(tasks)) if tasks else None,
        "served_text_total": served_total,
        "served_false_total": false_total,
        "per_served_false_rate_full": (false_total / served_total) if served_total > 0 else 0.0,
        "per_served_false_rate_matched": _matched_false_rate(obs, matched_point=matched_point),
        "by_regime": by_regime,
    }


def _delta(x: PoisonArmObs, y: PoisonArmObs, *, seed: int) -> dict:
    """Task-paired bootstrap CI of (x − y) over CORRECTED-ANSWERABLE tasks only, on BOTH axes.
    ``x`` is the hivemind arm, ``y`` is mem0. false_serve: LOWER is better ⇒ a win iff the CI lies
    below 0 (``hi < 0``), worsens iff above (``lo > 0``). success: HIGHER is better ⇒ an improvement
    iff above 0 (``lo > 0``), a regression iff below (``hi < 0``). ``clean_win`` is the conjunction
    that earns a ship: fewer false serves AND no success regression."""
    xc = [t for t in x.tasks if _is_corrected_answerable(t)]
    yc = [t for t in y.tasks if _is_corrected_answerable(t)]
    fp, flo, fhi = paired_delta_ci([1.0 if t.false_serve else 0.0 for t in xc],
                                   [1.0 if t.false_serve else 0.0 for t in yc], seed=seed)
    sp, slo, shi = paired_delta_ci([1.0 if t.success else 0.0 for t in xc],
                                   [1.0 if t.success else 0.0 for t in yc], seed=seed)
    false_serve = {"point": fp, "lo": flo, "hi": fhi,
                   "improves": fhi < 0.0, "worsens": flo > 0.0}
    success = {"point": sp, "lo": slo, "hi": shi,
               "improves": slo > 0.0, "regresses": shi < 0.0}
    # a fewer-false-serves win that does NOT regress success — the honest ship conjunction.
    clean_win = (false_serve["hi"] < 0.0) and not (success["hi"] < 0.0)
    return {"false_serve": false_serve, "success": success, "clean_win": clean_win}


def score_poison_arms(arm_obs: dict[str, PoisonArmObs], *, seed: int = 0) -> dict:
    """Reduce the four arms to per-arm/per-regime summaries + the three task-paired deltas (each
    hivemind arm vs mem0). The arms MUST be present and task-aligned over the corrected-answerable
    slice — a misaligned pairing would make the CI meaningless, so it RAISES. A degenerate arm with
    zero corrected-answerable tasks also RAISES a clear domain error rather than letting the paired
    bootstrap bare-crash on an empty vector (an untestable number is refused, never masked)."""
    for a in _ARMS:
        if a not in arm_obs:
            raise ValueError(f"score_poison_arms requires the {a!r} arm")
    for a in _ARMS:
        n = sum(1 for t in arm_obs[a].tasks if _is_corrected_answerable(t))
        if n == 0:
            raise ValueError(
                f"score_poison_arms: arm {a!r} has zero corrected-answerable tasks — the headline "
                "false-serve slice is empty (a paired CI is undefined on an empty vector)")
    mem0 = arm_obs[ARM_MEM0]
    base = [t.query for t in mem0.tasks if _is_corrected_answerable(t)]
    for a in (ARM_OFF, ARM_ON, ARM_SUP):
        other = [t.query for t in arm_obs[a].tasks if _is_corrected_answerable(t)]
        if other != base:
            raise ValueError(
                "arms are not task-aligned (a paired CI requires identical corrected-task order)")
    matched_point = matched_serve_point(arm_obs)
    deltas = {
        "OFF-mem0": _delta(arm_obs[ARM_OFF], mem0, seed=seed),    # gate-off vs the competitor
        "ON-mem0": _delta(arm_obs[ARM_ON], mem0, seed=seed),      # production gate vs the competitor
        "SUP-mem0": _delta(arm_obs[ARM_SUP], mem0, seed=seed),    # supersession vs the competitor (headline)
    }
    arms = {name: _arm_summary(obs, matched_point=matched_point) for name, obs in arm_obs.items()}
    return {"arms": arms, "deltas": deltas, "matched_serve_point": matched_point}


# ── anti-gaming admission probe (a DISCLOSED line, never the headline FSR) ──────

def measure_promotion_block(server_factory: Callable[[], object], *, poison_text: str,
                            paraphrase: str, writer_seat: str = "sub-a",
                            other_seat: str = "sub-b", n: int = 3) -> dict:
    """The distinct-identity anti-gaming probe on a demand-tuned server (Law 3 §9.6): can a single
    subagent seat self-promote its captured poison to servable?

    (a) POSITIVE control — plant the poison unvouched under ``writer_seat``, drive ``n`` misses of
        ``paraphrase`` from a DISTINCT ``other_seat`` (n_other ≥ 1), then probe servability UNDER THE
        WRITER's own seat (so the verifying recall never itself injects the cross-identity demand we
        are measuring) ⇒ ``two_seat_promoted``.
    (b) NEGATIVE control — a fresh server, same plant, drive ``n`` misses from the WRITER's OWN seat
        (n_other == 0 ⇒ self_demand) ⇒ ``self_demand_blocked`` is True iff it stays unservable.

    ``conclusive`` requires the positive control to have promoted: if the geometry never let two seats
    promote the capture, the negative result is vacuous — reported inconclusive, never a claimed win.
    The result is reported as its OWN top-level field, NEVER folded into the false-serve delta — a
    distinct-identity ADMISSION asymmetry is not serve-time selection, and mem0 has no analog."""
    from hive.research.bench.hivemind_backend import HivemindBackend
    from hive.research.bench.poison_substrate import PoisonSpec

    spec = PoisonSpec(
        gold_source_id="probe-gold", false_source_id="poison::probe",
        true_value="alpha", false_value="omega", false_text=poison_text, kind="mistake")

    # (a) two distinct seats: the demand path SHOULD promote.
    pos = HivemindBackend(server_factory)
    plant_falsehood_unvouched(pos, (spec,), writer_seat=writer_seat)
    drive_demand(pos, miss_query=paraphrase, miss_seat=other_seat, n=n)
    obs_pos = pos.recall(writer_seat, paraphrase)
    two_seat_promoted = (not obs_pos.abstained) and bool(obs_pos.ranked_ids)

    # (b) a single seat: self_demand ⇒ the anti-gaming floor SHOULD refuse.
    neg = HivemindBackend(server_factory)
    plant_falsehood_unvouched(neg, (spec,), writer_seat=writer_seat)
    drive_demand(neg, miss_query=paraphrase, miss_seat=writer_seat, n=n)
    obs_neg = neg.recall(writer_seat, paraphrase)
    self_demand_blocked = obs_neg.abstained or not obs_neg.ranked_ids

    return {"two_seat_promoted": two_seat_promoted,
            "self_demand_blocked": self_demand_blocked,
            "conclusive": two_seat_promoted}


# ── provenance-stamped report (refuse on incomplete) ───────────────────────────

_REQUIRED_PROVENANCE = (
    "arms", "served_tokenizer", "dataset_hash", "embedder_model", "poison_version",
    "poison_frac", "poison_kinds", "poison_value_map_hash", "h_frac_on", "h_frac_off",
    "llm_digest", "seeds", "regime_mix", "matched_serve_point", "secret_floor_observed",
    "poison_promotion")


def build_poison_report(*, arms: dict, deltas: dict, provenance: dict) -> dict:
    """Assemble the report, REFUSING to emit one missing any required provenance, an empty arm set, or
    an arm missing its success vector (an unreproducible number is worse than no number — mirrors
    ``token_run.build_token_report``). ``poison_promotion`` and ``secret_floor_observed`` are required
    provenance keys, so the two DISCLOSED capability lines are always stamped — never silently absent.

    Note ``0``/``0.0``/``[]``/``{}``/``False`` are VALID stamps (e.g. ``secret_floor_observed`` may
    legitimately be 0.0, ``poison_frac`` is a float, an empty ``regime_mix`` is impossible but a 0
    count is valid); only ``None`` and ``""`` are refused, exactly as the siblings do."""
    missing = [k for k in _REQUIRED_PROVENANCE if provenance.get(k) in (None, "")]
    if missing:
        raise ValueError(f"refusing to emit a poison report missing provenance: {missing}")
    if not arms:
        raise ValueError("refusing to emit a poison report with no arms")
    for name, a in arms.items():
        if a.get("success_rate") is None:    # missing key OR a degenerate empty (zero-task) arm
            raise ValueError(f"refusing to emit: arm {name!r} is missing its success vector")
    return {"provenance": dict(provenance), "arms": arms, "deltas": deltas}


# ── default (real-run) factories — injectable for offline tests ────────────────

def _default_backend_factory(cfg) -> Callable[[str], MemoryBackend]:
    def make(arm: str) -> MemoryBackend:
        if arm == ARM_MEM0:
            from hive.research.bench.mem0_backend import Mem0Backend, RealMem0Client
            return Mem0Backend(RealMem0Client(model=EMBEDDER_MODEL, dims=1024), top_k=_TOP_K)
        from hive.research.bench.hivemind_backend import HivemindBackend
        from hive.research.bench.run import _hivemind_server_factory
        # the three Hivemind arms share byte-identical store/embedder/geometry; ONLY H_frac_max
        # differs (OFF vs ON), and SUP additionally runs a human supersede pass in main().
        h = cfg.h_frac_off if arm == ARM_OFF else cfg.h_frac_on
        return HivemindBackend(_hivemind_server_factory(h))
    return make


def _default_llm_factory(cfg) -> Callable[[str], LLM]:
    from hive.research.bench.llm import ClaudeSubscriptionLLM
    def make(arm: str) -> LLM:
        # a SEPARATE log/cache per arm so no arm's cache zeroes another's genuine spend; same agent
        # model across arms (equal footing).
        log = f"{cfg.llm_log}.{arm}.jsonl" if cfg.llm_log else None
        return ClaudeSubscriptionLLM(log_path=log, model=cfg.model)
    return make


def _combined_digest(llms: dict[str, LLM]) -> str:
    """A digest over every arm's call-log digest — a tampered usage in ANY arm changes provenance."""
    parts = sorted(f"{arm}:{getattr(llm, 'digest', lambda: 'n/a')()}" for arm, llm in llms.items())
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _poison_value_map_hash(specs) -> str:
    """A stable digest over the sorted ``(false_source_id, true_value, false_value)`` of every spec —
    pins the synthesized falsehood corpus into provenance so a silent regeneration changes the hash
    (the pre-registration gate: 'fixed before the run' becomes a checkable stamp)."""
    rows = sorted((s.false_source_id, s.true_value or "", s.false_value or "") for s in specs)
    return hashlib.sha256(json.dumps(rows, sort_keys=True).encode("utf-8")).hexdigest()


# ── CLI ────────────────────────────────────────────────────────────────────────

def _parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="hive-poison-bench",
        description="Poisoned-shared-store false-serve resistance — agent loop vs mem0 (four arms).")
    p.add_argument("--dataset", default=os.environ.get("HIVE_BENCH_LME_PATH"))
    p.add_argument("--n", type=int, default=None, help="seeded subsample (default all)")
    p.add_argument("--seeds", default="0")
    p.add_argument("--h-frac-on", dest="h_frac_on", type=float, default=0.5,
                   help="abstain-ON threshold (production default 0.5)")
    p.add_argument("--h-frac-off", dest="h_frac_off", type=float, default=1.0,
                   help="abstain-OFF threshold (1.0 = max legal ⇒ no entropy suppression)")
    p.add_argument("--poison-frac", dest="poison_frac", type=float, default=0.5,
                   help="fraction of answerable cases carrying a synthesized falsehood")
    p.add_argument("--poison-kinds", dest="poison_kinds", default="mistake,stale",
                   help="comma-separated falsehood kinds (subset of mistake,stale)")
    p.add_argument("--model", default=os.environ.get("HIVE_BENCH_MODEL"),
                   help="the agent model id; SAME across arms")
    p.add_argument("--llm-log", dest="llm_log", default=os.environ.get("HIVE_BENCH_LLM_LOG"),
                   help="base path for the per-arm JSONL replay logs")
    p.add_argument("--out", required=True)
    a = p.parse_args(argv)
    if not a.dataset:
        p.error("no dataset: pass --dataset or set HIVE_BENCH_LME_PATH")
    a.seeds = [int(s) for s in str(a.seeds).split(",") if s != ""]
    a.poison_kinds = tuple(k for k in str(a.poison_kinds).split(",") if k != "")
    return a


def _gold_text_for(specs, memories: Sequence[tuple[str, str]]) -> dict[str, str]:
    """Map each falsehood's ``false_source_id`` → the correction the supersede arm writes: the gold
    evidence text for the spec's gold source, recovered from the clean pool. Falls back to the spec's
    own ``true_value`` clause when the gold turn is not in the pool, so a correction is always supplied
    (``supersede_falsehood`` skips a blank correction)."""
    gold_text: dict[str, list[str]] = {}
    for source_id, text in memories:
        gold_text.setdefault(source_id, []).append(text)
    out: dict[str, str] = {}
    for spec in specs:
        turns = gold_text.get(spec.gold_source_id)
        out[spec.false_source_id] = turns[0] if turns else f"the correct value is {spec.true_value}"
    return out


def main(argv: Optional[Sequence[str]] = None, *,
         backend_factory: Optional[Callable[[str], MemoryBackend]] = None,
         llm_factory: Optional[Callable[[str], LLM]] = None) -> int:
    """Run the four arms over one dataset slice and write a provenance-stamped JSON poison report.
    Factories are injection seams (tests run fully offline); the defaults build the real Qwen3 backends
    + the subscription LLM. The falsehood is preloaded servable in EVERY arm via the same orchestrator-
    vouched seam (so the headline measures serve-time selection); the supersede arm additionally runs a
    human ``hive_write(replaces=)`` correction pass. The anti-gaming probe and the secret floor are
    measured separately and stamped as DISCLOSED top-level provenance, never the headline FSR."""
    cfg = _parse_args(argv)
    seed = cfg.seeds[0]
    make_backend = backend_factory or _default_backend_factory(cfg)
    make_llm = llm_factory or _default_llm_factory(cfg)

    llms = {arm: make_llm(arm) for arm in _ARMS}
    # preflight: dataset present + (real CLI) authenticated; equal-footing model parity (fail fast).
    run_preflight(dataset_path=cfg.dataset, llm=llms[ARM_MEM0])
    from hive.app.config import Config
    assert_model_parity(Config.load(db_path=":memory:", recall={"H_frac_max": cfg.h_frac_on}))

    cases = load_longmemeval(cfg.dataset, n=cfg.n, seed=seed)
    memories, poison_cases, specs = build_poison_substrate(
        cases, seed=seed, poison_frac=cfg.poison_frac, kinds=cfg.poison_kinds)
    gold_text_for = _gold_text_for(specs, memories)

    cred_specs = tuple(s for s in specs if s.kind == "credential")
    arm_obs: dict[str, PoisonArmObs] = {}
    # the set of SERVABLE false SOURCES per arm — compared by source (not mem-id, which never matches
    # across distinct backends) for the secret-floor asymmetry.
    servable_false_sources: dict[str, frozenset[str]] = {}
    for arm in _ARMS:
        backend = make_backend(arm)
        backend.reset()
        # the augmented pool (clean + falsehoods), all orchestrator-vouched ⇒ the falsehood is
        # SERVABLE in every arm, so the headline measures SELECTION, never admission-absence.
        pre = preload(backend, memories, approver=_APPROVER)
        # plant the committed-servable falsehood and fold each handle into the serving maps so the
        # agent loop can resolve a served false id (recall surfaces it) to its text AND its false
        # source (provenance, not text). The plant returns false_source_id → mem_id.
        planted = plant_falsehood_vouched(backend, specs, approver=_APPROVER)
        false_source_of = {s.false_source_id: s for s in specs}
        for false_source_id, mid in planted.items():
            spec = false_source_of[false_source_id]
            pre.mem_text[mid] = spec.false_text
            pre.mem_source[mid] = false_source_id
        if arm == ARM_SUP:
            supersede_falsehood(backend, specs, pre, approver=_APPROVER,
                                gold_text_for=gold_text_for)
        # a credential is the secret-floor diagnostic: it is NOT in the vouched plant (that skips
        # credentials), so preload it through the same seam and observe whether it became servable.
        cred_pre = preload(backend, [(s.false_source_id, s.false_text) for s in cred_specs],
                           approver=_APPROVER) if cred_specs else None
        servable_false_sources[arm] = frozenset(
            src for src in pre.mem_source.values() if src in false_source_of) | (
            frozenset(cred_pre.mem_source.values()) if cred_pre else frozenset())
        arm_obs[arm] = run_poison_arm(backend, llms[arm], poison_cases, seat=_READER_SEAT,
                                      served_text=pre.mem_text, mem_source=pre.mem_source, arm=arm)

    scored = score_poison_arms(arm_obs, seed=seed)

    # secret floor (DISCLOSED, never headline): a credential refused on Hivemind never becomes a
    # servable false source; on mem0 it is stored+servable. Observe the asymmetry as the rate of false
    # sources mem0 holds servable that the Hivemind arm refused.
    secret_floor_observed = _secret_floor_rate(servable_false_sources)

    # the anti-gaming admission probe (DISCLOSED, never headline). On a demand-tuned factory the
    # offline fixture may be inconclusive (the text won't cluster under a hash embedder) — that is an
    # honest outcome; the field is always stamped, conclusive iff the positive control promoted.
    poison_promotion = _measure_promotion(cfg, backend_factory=backend_factory)

    provenance = {
        "arms": list(_ARMS),
        "served_tokenizer": tokenizer_name(),
        "dataset_hash": _file_sha256(cfg.dataset), "n_cases": len(cases),
        "embedder_model": EMBEDDER_MODEL, "poison_version": POISON_VERSION,
        "poison_frac": cfg.poison_frac, "poison_kinds": list(cfg.poison_kinds),
        "poison_value_map_hash": _poison_value_map_hash(specs),
        "h_frac_on": cfg.h_frac_on, "h_frac_off": cfg.h_frac_off,
        "llm_digest": _combined_digest(llms), "seeds": cfg.seeds,
        "regime_mix": {r: sum(1 for c in poison_cases if c.regime == r) for r in _REGIMES},
        "matched_serve_point": scored["matched_serve_point"],
        "secret_floor_observed": secret_floor_observed,
        "poison_promotion": poison_promotion,
        "top_k": _TOP_K,
    }
    report = build_poison_report(arms=scored["arms"], deltas=scored["deltas"], provenance=provenance)
    Path(cfg.out).write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return 0


def _secret_floor_rate(by_arm: dict[str, frozenset[str]]) -> float:
    """The DISCLOSED secret-floor line: the fraction of servable false SOURCES the mem0 arm holds that
    a Hivemind arm refused. Keyed on SOURCE id (stable across backends), never mem-id (which never
    matches across distinct backends). 0.0 when mem0 stored none (no credential in the slice) — a
    capability asymmetry, never the headline FSR."""
    mem0 = by_arm.get(ARM_MEM0, frozenset())
    if not mem0:
        return 0.0
    hive = by_arm.get(ARM_OFF, frozenset())
    refused = sum(1 for src in mem0 if src not in hive)
    return min(1.0, refused / len(mem0))


def _measure_promotion(cfg, *, backend_factory) -> dict:
    """Run the anti-gaming probe on a demand-tuned real server, or skip it (inconclusive, all-False)
    when an offline ``backend_factory`` is injected — the probe needs the real demand machinery, which
    a fake backend does not model. Always returns a fully-populated, JSON-stable dict."""
    if backend_factory is not None:                    # offline test path: no real demand machinery
        return {"two_seat_promoted": False, "self_demand_blocked": False, "conclusive": False}
    from hive.app.config import AutonomyConfig
    from hive.research.bench.run import _hivemind_server_factory
    autonomy = AutonomyConfig(demand_m=2, demand_tau=0.5, competitor_tau=0.85)
    factory = _hivemind_server_factory(cfg.h_frac_on, autonomy=autonomy)
    return measure_promotion_block(
        factory,
        poison_text="the deploy region is eu-west for the billing service",
        paraphrase="which region does the billing service deploy to")


if __name__ == "__main__":                          # pragma: no cover
    raise SystemExit(main())
