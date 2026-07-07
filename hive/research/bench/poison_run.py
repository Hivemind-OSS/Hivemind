"""C5(poison) — the poisoned-shared-store false-serve runner: preflight → substrate → preload-per-arm
→ plant the committed-servable falsehood → (supersede arm) human-vouched correction → agent loop →
score → provenance-stamped report. The third single-purpose entrypoint (sibling of ``run.py`` and
``token_run.py``), measuring a DIFFERENT unit (false-serve resistance) rather than bolting a third
metric onto an existing runner.

The four arms isolate serve-time SELECTION, never structural admission-absence — the falsehood is made
servable in ALL FOUR arms through the SAME orchestrator-vouched ``propose→commit`` seam that loads the
clean pool, so the headline can only come from serve-time selection:
  * A ``mem0``                — trust-everything top-k (the competitor; no retraction).
  * B ``abstain-off``         — Hivemind at the serve-everything proxy ``tau_serve=0.01`` (no
                                suppression); SAME store/embedder/geometry as C.
  * C ``abstain-on``          — the production gate (``tau_serve=0.70``); ``(ON−OFF)`` is the gate's
                                PURE contribution (expected ~0: the gate keys on ABSOLUTE relevance,
                                not truth, so it does not resist a STRONG-cosine servable falsehood —
                                an honest finding, never retuned away).
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

``--l4`` swaps the four serve-time arms for the VERIFIED-PROMOTION gate pair — the empirical gate
the ``verified_promotion`` default flip is conditioned on:
  * C′ ``l4-off``           — pool (gold AND poison) planted as CAPTURES (quarantined, unservable),
                              SHA-bound verified evidence injected (helped on gold, hurt on poison),
                              demand driven — with the rung OFF nothing can promote (the control).
  * E  ``verified-outcome`` — byte-identical treatment, ``verified_promotion=ON``: only the rung
                              differs, so the E − C′ delta is its end-to-end contribution.
  Scored SUCCESS-led over the corrected-answerable slice: ``clean_win`` ⇔ success improves
  (CI lo > 0) AND false-serve does not worsen — the §5 axis the rung exists to move, guarded by
  the poison side (a hurt row must never promote).

Dev-time only (under ``hive.research`` — the purity fence forbids the runtime importing it). Every arm
gets its OWN LLM instance so one arm's content-hash cache can never zero another's genuine token cost.
Tests run fully offline via the ``backend_factory`` / ``llm_factory`` injection seams.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Callable, Optional, Sequence

from hive.domain.change_evidence import (
    ChangeOutcome, TouchedSubject, _VERIFIED_REASONS, render_verified_payload,
    version_stamp,
)
from hive.domain.evidence_kinds import (
    EK_OUTCOME_VERIFIED_HELPED, EK_OUTCOME_VERIFIED_HURT,
)
from hive.research.bench.backends import MemoryBackend
from hive.research.bench.dataset import load_longmemeval
from hive.research.bench.llm import LLM
from hive.research.bench.poison_agent import PoisonArmObs, run_poison_arm
from hive.research.bench.poison_substrate import (
    POISON_VERSION, PoisonTaskCase, Preloaded, build_poison_substrate, drive_demand,
    make_credential_text, plant_falsehood_unvouched, preload_captures,
    supersede_falsehood,
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
_REGIMES = ("corrected", "clean")

# The L4 empirical-gate pair (run with --l4): the verified-win promotion rung ON vs OFF over
# IDENTICAL capture-only plants + identical injected verified evidence — the flag is the sole
# difference, so the E − C′ delta is the rung's end-to-end contribution and nothing else.
ARM_L4_OFF = "l4-off"                              # C′ — verified_promotion OFF (the control)
ARM_L4_ON = "verified-outcome"                     # E  — verified_promotion ON
_L4_ARMS = (ARM_L4_OFF, ARM_L4_ON)
_L4_DELTA_KEY = "E-Cprime"

_READER_SEAT = "reader"
_APPROVER = "orchestrator"
_DRIVER_SEAT = "demand-driver"                     # the L4 drive seat (distinct from the planter)
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
    even when one arm serves twice as many items as another. Computed over whatever arm set is
    present in ``arm_obs`` (the four serve-time arms, or the L4 gate pair)."""
    totals = [sum(t.served_text_count for t in obs.tasks) for obs in arm_obs.values()]
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


