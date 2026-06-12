"""P2.1 — the four-arm keystone EXPERIMENT driver (dry-run capable).

Runs the four control arms (utility / utility_off / recency / frequency) over a
hermetic store through the REAL recall pipeline + the REAL ``_rank_candidates``
seam, measures recall@k per arm, assembles a ``KeystoneSpec`` from those measured
vectors + a REAL credit-accrual sweep + REAL present-vs-ablated transfer, and applies
``run_keystone_eval``'s decision. Nothing in the spec is hand-pasted: every
number is produced by the same ranking machinery a Phase-2 production run would use —
only the *corpus* is constructed.

**PROVENANCE IS LOAD-BEARING.** On a constructed corpus this is a **DRY-RUN**: it
validates the experiment MACHINERY end-to-end (and the tests prove it can both KEEP
*and* KILL, so it is not rigged), but its verdict is **NEVER a product keep/kill** —
a real verdict requires real clawback-settled accrual clearing the readiness gate
(P2.0), which does not yet exist. ``is_product_verdict`` can only be True under
``provenance == "PRODUCTION"``; a synthetic run is *structurally* incapable of
claiming product status, so a dry-run KEEP can never be mistaken for the real thing.

Dev-time only — the P0.0 AST fence keeps ``domain``/``adapters``/``app`` from
importing ``hive.research``.
"""
from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass
from typing import Mapping, Sequence

from hive.research.eval_membrane import HashEmbedder, _EvalService
from hive.research.keystone import (
    RANKING_MODES, KeystoneResult, KeystoneSpec, _rank_candidates, run_keystone_eval,
)
from hive.research.metrics_ir import recall_at_k

log = logging.getLogger("hive.research.experiment")

_PRODUCTION = "PRODUCTION"
_DRY_RUN = "DRY-RUN-SYNTHETIC"
_DEFAULT_CREDIT = 0.9          # a confident credited posterior (factor f(0.9)=1.4 > 1.0)


# ── corpus carriers (the constructed, auditable inputs) ───────────────────────

@dataclass(frozen=True)
class Doc:
    """One memory in a task's candidate pool. ``credit`` is the utility posterior
    mean (0 ⇒ uncredited); ``ts``/``freq`` are the recency/frequency signals the
    baseline arms rank by; ``weight`` is the episode weight the utility arm scales."""
    text: str
    credit: float = 0.0
    ts: int = 0
    freq: int = 0
    weight: float = 1.0


@dataclass(frozen=True)
class Task:
    """A held-out task: a query, its candidate docs, and the index of the gold doc
    (the memory that, if surfaced in the top-k, counts as a recall@k hit)."""
    query: str
    docs: tuple[Doc, ...]
    gold: int


@dataclass(frozen=True)
class ExperimentReport:
    """The keystone verdict plus its PROVENANCE. ``is_product_verdict`` is the integrity
    flag: it is True only for a real PRODUCTION run over accrued clawback-settled
    outcomes — a synthetic dry-run is structurally pinned to False."""
    result: KeystoneResult
    spec: KeystoneSpec
    provenance: str
    is_product_verdict: bool
    note: str


# ── the real ranking machinery (recall → per-arm rerank → recall@k) ───────────

@dataclass(frozen=True)
class _Scored:
    """One task's recall candidate set + the signal maps, computed ONCE so the
    accrual sweep / transfer toggle the posteriors without re-running recall."""
    candidates: tuple
    gold_eid: int
    ts_map: dict
    freq_map: dict
    weight_map: dict
    base_credit: dict          # the task's own (eid → credit) for credited docs


