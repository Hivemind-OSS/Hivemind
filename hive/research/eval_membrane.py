"""M10 — the eval membrane: a labeled retrieval+abstention oracle
(``run_longmemeval``) + a deterministic capture→replay regression gate
(``export_baseline``/``replay``/``admit``), PORT+SIMPLIFY-d from the reference and
**rewired to the hive ports** (EpisodeStore + RecallPipeline) instead of the old
AgentCortexService. LLM-free on the load-bearing path; brings its own corpus.

Dev-time only — the runtime server (``hive.domain`` / ``hive.adapters`` /
``hive.app``) never imports ``hive.research`` (the P0.0 AST fence; the membrane
imports *down* into the public surface, the runtime never imports *up*).

Two surfaces:

1. ``run_longmemeval(dataset)`` — for each question builds a FRESH hermetic store
   (an independent ``:memory:`` DB — no cross-question leakage), writes that
   question's haystack, recalls, and scores the ranking with the pure
   ``metrics_ir`` functions. Two contracts on one surface: the **ranker pass**
   (gate OFF — measures "is the gold in top-k") and the **abstention pass** (gate
   ON at the production floor — measures the EMPTY refusal). **[C2]**: every scored
   AND abstention question also emits a continuous confidence ``1 - H/ln(N_eff)``
   plus its ``is_miss`` event, so the §6.1 #2 AUROC gate is proven end-to-end on
   *oracle output*, not a hand-built fixture.

2. ``export_baseline`` / ``replay`` / ``admit`` — capture each query's retrieved
   content-hash set + latency to NDJSON, then re-run on a changed system and report
   mean Jaccard@k, top-1 stability, latency Δ. ``admit`` is the eval-gated
   admission decision; **[C3]**: it tests ``mean_jaccard >= champion_floor +
   min_gain`` with a GENESIS floor of ``0.0`` (not the reference's self-jaccard
   ``1.0`` that made any positive ``min_gain`` arithmetically unreachable), and
   **fails CLOSED on zero signal** (``n==0 ⇒ False``).

The Selective-Forgetting scorer and the LongMemEval ability rollup are DROPPED
(§10: supersession/prune and the LME-paper ability map are out of the episodic MVP).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import statistics
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional, Sequence, Union

import numpy as np

from hive.adapters.index_exhaustive import ExhaustiveCosineIndex
from hive.adapters.sqlite_db import connect
from hive.adapters.store_sqlite import SqliteEpisodeStore
from hive.adapters.utility_store_sqlite import SqliteUtilityStore
from hive.domain.models import (
    CONFIDENT, EMPTY_NO_DATA, AgentContext, RecallResult, content_hash,
)
from hive.domain.recall import NormalizedEntropyGate, RecallPipeline
from hive.domain.surfacer import UtilitySurfacer
from hive.research.metrics_ir import (
    jaccard_at_k, mrr, ndcg_at_k, precision_at_k, recall_at_k,
)

log = logging.getLogger("hive.research.eval_membrane")

# Eval-only: the ranker pass disables the entropy gate so the harness scores the
# raw ranked candidates (the ranker), not the production inject-confidence decision.
# h_frac_max=1.0 ⇒ suppress iff entropy_norm > 1.0, impossible after the [0,1] clamp.
_RANKER_H_FRAC_MAX = 1.0

# The abstention pass runs the gate ENABLED at the production floor (§6.1 #2/#3).
_ABSTENTION_TYPE = "abstention"
_DEFAULT_ABSTENTION_H_FRAC_MAX = 0.5

# LoCoMo category → LME question type. Category 5 (adversarial / unanswerable)
# routes to abstention so the harness scores the refusal, not a hallucination.
_LOCOMO_CATEGORY_TYPE: dict = {
    1: "multi-session", 2: "temporal-reasoning", 3: "knowledge-update",
    4: "single-session-user", 5: _ABSTENTION_TYPE,
}

# The stamp trailer the §6.3 de-confounding rail strips (the prior eval-artifact
# label leak (a)). Default key matches producer.stamp_trailer ("Hive-Trace").
_DEFAULT_TRAILER_KEY = "Hive-Trace"
_TOKEN_PAT = re.compile(r"[a-z0-9]+")


# ── HashEmbedder — deterministic, dependency-light dev/eval embedder ──────────

class HashEmbedder:
    """A signed feature-hashing bag-of-words embedder: deterministic, torch-free,
    and lexically meaningful (a query sharing tokens with a doc gets a high cosine).

    Conforms to ``EmbeddingProvider`` (``d``, ``w_version``, ``encode``,
    ``encode_batch``). This is the membrane's fast hermetic embedder; a real corpus
    run injects the production ST embedder instead. // encode: O(T) for T tokens."""

    def __init__(self, d: int = 256, w_version: int = 1) -> None:
        self.d = int(d)
        self.w_version = int(w_version)

    def _vec(self, text: str) -> np.ndarray:
        v = np.zeros(self.d, dtype=np.float32)
        for tok in _TOKEN_PAT.findall(text.lower()):
            h = int.from_bytes(hashlib.sha256(tok.encode()).digest()[:8], "big")
            bucket = h % self.d
            sign = 1.0 if (h >> 63) & 1 else -1.0   # signed hashing reduces collisions
            v[bucket] += sign
        norm = float(np.linalg.norm(v))
        if norm == 0.0:
            # token-less text: derive the unit basis FROM the text (not a fixed e_0) so
            # two distinct token-less inputs do not collide to the same vector, and a
            # token-less query cannot align to cosine 1.0 with an arbitrary real doc
            # (which would fabricate a CONFIDENT surface in the abstention path).
            bucket = int.from_bytes(hashlib.sha256(text.encode()).digest()[:8], "big") % self.d
            v[bucket] = 1.0                          # deterministic, finite, unit-norm
            return v
        return v / norm

    def encode(self, text: str) -> np.ndarray:
        return self._vec(text)

    def encode_batch(self, texts: Sequence[str]) -> np.ndarray:
        return np.stack([self._vec(t) for t in texts], axis=0)