# ── the L4 gate pair: E (verified-outcome) vs C′ (l4-off) ───────────────────────

def _l4_delta(e: PoisonArmObs, cprime: PoisonArmObs, *, seed: int) -> dict:
    """Task-paired bootstrap CI of (E − C′) over CORRECTED-ANSWERABLE tasks on BOTH axes.
    Unlike the serve-time arms' FSR-led ``_delta``, the gate here is SUCCESS-LED — the axis the
    verified-win rung exists to move: ``clean_win`` ⇔ the success delta improves (CI lo > 0)
    AND the false-serve delta does not worsen (its CI is not entirely above 0). A rung that
    lifts success by also serving poison is refused; so is a rung that moves nothing."""
    ec = [t for t in e.tasks if _is_corrected_answerable(t)]
    cc = [t for t in cprime.tasks if _is_corrected_answerable(t)]
    fp, flo, fhi = paired_delta_ci([1.0 if t.false_serve else 0.0 for t in ec],
                                   [1.0 if t.false_serve else 0.0 for t in cc], seed=seed)
    sp, slo, shi = paired_delta_ci([1.0 if t.success else 0.0 for t in ec],
                                   [1.0 if t.success else 0.0 for t in cc], seed=seed)
    false_serve = {"point": fp, "lo": flo, "hi": fhi,
                   "improves": fhi < 0.0, "worsens": flo > 0.0}
    success = {"point": sp, "lo": slo, "hi": shi,
               "improves": slo > 0.0, "regresses": shi < 0.0}
    clean_win = success["improves"] and not false_serve["worsens"]
    return {"false_serve": false_serve, "success": success, "clean_win": clean_win}


def score_l4_arms(arm_obs: dict[str, PoisonArmObs], *, seed: int = 0) -> dict:
    """Reduce the L4 pair to per-arm/per-regime summaries + the single success-led E − C′
    delta. Mirrors ``score_poison_arms``'s refusals: both arms present, a non-empty
    corrected-answerable slice, and task alignment — a paired CI over misaligned or empty
    vectors is undefined and is REFUSED, never masked."""
    for a in _L4_ARMS:
        if a not in arm_obs:
            raise ValueError(f"score_l4_arms requires the {a!r} arm")
    for a in _L4_ARMS:
        n = sum(1 for t in arm_obs[a].tasks if _is_corrected_answerable(t))
        if n == 0:
            raise ValueError(
                f"score_l4_arms: arm {a!r} has zero corrected-answerable tasks — the headline "
                "success/false-serve slice is empty (a paired CI is undefined on an empty vector)")
    base = [t.query for t in arm_obs[ARM_L4_OFF].tasks if _is_corrected_answerable(t)]
    other = [t.query for t in arm_obs[ARM_L4_ON].tasks if _is_corrected_answerable(t)]
    if other != base:
        raise ValueError(
            "arms are not task-aligned (a paired CI requires identical corrected-task order)")
    matched_point = matched_serve_point(arm_obs)
    arms = {name: _arm_summary(obs, matched_point=matched_point) for name, obs in arm_obs.items()}
    deltas = {_L4_DELTA_KEY: _l4_delta(arm_obs[ARM_L4_ON], arm_obs[ARM_L4_OFF], seed=seed)}
    return {"arms": arms, "deltas": deltas, "matched_serve_point": matched_point}


def _evidence_sources(cases: Sequence[PoisonTaskCase]) -> tuple[set[str], set[str]]:
    """The evidence-bearing source partition: every gold-evidence session of an answerable task
    (the verified-HELPED set) and every planted-falsehood source (the verified-HURT set). A
    source in BOTH would make the injected evidence self-contradictory, so the overlap is
    REFUSED (PoisonSpec pins the distinctness at synthesis; this guards hand-built cases)."""
    gold_sources: set[str] = set()
    false_sources: set[str] = set()
    for c in cases:
        gold_sources |= set(c.gold_source_ids)
        false_sources |= set(c.false_source_ids)
    overlap = gold_sources & false_sources
    if overlap:
        raise ValueError(
            f"gold/false source overlap {sorted(overlap)} — a falsehood source may never also "
            "be gold evidence (the injected verified rows would contradict each other)")
    return gold_sources, false_sources