def _score_task(task: Task, *, d: int, beta: float, recall_top_n: int) -> _Scored:
    """Write the task's docs to a fresh hermetic store, recall the query, and capture
    the candidate set ``[(eid, sim), ...]`` + the per-eid signal maps. The store
    dedups identical text, so the caller must keep doc texts distinct. // O(D) writes
    + one recall."""
    svc = _EvalService(embedder=HashEmbedder(d=d), d=d, h_frac_max=1.0, beta=beta,
                       recall_top_n=recall_top_n)
    eids = [svc.write(doc.text, weight=doc.weight) for doc in task.docs]
    res = svc.recall(task.query)
    candidates = tuple((h.episode_id, h.sim) for h in res.hits)
    svc.close()
    ts_map = {eids[i]: task.docs[i].ts for i in range(len(eids))}
    freq_map = {eids[i]: task.docs[i].freq for i in range(len(eids))}
    weight_map = {eids[i]: task.docs[i].weight for i in range(len(eids))}
    base_credit = {eids[i]: task.docs[i].credit
                   for i in range(len(eids)) if task.docs[i].credit > 0}
    return _Scored(candidates=candidates, gold_eid=eids[task.gold], ts_map=ts_map,
                   freq_map=freq_map, weight_map=weight_map, base_credit=base_credit)


def _recall_under(mode: str, sc: _Scored, *, posteriors: Mapping[int, float],
                  k: int) -> float:
    """recall@k of the gold after re-ranking ``sc.candidates`` under ``mode`` with the
    given posteriors. // O(n log n) rerank + O(k)."""
    ranked = _rank_candidates(
        mode, list(sc.candidates), posteriors=posteriors, ts_map=sc.ts_map,
        freq_map=sc.freq_map, weight_map=sc.weight_map)
    return recall_at_k([str(e) for e in ranked], {str(sc.gold_eid)}, k)


def _arm_vectors(scored: Sequence[_Scored], *, k: int) -> dict[str, list[float]]:
    """Per-arm per-task recall@k vectors over the SAME candidate sets — the causal
    A/B holds everything constant except the ranking signal (the arm)."""
    out: dict[str, list[float]] = {m: [] for m in RANKING_MODES}
    for sc in scored:
        for m in RANKING_MODES:
            out[m].append(_recall_under(m, sc, posteriors=sc.base_credit, k=k))
    return out


def _accrual_points(scored: Sequence[_Scored], *, k: int,
                    credit: float = _DEFAULT_CREDIT) -> tuple:
    """The REAL credit-accrual curve: as credit accrues to MORE memories, more golds
    are promoted into the top-k. At step ``v`` the first ``v`` golds are credited; the
    utility-arm mean recall is one curve point ``(v, mean_recall)``. A genuinely
    rising curve measured through the ranking machinery — not a hand-drawn fixture.
    // O(N² ) reranks (N tasks × N+1 steps), all cached recall."""
    n = len(scored)
    pts: list[tuple[float, float]] = []
    for v in range(n + 1):
        recalls = [
            _recall_under("utility", sc,
                          posteriors={sc.gold_eid: credit} if j < v else {}, k=k)
            for j, sc in enumerate(scored)
        ]
        pts.append((float(v), statistics.fmean(recalls) if recalls else 0.0))
    return tuple(pts)


def _transfer_vectors(scored: Sequence[_Scored], *, k: int,
                      credit: float = _DEFAULT_CREDIT) -> tuple[tuple, tuple]:
    """REAL within-family transfer: present = utility-arm recall WITH the gold's
    credited posterior; ablated = the SAME task with the posterior removed. A lift
    (present − ablated > 0) means the credit, not the lexical match, surfaced the
    memory. // O(N) reranks."""
    present = tuple(_recall_under("utility", sc, posteriors={sc.gold_eid: credit}, k=k)
                    for sc in scored)
    ablated = tuple(_recall_under("utility", sc, posteriors={}, k=k) for sc in scored)
    return present, ablated


# ── the driver ────────────────────────────────────────────────────────────────