# ── hermetic store wired to the real hive ports ──────────────────────────────

class _EvalService:
    """A fresh hermetic store (independent ``:memory:`` DB) wired to the real hive
    ports — the membrane reaches the system the way an agent would: stage → approve
    → recall → resolve content-hash. The ranker pass disables the gate; the
    abstention pass enables it at the production floor. Surfacer is Phase-1 inert
    (``enabled=False``) unless a caller flips it (the keystone never touches the
    runtime surfacer — it ranks arms itself)."""

    def __init__(self, *, embedder, d: int, h_frac_max: float, beta: float,
                 recall_top_n: int, tenant: str = "lme", isolation_frac: float = 0.0,
                 surfacer_enabled: bool = False, epsilon_explore: float = 0.1,
                 f_min: float = 0.5, f_max: float = 1.5, db_path: str = ":memory:") -> None:
        import random
        conn = connect(db_path)
        self._conn = conn
        self.tenant = str(tenant)
        self.embedder = embedder
        self.util = SqliteUtilityStore(conn)                       # FIRST (isolation stamp seam)
        self.index = ExhaustiveCosineIndex(int(d))
        self.store = SqliteEpisodeStore(conn, self.index, isolation_frac=isolation_frac)
        gate = NormalizedEntropyGate(float(h_frac_max), float(beta))
        surfacer = UtilitySurfacer(
            enabled=surfacer_enabled, epsilon_explore=epsilon_explore,
            f_min=f_min, f_max=f_max, rng=random.Random(0))
        self.pipeline = RecallPipeline(
            embedder=embedder, index=self.index, gate=gate, surfacer=surfacer,
            reader=self.store, utility_store=self.util,
            recall_top_n=int(recall_top_n))
        self._ts = 0

    def write(self, text: str, *, weight: float = 1.0) -> int:
        """Stage + approve one trusted dev-corpus doc (recall reads approved only)."""
        eid, deduped = self.store.stage(
            text=text, weight=weight, source="eval", tags="", proposed_by="eval",
            tenant_id=self.tenant, ts=self._ts)
        self._ts += 1
        if not deduped:
            self.store.approve(eid, "eval", self.embedder.encode(text),
                               expected_version=0, approved_ts=0)
        return eid

    def recall(self, query: str, *, ctx: Optional[AgentContext] = None) -> RecallResult:
        return self.pipeline.recall(query, agent_id=self.tenant,
                                    agent_ctx=ctx or AgentContext())

    def close(self) -> None:
        """Release the sqlite connection. The oracle builds one hermetic store per
        question, so closing each keeps connections from accumulating across a large
        corpus run (RecallHit carries its text, so reads after close are safe)."""
        try:
            self._conn.close()
        except Exception:                            # noqa: BLE001 — best-effort cleanup
            pass