def _bench_stamp(*, dataset_hash: str, seed: int) -> dict:
    """The injected rows' full ``ModelVersion ⊕ VerifierVersion ⊕ SHA`` binding (L7), with
    bench-labelled values naming the run's actual inputs (dataset digest + seed). Built through
    the REAL ``version_stamp`` walk so the shape can never drift from what the census ingest
    writes — synthetic provenance, honestly labelled, never posing as a census receipt."""
    stamp = version_stamp({
        "base_sha": f"bench-l4-seed-{seed}",
        "head_sha": dataset_hash,
        "combdrift": {"engine": f"bench-l4/{POISON_VERSION}"},
        "matrix": {"head": {"graph_sha256": dataset_hash,
                            "commit_sha": f"bench-l4-seed-{seed}",
                            "engine_version": f"bench-l4/{POISON_VERSION}"}},
    })
    if stamp is None:                                # unreachable by construction; refuse loudly
        raise ValueError("bench stamp failed the version_stamp walk")
    return stamp


def inject_verified_evidence(store, pre: Preloaded, cases: Sequence[PoisonTaskCase], *,
                             ts: int, stamp: dict, receipt_sha256: str) -> dict:
    """The bench plays the census-ingest role: append the SHA-bound verified-outcome ledger rows
    the L4 rung reads — ``outcome_verified_helped`` on every captured row whose source is gold
    evidence for an answerable task, ``outcome_verified_hurt`` on every planted-falsehood row.
    Rows with neither stay evidence-free (the rung can never promote them). Payloads are rendered
    by the SAME ``render_verified_payload`` the real ingest uses (single owner of the payload
    shape) with a bench-labelled outcome, and carry the full version ``stamp`` (L7). The batch
    rides the store's content-keyed idempotent ``append_evidence``, so a re-injection inserts
    nothing. Dev-time only: reaching under the MCP surface to the raw store handle is sanctioned
    here precisely because ``evidence_events`` is server-written in production — the bench
    substitutes for the operator-run census ingest, not for an agent."""
    gold_sources, false_sources = _evidence_sources(cases)
    outcome = ChangeOutcome(
        base_sha=stamp["base_sha"], head_sha=stamp["head_sha"],
        receipt_sha256=receipt_sha256, receipt_schema_version="bench-l4",
        predicate_type="bench-l4/poison", phase="pre_merge", verdict="fail",
        tag="machine-checked", signal="none", hive_census_version="bench-l4")
    rows: list[tuple[int, str, str, int, str]] = []
    helped = hurt = 0
    for mid, src in sorted(pre.mem_source.items(), key=lambda kv: int(kv[0])):
        if src in gold_sources:
            kind = EK_OUTCOME_VERIFIED_HELPED
            helped += 1
        elif src in false_sources:
            kind = EK_OUTCOME_VERIFIED_HURT
            hurt += 1
        else:
            continue
        payload = render_verified_payload(
            outcome, TouchedSubject(path=f"bench/{src}", symbol=src), "bench",
            _VERIFIED_REASONS[kind], stamp)
        rows.append((int(mid), kind, "bench-l4", int(ts), payload))
    inserted, skipped = store.append_evidence(rows)
    return {"helped_rows": helped, "hurt_rows": hurt,
            "inserted": len(inserted), "skipped": skipped}


def drive_l4_demand(backend: MemoryBackend, pre: Preloaded,
                    cases: Sequence[PoisonTaskCase], *, seat: str = _DRIVER_SEAT) -> int:
    """One demand tick per evidence-bearing capture: recall the capture's OWN text (cosine 1.0
    against itself under ANY embedder, so the miss is deterministically 'about' the candidate
    and the verified-win rung is reached without tuning ``demand_tau``). Only rows whose source
    carries injected evidence are driven — a neutral haystack row has no win to evaluate, so
    driving it is pure cost. The IDENTICAL drive runs in both arms (only the wired rung
    differs); in C′ every tick is a no-op by construction. Returns the tick count."""
    gold_sources, false_sources = _evidence_sources(cases)
    ticks = 0
    for mid, src in sorted(pre.mem_source.items(), key=lambda kv: int(kv[0])):
        if src in gold_sources or src in false_sources:
            drive_demand(backend, miss_query=pre.mem_text[mid], miss_seat=seat, n=1)
            ticks += 1
    return ticks


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
    "poison_frac", "poison_kinds", "poison_value_map_hash", "tau_serve_on", "tau_serve_off",
    "llm_digest", "seeds", "regime_mix", "matched_serve_point", "secret_floor_observed",
    "poison_promotion")