def run_keystone_experiment(
    score_tasks: Sequence[Task],
    transfer_tasks: Sequence[Task],
    *,
    settled: int,
    distinct_memories: int,
    clawback_traced: bool = True,
    k: int = 5,
    d: int = 128,
    beta: float = 16.0,
    recall_top_n: int = 16,
    seed: int = 0,
    provenance: str = _DRY_RUN,
    is_product_verdict: bool = False,
    note: str = "",
) -> ExperimentReport:
    """Drive the four-arm experiment over the constructed corpus and apply the
    keystone decision. Returns the verdict + the assembled spec + its provenance.

    The integrity invariant: ``is_product_verdict`` is forced False unless
    ``provenance == "PRODUCTION"``, so a synthetic dry-run can NEVER report a product
    keep/kill. The four arm vectors, the accrual curve, and the transfer vectors are
    all measured through the real ranking machinery; only the corpus is constructed."""
    scored = [_score_task(t, d=d, beta=beta, recall_top_n=recall_top_n)
              for t in score_tasks]
    xfer = [_score_task(t, d=d, beta=beta, recall_top_n=recall_top_n)
            for t in transfer_tasks]

    arms = _arm_vectors(scored, k=k)
    accrual = _accrual_points(scored, k=k)
    present, ablated = _transfer_vectors(xfer, k=k)

    spec = KeystoneSpec(
        utility_recall=tuple(arms["utility"]),
        recency_recall=tuple(arms["recency"]),
        frequency_recall=tuple(arms["frequency"]),
        utility_off_recall=tuple(arms["utility_off"]),
        accrual_points=accrual, transfer_present=present, transfer_ablated=ablated,
        settled=int(settled), distinct_memories=int(distinct_memories),
        clawback_traced=bool(clawback_traced))
    result = run_keystone_eval(spec, seed=seed)

    real = bool(is_product_verdict) and provenance == _PRODUCTION
    log.info("keystone.experiment provenance=%s product=%s win=%s killed=%s "
             "inconclusive=%s settled=%d distinct=%d", provenance, real, result.win,
             result.killed, result.inconclusive, settled, distinct_memories)
    return ExperimentReport(result=result, spec=spec, provenance=provenance,
                            is_product_verdict=real, note=note)


# ── constructed corpora (clearly synthetic; the dry-run inputs) ───────────────

_Q_TOKENS = ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot")
_QUERY = " ".join(_Q_TOKENS)


def _keep_task(i: int, *, n_distractors: int = 6) -> Task:
    """A task where UTILITY carries the signal the baselines miss: the gold is an OLD,
    RARELY-recalled, MODERATE-sim memory that is CREDITED; the distractors are NEWER,
    MORE-FREQUENT, HIGHER-sim, and UNcredited. So recency/frequency/utility_off all
    rank ≥5 distractors above the gold (gold out of top-5), while the utility arm's
    credit (factor 1.4 > 1.0) lifts the gold to the top."""
    # gold: shares only 3 of 6 query tokens ⇒ lower sim than the full-overlap distractors.
    gold = Doc(text=f"alpha bravo charlie goldmemory{i}", credit=_DEFAULT_CREDIT,
               ts=0, freq=0, weight=1.0)
    distractors = tuple(
        Doc(text=f"{_QUERY} distractor{i}x{j}", credit=0.0, ts=10 + j, freq=5 + j,
            weight=1.0)
        for j in range(n_distractors))
    return Task(query=_QUERY, docs=(gold,) + distractors, gold=0)


def _kill_task(i: int, *, n_distractors: int = 6) -> Task:
    """A task where UTILITY provides NO lift over recency/frequency: the gold is ALSO
    the newest, most-frequent, highest-sim memory, so every arm surfaces it. The
    utility arm ties recency ⇒ utility beats nothing ⇒ the pre-committed KILL."""
    gold = Doc(text=f"{_QUERY} goldmemory{i}", credit=_DEFAULT_CREDIT,
               ts=99, freq=99, weight=1.0)
    distractors = tuple(
        Doc(text=f"alpha bravo distractor{i}x{j}", credit=0.0, ts=1 + j, freq=1 + j,
            weight=1.0)
        for j in range(n_distractors))
    return Task(query=_QUERY, docs=(gold,) + distractors, gold=0)


def build_keep_corpus(n_tasks: int = 12, n_transfer: int = 8) -> tuple[list[Task], list[Task]]:
    """A synthetic corpus where utility genuinely carries the signal ⇒ a dry-run KEEP."""
    return ([_keep_task(i) for i in range(n_tasks)],
            [_keep_task(1000 + i) for i in range(n_transfer)])


def build_kill_corpus(n_tasks: int = 12, n_transfer: int = 8) -> tuple[list[Task], list[Task]]:
    """A synthetic corpus where utility adds nothing over recency ⇒ a dry-run KILL —
    proof the harness discriminates and is not rigged to always keep."""
    return ([_kill_task(i) for i in range(n_tasks)],
            [_kill_task(1000 + i) for i in range(n_transfer)])