def _recalled_content_hashes(svc: _EvalService, query: str,
                             ctx: Optional[AgentContext] = None) -> tuple[list[str], float]:
    """Recall ``query`` and resolve the surfaced hits to their content-hash hex in
    rank order; returns ``(hashes, latency_ms)``. Content-hash (not eid) is the
    cross-run-stable identifier capture→replay compares."""
    t0 = time.perf_counter()
    res = svc.recall(query, ctx=ctx)
    latency_ms = (time.perf_counter() - t0) * 1000.0
    return [content_hash(h.text) for h in res.hits], latency_ms


# ── LongMemEval carriers ─────────────────────────────────────────────────────

@dataclass
class LMEQuestion:
    question_id: str
    question: str
    haystack: list[dict]          # [{"id": str, "text": str}, ...]
    answer_doc_ids: list[str]
    qtype: str = "default"


@dataclass
class AbstentionScore:
    """Ternary abstention outcome. A false-premise question is ``correct`` iff
    recall returns NO surface (ABSTAIN/EMPTY) with the gate ENABLED; ``fail`` iff
    it confidently surfaces a non-existent answer. Never a pass-by-default."""
    n: int = 0
    correct: int = 0
    fail: int = 0


@dataclass
class LMEResult:
    p_at_5: float
    r_at_5: float
    mrr: float
    ndcg_at_5: float
    n: int                        # questions scored on the retrieval metrics
    by_type: dict = field(default_factory=dict)
    abstention: AbstentionScore = field(default_factory=AbstentionScore)
    skipped: dict = field(default_factory=dict)
    total_loaded: int = 0
    # [C2] live AUROC wiring: one continuous confidence + is_miss event per scored
    # AND abstention question, so abstention_auroc(scores, is_miss) is proven on
    # oracle output (len == scored + abstention.n).
    abstention_scores: list = field(default_factory=list)
    is_miss: list = field(default_factory=list)

    @property
    def scored(self) -> int:
        """Questions scored on the retrieval metrics (alias of ``n``)."""
        return self.n


def _first(d: dict, *keys, default=None):
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def _normalize_doc(raw, idx: int) -> Optional[dict]:
    """Normalize one haystack entry into {"id","text"}; None if it has no text."""
    if isinstance(raw, str):
        return {"id": str(idx), "text": raw}
    if not isinstance(raw, dict):
        return None
    text = _first(raw, "text", "content", "body", "value")
    if not text:
        return None
    doc_id = _first(raw, "id", "doc_id", "session_id", "sid", default=str(idx))
    return {"id": str(doc_id), "text": str(text)}