# The L4 gate report's required stamps: the shared gate threshold + the L4 autonomy posture +
# the pairing invariants (per-arm promoted counts, injected-evidence counts, the L7 stamp, the
# agent model) replace the four-arm run's mem0-relative keys.
_REQUIRED_PROVENANCE_L4 = (
    "arms", "served_tokenizer", "dataset_hash", "n_cases", "embedder_model", "poison_version",
    "poison_frac", "poison_kinds", "poison_value_map_hash", "tau_serve", "autonomy",
    "llm_digest", "model", "seeds", "regime_mix", "matched_serve_point", "promoted",
    "evidence_injected", "verified_stamp", "top_k")


def build_poison_report(*, arms: dict, deltas: dict, provenance: dict,
                        required: Sequence[str] = _REQUIRED_PROVENANCE) -> dict:
    """Assemble the report, REFUSING to emit one missing any required provenance, an empty arm set, or
    an arm missing its success vector (an unreproducible number is worse than no number — mirrors
    ``token_run.build_token_report``). ``poison_promotion`` and ``secret_floor_observed`` are required
    provenance keys, so the two DISCLOSED capability lines are always stamped — never silently absent.
    ``required`` selects the stamp set per runner (the four-arm default, or the L4 gate's).

    Note ``0``/``0.0``/``[]``/``{}``/``False`` are VALID stamps (e.g. ``secret_floor_observed`` may
    legitimately be 0.0, ``poison_frac`` is a float, an empty ``regime_mix`` is impossible but a 0
    count is valid); only ``None`` and ``""`` are refused, exactly as the siblings do."""
    missing = [k for k in required if provenance.get(k) in (None, "")]
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
        # the three Hivemind arms share byte-identical store/embedder/geometry; ONLY tau_serve
        # differs (OFF vs ON), and SUP additionally runs a human supersede pass in main().
        tau = cfg.tau_serve_off if arm == ARM_OFF else cfg.tau_serve_on
        return HivemindBackend(_hivemind_server_factory(tau))
    return make


def _default_llm_factory(cfg) -> Callable[[str], LLM]:
    from hive.research.bench.llm import ClaudeSubscriptionLLM
    def make(arm: str) -> LLM:
        # a SEPARATE log/cache per arm so no arm's cache zeroes another's genuine spend; same agent
        # model across arms (equal footing).
        log = f"{cfg.llm_log}.{arm}.jsonl" if cfg.llm_log else None
        return ClaudeSubscriptionLLM(log_path=log, model=cfg.model)
    return make


def _l4_autonomy(verified_promotion: bool) -> dict:
    """The L4 arm posture: the demand rule inert (``demand_m`` huge) so the verified-win rung is
    the ONLY mechanical path out of quarantine, every other knob at its production default, and
    the flag per arm. Single source for BOTH the arm environments and the report provenance."""
    return {"demand_m": 10**9, "verified_promotion": verified_promotion}


def _default_l4_env_factory(cfg) -> Callable[[str], tuple[MemoryBackend, object]]:
    """Real Qwen3 L4 environments: per arm a fresh in-process server over an ephemeral
    ``:memory:`` store, sharing ONE warmed embedder across both arms. Returns
    ``(backend, store)`` — the raw store handle is the evidence-injection seam (the bench plays
    the census-ingest role; production ``evidence_events`` stays server-written). The bench must
    NEVER touch a persistent operator store, so a store path that resolves anywhere but
    ``:memory:`` (e.g. a stray HIVE_STORE__DB_PATH in the environment) is REFUSED, not used."""
    from hive.app import registry
    from hive.app.config import Config
    from hive.app.container import build_container
    from hive.research.bench.hivemind_backend import HivemindBackend
    base_cfg = Config.load(db_path=":memory:", recall={"tau_serve": cfg.tau_serve_on},
                           autonomy=_l4_autonomy(False))
    embedder = registry.build_embedder(base_cfg)
    embedder.load()                                    # warm Qwen3 ONCE, shared across both arms

    def make(arm: str) -> tuple[MemoryBackend, object]:
        c = Config.load(db_path=":memory:", recall={"tau_serve": cfg.tau_serve_on},
                        autonomy=_l4_autonomy(arm == ARM_L4_ON))
        if c.db_path != ":memory:":
            raise RuntimeError(
                f"bench isolation violated: the L4 arm store resolved to {c.db_path!r} — the "
                "gate runs on scratch :memory: stores only (unset HIVE_STORE__DB_PATH)")
        cont = build_container(c, tenant_id="default", agent_id="bench-orchestrator",
                               embedder=embedder)
        cont.migrate()
        cont.build_index()
        cont.warm_embedder()                           # idempotent on the shared warmed embedder
        server = cont.make_server()
        return HivemindBackend(lambda: server), cont.store
    return make