def _question_from_obj(obj: dict, lineno: int) -> Optional[LMEQuestion]:
    question = _first(obj, "question", "query", "q")
    raw_hay = _first(obj, "haystack", "docs", "documents", "sessions", default=[])
    if not question or not raw_hay:
        return None
    haystack = []
    for i, raw in enumerate(raw_hay):
        doc = _normalize_doc(raw, i)
        if doc is not None:
            haystack.append(doc)
    answer_ids = _first(obj, "answer_doc_ids", "relevant", "evidence_ids",
                        "answer_ids", "gold_doc_ids", default=[])
    qid = _first(obj, "question_id", "id", "qid", default=str(lineno))
    qtype = _first(obj, "type", "question_type", "qtype", default="default")
    return LMEQuestion(
        question_id=str(qid), question=str(question), haystack=haystack,
        answer_doc_ids=[str(a) for a in answer_ids], qtype=str(qtype))


def load_longmemeval(dataset_path: str) -> list[LMEQuestion]:
    """Load a JSONL LongMemEval(-shaped) dataset, tolerant of key aliases. Skips
    blank lines and entries with no question or no haystack; an unparseable line is
    a WARN-and-continue, never an abort (§6 boundary logging)."""
    questions: list[LMEQuestion] = []
    with open(dataset_path, "r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                log.warning("eval: skipping unparseable JSONL line %d", lineno)
                continue
            q = _question_from_obj(obj, lineno)
            if q is not None:
                questions.append(q)
    return questions


def _coerce_questions(dataset: "Union[str, Iterable]") -> list[LMEQuestion]:
    """Accept a JSONL path, a list of ``LMEQuestion``, or a list of dict rows."""
    if isinstance(dataset, str):
        return load_longmemeval(dataset)
    out: list[LMEQuestion] = []
    for i, item in enumerate(dataset, start=1):
        if isinstance(item, LMEQuestion):
            out.append(item)
        elif isinstance(item, dict):
            q = _question_from_obj(item, i)
            if q is not None:
                out.append(q)
        else:
            raise TypeError(f"unsupported dataset row type: {type(item).__name__}")
    return out


def run_longmemeval(
    dataset: "Union[str, Iterable]",
    *,
    k: int = 5,
    retrieval_only: bool = True,
    embedder=None,
    d: int = 256,
    recall_top_n: Optional[int] = None,
    h_frac_max: float = _DEFAULT_ABSTENTION_H_FRAC_MAX,
    beta: float = 16.0,
    tenant: str = "lme",
    on_question: Optional[Callable[[LMEQuestion, list[str]], None]] = None,
) -> LMEResult:
    """Run LongMemEval retrieval-only over fresh per-question hermetic stores.

    Routing — every loaded question lands in exactly one bucket, with the closure
    invariant ``scored + abstention.n + Σskipped == total_loaded`` ASSERTED at the
    end (nothing silently vanishes):

    - ``qtype == "abstention"`` → gate ENABLED at ``h_frac_max``; ternary scoring:
      NO surface (ABSTAIN/EMPTY) ⇒ ``correct`` refusal, a confident surface ⇒ ``fail``.
    - empty ``answer_doc_ids`` (non-abstention) → ``skipped["no_relevant"]``.
    - otherwise → scored on precision/recall/mrr/ndcg @k (ranker pass, gate OFF).

    **[C2]**: every scored AND abstention question appends a continuous confidence
    ``1 - entropy_norm`` to ``abstention_scores`` and its ``is_miss`` event (gold
    not in top-k; for abstention questions the gate *should* abstain ⇒ ``is_miss``
    is True), so ``abstention_auroc(result.abstention_scores, result.is_miss)`` is
    proven on live oracle output. ``retrieval_only=False`` RAISES (LLM-free path).
    """
    if not retrieval_only:
        raise NotImplementedError(
            "only retrieval_only is built (the LLM-free load-bearing path); "
            "answer-generation scoring needs an LLM and is out of scope.")
    if embedder is None:
        embedder = HashEmbedder(d=d)
    top_n = int(recall_top_n) if recall_top_n is not None else max(8, k)

    questions = _coerce_questions(dataset)
    rows: list[tuple[float, float, float, float]] = []
    per_type: dict[str, list[tuple[float, float, float, float]]] = {}
    abstention = AbstentionScore()
    skipped: dict[str, int] = {}
    abstention_scores: list[float] = []
    is_miss: list[bool] = []

    def _conf(res: RecallResult) -> float:
        # continuous confidence proxy: HIGHER ⇒ less likely to abstain. The gate's
        # normalized entropy H/ln(N_eff) ∈ [0,1]; confidence = 1 − that. EMPTY_NO_DATA
        # is the OVERLOAD trap: its entropy_norm is 0.0, but that means "no data — the
        # gate never computed a confidence", NOT "maximally confident". A no-data
        # refusal must read as ZERO confidence (a should-abstain event), or the AUROC
        # gate sees an EMPTY refusal as the most confident-wrong point (signal inverted).
        if res.state == EMPTY_NO_DATA:
            return 0.0
        return 1.0 - float(res.entropy_norm)

    for q in questions:
        relevant = set(q.answer_doc_ids)

        # --- abstention bucket: gate ENABLED, ternary EMPTY scoring -------------
        if q.qtype == _ABSTENTION_TYPE:
            svc = _EvalService(embedder=embedder, d=d, h_frac_max=h_frac_max,
                               beta=beta, recall_top_n=top_n, tenant=tenant)
            for doc in q.haystack:
                svc.write(doc["text"])
            res = svc.recall(q.question)
            retrieved = [content_hash(h.text) for h in res.hits]
            svc.close()
            if on_question is not None:
                on_question(q, retrieved)
            abstention.n += 1
            abstention_scores.append(_conf(res))
            is_miss.append(True)                       # a false premise SHOULD abstain
            if res.state != CONFIDENT:
                abstention.correct += 1                # correct refusal
            else:
                abstention.fail += 1                   # surfaced a non-existent answer
            continue

        # --- skipped bucket: undefined relevance (counted, not silent) ----------
        if not relevant:
            skipped["no_relevant"] = skipped.get("no_relevant", 0) + 1
            continue

        # --- scored bucket: ranker pass, gate OFF -------------------------------
        svc = _EvalService(embedder=embedder, d=d, h_frac_max=_RANKER_H_FRAC_MAX,
                           beta=beta, recall_top_n=top_n, tenant=tenant)
        # content_hash → ALL doc ids sharing that text. The store dedups identical text
        # to one episode, so a single retrieved hash may stand for several haystack
        # docs; mapping it to a SINGLE id (last-writer-wins) would erase a gold whose
        # text collides with a later distractor → a real hit scored as a full miss.
        hash_to_ids: dict[str, list[str]] = {}
        for doc in q.haystack:
            svc.write(doc["text"])
            hash_to_ids.setdefault(content_hash(doc["text"]), []).append(doc["id"])
        res = svc.recall(q.question)
        retrieved_hashes = [content_hash(h.text) for h in res.hits]
        svc.close()
        if on_question is not None:
            on_question(q, retrieved_hashes)
        # collision-safe resolution: if any doc id sharing this retrieved text is gold,
        # resolve to it (a shared-text gold/distractor must never erase the gold).
        retrieved_docs: list[str] = []
        for h in retrieved_hashes:
            ids = hash_to_ids.get(h)
            if not ids:
                continue
            gold_id = next((i for i in ids if i in relevant), None)
            retrieved_docs.append(gold_id if gold_id is not None else ids[0])
        r_at_k = recall_at_k(retrieved_docs, relevant, k)
        scored = (
            precision_at_k(retrieved_docs, relevant, k),
            r_at_k,
            mrr(retrieved_docs, relevant),
            ndcg_at_k(retrieved_docs, relevant, k),
        )
        rows.append(scored)
        per_type.setdefault(q.qtype, []).append(scored)
        abstention_scores.append(_conf(res))
        is_miss.append(r_at_k == 0.0)                  # gold not in top-k ⇒ a miss

    # Closure invariant: every loaded question accounted for exactly once.
    total = len(questions)
    accounted = len(rows) + abstention.n + sum(skipped.values())
    assert accounted == total, (
        f"eval accounting leak: scored={len(rows)} + abstention={abstention.n} "
        f"+ skipped={sum(skipped.values())} = {accounted} != total_loaded={total}")

    def mean_col(data, col):
        return statistics.fmean(r[col] for r in data)

    by_type = {
        t: {"n": len(v), "p_at_k": mean_col(v, 0), "r_at_k": mean_col(v, 1),
            "mrr": mean_col(v, 2), "ndcg_at_k": mean_col(v, 3)}
        for t, v in per_type.items()
    }
    if not rows:
        return LMEResult(0.0, 0.0, 0.0, 0.0, 0, by_type, abstention, skipped, total,
                         abstention_scores, is_miss)
    return LMEResult(
        p_at_5=mean_col(rows, 0), r_at_5=mean_col(rows, 1), mrr=mean_col(rows, 2),
        ndcg_at_5=mean_col(rows, 3), n=len(rows), by_type=by_type,
        abstention=abstention, skipped=skipped, total_loaded=total,
        abstention_scores=abstention_scores, is_miss=is_miss)


# ── LoCoMo adapter ───────────────────────────────────────────────────────────

def locomo_to_lme_rows(sample: dict) -> list[dict]:
    """Adapt one LoCoMo sample into LME rows (the doc-level shape
    ``run_longmemeval`` consumes). The adversarial slice (category 5 / unanswerable)
    routes to ``qtype="abstention"`` with empty answers, so it is scored on the
    refusal path rather than as a hallucinated retrieval."""
    haystack: list[dict] = []
    conv = sample.get("conversation", sample.get("sessions", {})) or {}
    sessions = conv.values() if isinstance(conv, dict) else conv
    for turns in sessions:
        if not isinstance(turns, list):
            continue
        for turn in turns:
            if not isinstance(turn, dict):
                continue
            did = _first(turn, "dia_id", "id", "turn_id", "sid")
            text = _first(turn, "text", "clean_text", "content", "value")
            if not did or not text:
                continue
            speaker = _first(turn, "speaker", "role", default="")
            doc_text = f"{speaker}: {text}".strip(": ") if speaker else str(text)
            haystack.append({"id": str(did), "text": doc_text})

    rows: list[dict] = []
    for qa in sample.get("qa", sample.get("questions", [])) or []:
        q = _first(qa, "question", "query", "q")
        if not q:
            continue
        evidence = _first(qa, "evidence", "evidence_ids", "answer_doc_ids", default=[]) or []
        category = qa.get("category")
        answer = _first(qa, "answer", "a")
        is_adversarial = (
            category == 5 or bool(qa.get("adversarial"))
            or (not evidence and (answer in (None, "", "Not mentioned in the conversation",
                                             "No information available"))))
        qtype = (_ABSTENTION_TYPE if is_adversarial
                 else _LOCOMO_CATEGORY_TYPE.get(category, "multi-session"))
        rows.append({
            "question_id": str(_first(qa, "id", "qid", default=str(q)[:24])),
            "question": str(q), "type": qtype, "haystack": haystack,
            "answer_doc_ids": [] if is_adversarial else [str(e) for e in evidence],
        })
    return rows


def load_locomo(path: str) -> list[dict]:
    """Load a LoCoMo JSON file (a list of samples) and flatten to LME rows."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    samples = data if isinstance(data, list) else data.get("samples", [data])
    out: list[dict] = []
    for s in samples:
        out.extend(locomo_to_lme_rows(s))
    return out


# ── §6.3 de-confounding rails (BUILD-NEW) ────────────────────────────────────

def strip_stamped_tokens(text: str, *, trailer_key: str = _DEFAULT_TRAILER_KEY) -> str:
    """Remove the commit-stamp label leak (eval artifact (a)): every stamped trailer
    is deleted so a stamped trace id can never leak into the corpus as a retrievable
    token and fake a match. The exact stamp format is pinned here (part of the
    contract): the trailer key, ``:``, and the WHOLE value run to end-of-line — the
    producer's stamp is multi-token (``Hive-Trace: <T1> <T2> ...``, models.py) and a
    trailer is conventionally its own line, so EOL is the correct boundary (matching
    one ``\\S+`` token would leak T2, T3, …). Case-INSENSITIVE because git matches
    trailer keys case-insensitively, so a lowercase ``hive-trace:`` stamp must also be
    stripped. // O(len)."""
    pat = re.compile(rf"{re.escape(trailer_key)}\s*:[^\n\r]*", re.IGNORECASE)
    return pat.sub("", text)


def assert_clean_store(svc: _EvalService) -> None:
    """Guard eval artifact (c): refuse to run a measurement on a non-empty store
    (a re-consolidated/persisted store would leak prior state into the result).
    RAISES ``ValueError`` with the live episode count if anything is present."""
    approved, pending = svc.store.counts()
    if approved or pending:
        raise ValueError(
            f"assert_clean_store: store is not empty (approved={approved}, "
            f"pending={pending}) — a hermetic measurement requires a fresh store")


def assert_exact_path(index) -> None:
    """Guard the §4.3 ``approx_threshold`` trap (eval artifact (a)): refuse to
    measure on a non-authoritative (ANN) index, where recall can silently → 0 once
    N crosses a threshold. RAISES ``ValueError`` naming the backend if it is not
    authoritative-exhaustive."""
    if not index.is_authoritative():
        raise ValueError(
            f"assert_exact_path: index {type(index).__name__} is not authoritative "
            "(ANN path engaged) — a measurement here can silently read recall→0")


# ── capture → replay + admission (PORT, [C3] champion_floor) ─────────────────

@dataclass
class ReplayReport:
    mean_jaccard: float
    top1_stability: float
    latency_delta_ms: float       # mean(live) - mean(baseline)
    n: int
    regressions: list = field(default_factory=list)


def export_baseline(out_path: str, queries: list[str], svc: _EvalService, *,
                    k: int = 5, ctx: Optional[AgentContext] = None) -> None:
    """Capture, per query, its top-k retrieved content-hash list + latency to NDJSON.
    A dev/CI artifact that DOES carry query text (for replayability) — distinct from
    the §6 text-free telemetry sink. ``query_hash`` is a stable label."""
    import os
    parent = os.path.dirname(out_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        for query in queries:
            hashes, latency_ms = _recalled_content_hashes(svc, query, ctx)
            f.write(json.dumps({
                "query_hash": content_hash(query),
                "query": query,
                "retrieved": hashes[:k] if k else hashes,
                "latency_ms": latency_ms,
            }) + "\n")


def replay(baseline_path: str, svc: _EvalService, *, k: int = 5,
           max_regressions: int = 20, ctx: Optional[AgentContext] = None) -> ReplayReport:
    """Re-run a captured baseline's queries on the (possibly changed) ``svc`` and
    report retrieval drift: mean Jaccard@k, top-1 stability, latency Δ. An
    empty/unreplayable baseline → ``ReplayReport(1,1,0,0,[])``. A baseline entry
    missing ``query`` (text-free) is a WARN-and-skip (counted, not silently stable)."""
    entries: list[dict] = []
    with open(baseline_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    if not entries:
        return ReplayReport(1.0, 1.0, 0.0, 0, [])

    jaccards: list[float] = []
    stable = 0
    base_latencies: list[float] = []
    live_latencies: list[float] = []
    regressions: list[dict] = []
    for e in entries:
        query = e.get("query")
        base_retrieved = list(e.get("retrieved", []))
        if query is None:
            log.warning("replay: baseline entry has no 'query'; cannot re-run")
            continue
        # accumulate the baseline latency ONLY for re-run entries, so base and live
        # latencies are over the IDENTICAL paired population — a text-free skip must not
        # leave its baseline latency in the mean with no live counterpart (which would
        # corrupt, and could sign-flip, the latency-delta regression signal).
        base_latencies.append(float(e.get("latency_ms", 0.0)))
        live_retrieved, live_lat = _recalled_content_hashes(svc, query, ctx)
        live_latencies.append(live_lat)
        j = jaccard_at_k(base_retrieved, live_retrieved, k)
        jaccards.append(j)
        base_top1 = base_retrieved[0] if base_retrieved else None
        live_top1 = live_retrieved[0] if live_retrieved else None
        if base_top1 == live_top1:
            stable += 1
        if j < 1.0:
            regressions.append({
                "query_hash": e.get("query_hash"), "jaccard": j,
                "baseline_top1": base_top1, "live_top1": live_top1})

    n = len(jaccards)
    if n == 0:
        return ReplayReport(1.0, 1.0, 0.0, 0, [])
    regressions.sort(key=lambda r: r["jaccard"])
    latency_delta = (statistics.fmean(live_latencies) if live_latencies else 0.0) \
        - (statistics.fmean(base_latencies) if base_latencies else 0.0)
    return ReplayReport(
        mean_jaccard=statistics.fmean(jaccards), top1_stability=stable / n,
        latency_delta_ms=latency_delta, n=n, regressions=regressions[:max_regressions])


def admit_decision(report: ReplayReport, *, min_gain: float = 0.0,
                   champion_floor: float = 0.0, max_regressions: int = 0) -> bool:
    """The eval-gated admission predicate ([C3]). Admits iff EVERY condition holds:

    1. ``report.n > 0`` — at least one query was replayed. An empty/unreplayable
       baseline NEVER auto-passes (anti-ERROR_MASKING — a gate green on zero signal
       is decorative). **Fails CLOSED.**
    2. ``report.mean_jaccard >= champion_floor + min_gain`` — the candidate's
       retrieval agreement clears the incumbent's MEASURED floor plus a required
       margin. ``champion_floor`` is the incumbent's measured agreement floor
       (genesis ``0.0``, NOT the reference's self-jaccard ``1.0`` that made any
       positive ``min_gain`` arithmetically unreachable since Jaccard ≤ 1.0). With
       ``champion_floor=0.0`` a positive ``min_gain`` is reachable and LIVE.
    3. ``len(report.regressions) <= max_regressions`` — bounded per-query drift.
    """
    if report.n <= 0:
        log.warning("admission gate: 0 queries replayed (empty/unreplayable "
                    "baseline); refusing to admit (fail-closed)")
        return False
    if report.mean_jaccard < champion_floor + min_gain:
        log.info("admission gate: mean_jaccard %.4f < floor %.4f + min_gain %.4f; "
                 "rejecting", report.mean_jaccard, champion_floor, min_gain)
        return False
    if len(report.regressions) > max_regressions:
        log.info("admission gate: %d regressing queries > max_regressions %d; "
                 "rejecting", len(report.regressions), max_regressions)
        return False
    log.info("admission gate: admitted (mean_jaccard=%.4f >= %.4f, regressions=%d "
             "<= %d, n=%d)", report.mean_jaccard, champion_floor + min_gain,
             len(report.regressions), max_regressions, report.n)
    return True


def admit(champion_baseline_path: str, candidate_svc: _EvalService, *, k: int = 5,
          min_gain: float = 0.0, champion_floor: float = 0.0, max_regressions: int = 0,
          ctx: Optional[AgentContext] = None) -> bool:
    """Replay the CHAMPION's captured baseline against the CANDIDATE served state
    and decide admission via ``admit_decision`` ([C3]). Returns True ⇒ admit, False
    ⇒ keep the incumbent; fails CLOSED on a degenerate baseline (n==0)."""
    report = replay(champion_baseline_path, candidate_svc, k=k,
                    max_regressions=max_regressions + 1,  # +1 so we can SEE one over budget
                    ctx=ctx)
    return admit_decision(report, min_gain=min_gain, champion_floor=champion_floor,
                          max_regressions=max_regressions)