def run_l4_gate(cfg, cases, *, seed: int, env_factory: Callable[[str], tuple[MemoryBackend, object]],
                llms: dict[str, LLM]) -> dict:
    """The L4 empirical gate: plant the augmented pool (gold AND poison) as CAPTURES in both
    arms, inject the SHA-bound verified rows (helped on gold, hurt on poison), drive one demand
    tick per evidence row, run the IDENTICAL agent loop, and score E − C′ success-led. The two
    arms differ in exactly one bit — ``verified_promotion`` — so the delta is the rung's
    end-to-end contribution. A C′ that promotes ANYTHING is a broken control: the pairing is
    invalid and the run RAISES rather than emit a report."""
    memories, poison_cases, specs = build_poison_substrate(
        cases, seed=seed, poison_frac=cfg.poison_frac, kinds=cfg.poison_kinds)
    dataset_hash = _file_sha256(cfg.dataset)
    stamp = _bench_stamp(dataset_hash=dataset_hash, seed=seed)
    receipt_sha = _poison_value_map_hash(specs)
    ts = int(time.time())
    arm_obs: dict[str, PoisonArmObs] = {}
    promoted: dict[str, int] = {}
    injected: dict[str, dict] = {}
    for arm in _L4_ARMS:
        backend, store = env_factory(arm)
        pre = preload_captures(backend, memories)
        injected[arm] = inject_verified_evidence(store, pre, poison_cases, ts=ts,
                                                 stamp=stamp, receipt_sha256=receipt_sha)
        drive_l4_demand(backend, pre, poison_cases)
        promoted[arm] = int(store.trust_counts().get("provisional", 0))
        if arm == ARM_L4_OFF and promoted[arm]:
            raise ValueError(
                f"l4-off promoted {promoted[arm]} row(s) — the OFF arm must stay fully "
                "quarantined; the pairing is invalid")
        arm_obs[arm] = run_poison_arm(backend, llms[arm], poison_cases, seat=_READER_SEAT,
                                      served_text=pre.mem_text, mem_source=pre.mem_source,
                                      arm=arm)
    scored = score_l4_arms(arm_obs, seed=seed)
    from hive.app.config import AutonomyConfig
    aut = AutonomyConfig(**_l4_autonomy(False))
    provenance = {
        "arms": list(_L4_ARMS),
        "served_tokenizer": tokenizer_name(),
        "dataset_hash": dataset_hash, "n_cases": len(cases),
        "embedder_model": EMBEDDER_MODEL, "poison_version": POISON_VERSION,
        "poison_frac": cfg.poison_frac, "poison_kinds": list(cfg.poison_kinds),
        "poison_value_map_hash": receipt_sha,
        "tau_serve": cfg.tau_serve_on,
        "autonomy": {"demand_m": aut.demand_m, "demand_tau": aut.demand_tau,
                     "competitor_tau": aut.competitor_tau},
        "llm_digest": _combined_digest(llms),
        "model": cfg.model or "subscription-default",
        "seeds": cfg.seeds,
        "regime_mix": {r: sum(1 for c in poison_cases if c.regime == r) for r in _REGIMES},
        "matched_serve_point": scored["matched_serve_point"],
        "promoted": promoted,
        "evidence_injected": injected,
        "verified_stamp": stamp,
        "top_k": _TOP_K,
    }
    return build_poison_report(arms=scored["arms"], deltas=scored["deltas"],
                               provenance=provenance, required=_REQUIRED_PROVENANCE_L4)


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
    p.add_argument("--tau-serve-on", dest="tau_serve_on", type=float, default=0.70,
                   help="abstain-ON threshold (production default 0.70)")
    p.add_argument("--tau-serve-off", dest="tau_serve_off", type=float, default=0.01,
                   help="abstain-OFF threshold (0.01 = serve-everything proxy ⇒ no suppression)")
    p.add_argument("--poison-frac", dest="poison_frac", type=float, default=0.5,
                   help="fraction of answerable cases carrying a synthesized falsehood")
    p.add_argument("--poison-kinds", dest="poison_kinds", default="mistake,stale",
                   help="comma-separated falsehood kinds (subset of mistake,stale)")
    p.add_argument("--model", default=os.environ.get("HIVE_BENCH_MODEL"),
                   help="the agent model id; SAME across arms")
    p.add_argument("--llm-log", dest="llm_log", default=os.environ.get("HIVE_BENCH_LLM_LOG"),
                   help="base path for the per-arm JSONL replay logs")
    p.add_argument("--l4", action="store_true",
                   help="run the L4 verified-promotion gate pair (E verified-outcome vs C' "
                        "l4-off: capture-only plants + injected verified evidence, the "
                        "verified_promotion flag the sole difference; both arms at "
                        "--tau-serve-on) instead of the four serve-time arms")
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
         llm_factory: Optional[Callable[[str], LLM]] = None,
         l4_env_factory: Optional[Callable[[str], tuple[MemoryBackend, object]]] = None) -> int:
    """Run the four arms over one dataset slice and write a provenance-stamped JSON poison report.
    Factories are injection seams (tests run fully offline); the defaults build the real Qwen3 backends
    + the subscription LLM. The falsehood is preloaded servable in EVERY arm via the same orchestrator-
    vouched seam (so the headline measures serve-time selection); the supersede arm additionally runs a
    human ``hive_write(replaces=)`` correction pass. The anti-gaming probe and the secret floor are
    measured separately and stamped as DISCLOSED top-level provenance, never the headline FSR.

    Under ``--l4`` the runner measures the OTHER question instead — not serve-time selection but
    the verified-win promotion rung: ``run_l4_gate`` over the E/C′ pair, injected via
    ``l4_env_factory`` (default: real Qwen3 in-process servers on scratch stores)."""
    cfg = _parse_args(argv)
    seed = cfg.seeds[0]
    make_llm = llm_factory or _default_llm_factory(cfg)

    if cfg.l4:
        llms_l4 = {arm: make_llm(arm) for arm in _L4_ARMS}
        run_preflight(dataset_path=cfg.dataset, llm=llms_l4[ARM_L4_OFF])
        cases = load_longmemeval(cfg.dataset, n=cfg.n, seed=seed)
        env_factory = l4_env_factory or _default_l4_env_factory(cfg)
        report = run_l4_gate(cfg, cases, seed=seed, env_factory=env_factory, llms=llms_l4)
        Path(cfg.out).write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        return 0

    make_backend = backend_factory or _default_backend_factory(cfg)
    llms = {arm: make_llm(arm) for arm in _ARMS}
    # preflight: dataset present + (real CLI) authenticated; equal-footing model parity (fail fast).
    run_preflight(dataset_path=cfg.dataset, llm=llms[ARM_MEM0])
    from hive.app.config import Config
    assert_model_parity(Config.load(db_path=":memory:", recall={"tau_serve": cfg.tau_serve_on}))

    cases = load_longmemeval(cfg.dataset, n=cfg.n, seed=seed)
    memories, poison_cases, specs = build_poison_substrate(
        cases, seed=seed, poison_frac=cfg.poison_frac, kinds=cfg.poison_kinds)
    gold_text_for = _gold_text_for(specs, memories)

    arm_obs: dict[str, PoisonArmObs] = {}
    for arm in _ARMS:
        backend = make_backend(arm)
        backend.reset()
        # The augmented pool (clean + the committed-servable falsehoods) is loaded through the SAME
        # orchestrator-vouched seam in EVERY arm — so the falsehood is servable everywhere and the
        # headline measures serve-time SELECTION, never admission-absence. preload maps each false
        # mem-id to its "poison::<gold>" source; the agent loop resolves a served false id by
        # PROVENANCE (mem_source ∩ case.false_source_ids), never by text. No second plant — a double
        # plant would give a no-dedup store (mem0) the falsehood twice, a serve-volume confound.
        pre = preload(backend, memories, approver=_APPROVER)
        if arm == ARM_SUP:
            supersede_falsehood(backend, specs, pre, approver=_APPROVER,
                                gold_text_for=gold_text_for)
        arm_obs[arm] = run_poison_arm(backend, llms[arm], poison_cases, seat=_READER_SEAT,
                                      served_text=pre.mem_text, mem_source=pre.mem_source, arm=arm)

    scored = score_poison_arms(arm_obs, seed=seed)

    # secret floor (DISCLOSED, never headline): Hivemind refuses a credential-shaped fact at the
    # write boundary (never servable); mem0 stores+serves it. Measured as a standalone write-path
    # probe — a credential is not a recall task, and the asymmetry is a backend CAPABILITY difference.
    secret_floor_observed = measure_secret_floor(make_backend)

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
        "tau_serve_on": cfg.tau_serve_on, "tau_serve_off": cfg.tau_serve_off,
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


def measure_secret_floor(make_backend: Callable[[str], MemoryBackend]) -> dict:
    """The DISCLOSED secret-floor line: write a credential-shaped fact into a Hivemind arm and into
    mem0 and observe servability. Hivemind refuses it at the secret floor (the write never stores ⇒
    never servable); mem0 has no such floor, so it stores+serves it. ``conclusive`` requires mem0 to
    have stored it (else the probe says nothing). A standalone write-path probe — a credential is not
    a recall task, and the refusal is a backend CAPABILITY difference reported alongside, NEVER folded
    into the headline FSR. Offline a fake backend that does not model the scanner reads not-refused —
    an honest inconclusive, exactly like the anti-gaming probe."""
    cred = make_credential_text("deploy")
    hive = make_backend(ARM_OFF)
    hive.reset()
    hp = hive.propose(_READER_SEAT, cred, source_id="cred::probe")
    hive_mid = hive.commit(hp, approver=_APPROVER) if hp.proposal_id else ""
    refused_by_hive = (not hive_mid) or hive.recall(_READER_SEAT, cred).abstained
    mem0 = make_backend(ARM_MEM0)
    mem0.reset()
    mp = mem0.propose(_READER_SEAT, cred, source_id="cred::probe")
    mem0_mid = mem0.commit(mp, approver=_APPROVER) if mp.proposal_id else ""
    stored_by_mem0 = bool(mem0_mid) and not mem0.recall(_READER_SEAT, cred).abstained
    return {"refused_by_hive": bool(refused_by_hive), "stored_by_mem0": bool(stored_by_mem0),
            "conclusive": bool(stored_by_mem0)}


def _measure_promotion(cfg, *, backend_factory) -> dict:
    """Run the anti-gaming probe on a demand-tuned real server, or skip it (inconclusive, all-False)
    when an offline ``backend_factory`` is injected — the probe needs the real demand machinery, which
    a fake backend does not model. Always returns a fully-populated, JSON-stable dict."""
    if backend_factory is not None:                    # offline test path: no real demand machinery
        return {"two_seat_promoted": False, "self_demand_blocked": False, "conclusive": False}
    from hive.app.config import AutonomyConfig
    from hive.research.bench.run import _hivemind_server_factory
    autonomy = AutonomyConfig(demand_m=2, demand_tau=0.5, competitor_tau=0.85)
    factory = _hivemind_server_factory(cfg.tau_serve_on, autonomy=autonomy)
    return measure_promotion_block(
        factory,
        poison_text="the deploy region is eu-west for the billing service",
        paraphrase="which region does the billing service deploy to")


if __name__ == "__main__":                          # pragma: no cover
    raise SystemExit(main())
