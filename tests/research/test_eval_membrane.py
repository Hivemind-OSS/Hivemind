"""P1.10 — M10 oracle (run_longmemeval, rewired to hive ports) + [C2] live AUROC
wiring + capture→replay + [C3] admit + §6.3 de-confounding rails.

Locked assertions: docs/05-BUILD-PLAN.md §P1.10(a) + docs/03-modules/M10-eval.md §2.3/§2.4/§8.
"""
from __future__ import annotations

import pytest

from hive.research.eval_membrane import (
    HashEmbedder, LMEQuestion, ReplayReport, _EvalService, admit, admit_decision,
    assert_clean_store, assert_exact_path, export_baseline, load_longmemeval,
    locomo_to_lme_rows, replay, run_longmemeval, strip_stamped_tokens,
)
from hive.research.metrics_ir import abstention_auroc


# ── corpus helpers: lexically-distinct docs so the HashEmbedder retrieves gold ──
def _doc(doc_id: str, text: str) -> dict:
    return {"id": doc_id, "text": text}


def _q(qid, question, haystack, answers, qtype="default") -> LMEQuestion:
    return LMEQuestion(question_id=qid, question=question, haystack=haystack,
                       answer_doc_ids=answers, qtype=qtype)


# A small corpus where each query shares its salient tokens with exactly one gold
# doc (high cosine) and little with the distractors.
def _gold_question(qid, gold_id, gold_text, query, *, distractors, qtype="default"):
    hay = [_doc(gold_id, gold_text)] + [_doc(f"{qid}-d{i}", t) for i, t in enumerate(distractors)]
    return _q(qid, query, hay, [gold_id], qtype=qtype)


_DISTRACTORS = [
    "the quarterly budget spreadsheet was reconciled on friday afternoon",
    "weather patterns over the pacific shifted the migration of seabirds",
    "the orchestra tuned their instruments before the evening performance",
]


# ── oracle happy path + leakage + closure ─────────────────────────────────────
def test_run_longmemeval_scores_and_finds_gold():
    qs = [
        _gold_question("q1", "g1",
                       "the postgres connection pool was exhausted under load spikes",
                       "why did the postgres connection pool get exhausted under load",
                       distractors=_DISTRACTORS),
        _gold_question("q2", "g2",
                       "redis eviction dropped queued video jobs during the cache flush",
                       "what made redis eviction drop the queued video jobs",
                       distractors=_DISTRACTORS),
    ]
    res = run_longmemeval(qs, k=5)
    assert res.n == 2 and res.total_loaded == 2
    assert res.r_at_5 >= 0.5          # both golds retrieved in top-5


def test_leakage_each_question_uses_a_fresh_store():
    # q2's gold text is q1's distractor token salad; if stores leaked, q2 could match
    # q1's docs. A fresh per-question store makes each recall see only its own haystack.
    seen: list[tuple[str, list[str]]] = []
    qs = [
        _gold_question("q1", "g1", "alpha bravo charlie delta echo",
                       "alpha bravo charlie", distractors=["zulu yankee xray"]),
        _gold_question("q2", "g2", "foxtrot golf hotel india juliet",
                       "foxtrot golf hotel", distractors=["zulu yankee xray"]),
    ]
    run_longmemeval(qs, k=5, on_question=lambda q, r: seen.append((q.question_id, r)))
    assert len(seen) == 2                          # each question scored once


def test_closure_invariant_accounts_every_question():
    qs = [
        _gold_question("scored", "g", "kafka rebalanced partitions mid-stream",
                       "kafka rebalanced partitions", distractors=_DISTRACTORS),
        _q("norel", "a question with no labeled gold", [_doc("x", "irrelevant doc")], []),
        _gold_question("abst", "g2", "a real memory about tls handshakes",
                       "what is the secret launch code for the mainframe",
                       distractors=_DISTRACTORS, qtype="abstention"),
    ]
    res = run_longmemeval(qs, k=5)
    assert res.scored + res.abstention.n + sum(res.skipped.values()) == res.total_loaded == 3
    assert res.skipped.get("no_relevant") == 1
    assert res.abstention.n == 1


def test_run_longmemeval_topk_above_8_not_capped():
    # 12 docs, gold present; k=10 must not be silently capped by an 8-candidate default.
    gold = _doc("g", "the canonical answer about distributed consensus quorums")
    hay = [gold] + [_doc(f"d{i}", f"unrelated filler document number {i} about cooking")
                    for i in range(11)]
    q = _q("big", "distributed consensus quorums answer", hay, ["g"])
    res = run_longmemeval([q], k=10, recall_top_n=12)
    assert res.r_at_5 >= 1.0          # gold found within the (uncapped) window


def test_retrieval_only_false_raises():
    with pytest.raises(NotImplementedError):
        run_longmemeval([], retrieval_only=False)


# ── abstention contract (gate ON, ternary) ────────────────────────────────────
def test_abstention_bucket_gate_enabled_ternary():
    # a false-premise question over a flat/uniform haystack ⇒ gate ON ⇒ ABSTAIN ⇒
    # correct refusal (never a confident surface of a non-existent answer).
    hay = [_doc(f"d{i}", f"generic note number {i} about gardening tools") for i in range(4)]
    q = _q("fp", "what is the password to the vault", hay, [], qtype="abstention")
    res = run_longmemeval([q], k=5, h_frac_max=0.5)
    assert res.abstention.n == 1
    assert res.abstention.correct == 1 and res.abstention.fail == 0


def test_abstention_confident_surface_is_fail():
    # if the gate is wide open (h_frac_max≈1.0) a false-premise q surfaces ⇒ fail,
    # proving the bucket is not pass-by-default (the gate is the thing under test).
    hay = [_doc("g", "the deploy runbook lives in the ops wiki under releases")]
    q = _q("fp", "the deploy runbook lives in the ops wiki under releases",
           hay, [], qtype="abstention")
    res = run_longmemeval([q], k=5, h_frac_max=0.999)
    assert res.abstention.fail == 1 and res.abstention.correct == 0


# ── [C2] live AUROC wiring on oracle output ───────────────────────────────────
def _c2_corpus():
    """A 4-tier fixture whose live (conf, is_miss) stream lands AUROC at 0.75 (in the
    §6.1 #2 band [0.70,0.84]) — NOT a perfect 1.0 separation. The tiers (by the gate's
    own normalized-entropy confidence ``1 - H/ln(N_eff)``):
      clean hit (~1.0)  >  leaky abstention miss (~0.8)  >  ambiguous hit (~0.21)  >
      clean abstention miss (~0.0)
    so the two confidently-wrong abstention misses are the discordant points that pull
    AUROC off 1.0 — exactly the imperfect-gate signal the gate is being scored on."""
    qs = []
    # 3 clean hits: query strongly overlaps exactly one gold ⇒ confident + correct
    clean = [
        ("h1", "g1", "the postgres connection pool exhausted under sudden load spikes",
         "postgres connection pool exhausted under load spikes"),
        ("h2", "g2", "redis eviction dropped queued video encoding jobs at cache flush",
         "redis eviction dropped queued video encoding jobs"),
        ("h3", "g3", "kafka consumer rebalance stalled the ingestion partitions badly",
         "kafka consumer rebalance stalled ingestion partitions"),
    ]
    for qid, gid, gtext, query in clean:
        qs.append(_gold_question(qid, gid, gtext, query, distractors=_DISTRACTORS))
    # 3 ambiguous hits: gold + 2 near-dups all carry the query tokens ⇒ high entropy
    # ⇒ low conf, but the gold IS in top-5 (is_miss False).
    amb = [("am1", "shared overlap token cluster about indexing latency tuning"),
           ("am2", "parallel cache warmup token cluster about latency tuning"),
           ("am3", "batch ingest token cluster about latency tuning windows")]
    for qid, query in amb:
        g = f"{qid}-ga"
        hay = [_doc(g, query + " primary"), _doc(f"{qid}-gb", query + " secondary"),
               _doc(f"{qid}-gc", query + " tertiary"),
               _doc(f"{qid}-u", "unrelated payroll scheduling memo")]
        qs.append(_q(qid, query, hay, [g]))
    # 2 leaky abstention misses: a false premise that peaks moderately on ONE doc ⇒
    # confidently-wrong (high conf, is_miss True) — the gate failed to abstain.
    leaky = [
        ("lk1", "what is the indexing latency override sequence today",
         ["indexing latency tuning notes from the platform team",
          "office snacks restocked in the third floor kitchen",
          "reminder about the all hands meeting on tuesday",
          "the parking garage gate code rotates monthly"]),
        ("lk2", "cache warmup runbook override sequence please",
         ["cache warmup runbook lives under ops releases for the platform",
          "the gym membership renewal is due next week",
          "lunch options near the office include three cafes",
          "the printer on floor two is out of toner again"]),
    ]
    for qid, query, docs in leaky:
        qs.append(_q(qid, query, [_doc(f"{qid}-{i}", t) for i, t in enumerate(docs)],
                     [], qtype="abstention"))
    # 2 clean abstention misses: flat haystack ⇒ max entropy ⇒ conf ~0, correct refusal
    for i in range(2):
        hay = [_doc(f"cb{i}-{j}", f"bland filler note {j} about generic office supplies")
               for j in range(4)]
        qs.append(_q(f"cb{i}", "what is the secret vault passphrase", hay, [],
                     qtype="abstention"))
    return qs


def test_run_longmemeval_emits_continuous_abstention_scores():
    res = run_longmemeval(_c2_corpus(), k=5, h_frac_max=0.5)
    # one (conf, is_miss) per scored AND abstention question
    assert len(res.abstention_scores) == res.scored + res.abstention.n
    assert len(res.is_miss) == len(res.abstention_scores)
    assert all(0.0 <= s <= 1.0 for s in res.abstention_scores)
    # live-wired AUROC: feeding the oracle's own output to the scorer lands in band
    auroc = abstention_auroc(res.abstention_scores, res.is_miss)
    assert 0.70 <= auroc <= 0.84


# ── §6.3 de-confounding rails ─────────────────────────────────────────────────
def test_strip_stamped_tokens_removes_label_leak():
    # AUDIT HIGH-3/HIGH-4: the producer stamp is MULTI-token (Hive-Trace: <T1> <T2> ...)
    # and a trailer is its own line; the rail must strip the WHOLE value run (not just
    # the first token) and match the key CASE-INSENSITIVELY (git trailers are).
    leaked = "fixed the pool bug under load\nHive-Trace: a1b2c3 d4e5f6 g7h8i9"
    cleaned = strip_stamped_tokens(leaked)
    for tok in ("a1b2c3", "d4e5f6", "g7h8i9", "Hive-Trace"):
        assert tok not in cleaned                     # every stamped token gone
    assert "fixed the pool bug under load" in cleaned  # prior-line real content kept
    # lowercase / uppercase trailer keys are both stripped (git matches case-insensitively)
    assert "SECRET123" not in strip_stamped_tokens("fix bug\nhive-trace: SECRET123")
    assert "SECRET9" not in strip_stamped_tokens("fix bug\nHIVE-TRACE: SECRET9")
    # a custom trailer key with a multi-token value
    assert "T9" not in strip_stamped_tokens("note\nco-trace: T8 T9", trailer_key="Co-Trace")


def test_scored_bucket_duplicate_text_keeps_gold():
    # AUDIT MED-6: a gold doc whose text is identical to a later distractor must NOT be
    # erased by last-writer-wins id resolution (the store dedups identical text to one
    # episode). The gold text is the top hit ⇒ recall must be 1.0, is_miss False.
    T = "the postgres connection pool was exhausted under sudden load spikes"
    q = _q("dup", "postgres connection pool exhausted under load",
           [_doc("gold", T), _doc("dup", T)], ["gold"])
    res = run_longmemeval([q], k=5)
    assert res.r_at_5 == pytest.approx(1.0)           # gold text retrieved ⇒ a hit
    assert res.is_miss == [False]                     # NOT a false miss


def test_abstention_empty_haystack_conf_is_zero():
    # AUDIT MED-7: an EMPTY_NO_DATA refusal (empty haystack) must read as ZERO
    # confidence, not 1.0 — entropy_norm=0.0 there means "no data", not "max confident".
    q = _q("fp", "what is the vault passphrase", [], [], qtype="abstention")
    res = run_longmemeval([q], k=5, h_frac_max=0.5)
    assert res.abstention.correct == 1                # correct refusal (no data)
    assert res.abstention_scores == [0.0]             # low confidence, not 1.0
    assert res.is_miss == [True]


def test_hash_embedder_tokenless_distinct():
    # AUDIT MED-9: two distinct token-less texts must not collide to cosine 1.0, and a
    # token-less query must not confidently retrieve a real doc.
    e = HashEmbedder(d=256)
    import numpy as np
    a, b = e.encode("!!!"), e.encode("@@@###")
    assert float(np.dot(a, b)) < 1.0                  # distinct fallback directions
    svc = _EvalService(embedder=e, d=256, h_frac_max=0.5, beta=16.0, recall_top_n=8)
    svc.write("gv")                                   # a real one-token doc
    res = svc.recall("???")                           # token-less query
    # under the old fixed-e_0 fallback the token-less query FALSELY aligned at cosine
    # 1.0 with 'gv'; now distinct buckets ⇒ no fake perfect match.
    assert all(h.sim < 0.99 for h in res.hits)


def test_replay_latency_paired_population(tmp_path):
    # AUDIT MED-8: a text-free baseline entry must NOT leave its latency in the base
    # mean without a live counterpart (which corrupts / can sign-flip latency_delta_ms).
    import json
    bl = tmp_path / "mixed.ndjson"
    bl.write_text(
        json.dumps({"query_hash": "tf", "retrieved": ["x"], "latency_ms": 999.0}) + "\n"
        + json.dumps({"query": "postgres connection pool exhausted",
                      "retrieved": ["y"], "latency_ms": 1.0}) + "\n")
    rep = replay(str(bl), _replay_corpus_svc(), k=5)
    assert rep.n == 1                                 # one replayable entry
    # paired delta uses ONLY the re-run entry's baseline (1.0ms), not the 999ms skip;
    # live recall is sub-millisecond ⇒ the delta is small/negative, never ~-499.
    assert rep.latency_delta_ms > -50.0


def test_assert_exact_path_raises_on_ann():
    class _ANNIndex:
        def is_authoritative(self) -> bool:
            return False
    with pytest.raises(ValueError, match=r"authoritative|ANN"):
        assert_exact_path(_ANNIndex())
    # an authoritative index passes silently
    svc = _EvalService(embedder=HashEmbedder(d=64), d=64, h_frac_max=0.5, beta=16.0,
                       recall_top_n=8)
    assert_exact_path(svc.index)


def test_assert_clean_store_raises_on_nonempty():
    svc = _EvalService(embedder=HashEmbedder(d=64), d=64, h_frac_max=0.5, beta=16.0,
                       recall_top_n=8)
    assert_clean_store(svc)                            # fresh ⇒ ok
    svc.write("a planted memory that makes the store non-empty")
    with pytest.raises(ValueError, match=r"not empty|clean"):
        assert_clean_store(svc)


def test_locomo_category5_routes_to_abstention():
    sample = {
        "conversation": {"s1": [{"dia_id": "t1", "speaker": "A", "text": "hi there"}]},
        "qa": [
            {"id": "q1", "question": "what did A say", "evidence": ["t1"], "category": 4},
            {"id": "q5", "question": "what is the bank PIN", "category": 5},
        ],
    }
    rows = locomo_to_lme_rows(sample)
    by_id = {r["question_id"]: r for r in rows}
    assert by_id["q5"]["type"] == "abstention" and by_id["q5"]["answer_doc_ids"] == []
    assert by_id["q1"]["type"] != "abstention"


def test_run_longmemeval_skips_empty_relevant():
    q = _q("norel", "a question with no gold", [_doc("x", "some unrelated text")], [])
    res = run_longmemeval([q], k=5)
    assert res.n == 0 and res.skipped.get("no_relevant") == 1


def test_loader_normalizes_aliases(tmp_path):
    p = tmp_path / "ds.jsonl"
    p.write_text(
        '{"q": "aliased query", "documents": [{"content": "doc body"}], "relevant": ["0"]}\n'
        "\n"                                            # blank line skipped
        "this is not json at all\n"                     # unparseable ⇒ WARN+skip
        '{"question": "no haystack here"}\n'            # no haystack ⇒ dropped
    )
    qs = load_longmemeval(str(p))
    assert len(qs) == 1
    assert qs[0].question == "aliased query" and qs[0].haystack[0]["text"] == "doc body"


# ── [C3] admit min_gain reachable / fails-closed ──────────────────────────────
def test_admit_positive_min_gain_is_reachable_or_documented_dead():
    # genesis floor 0.0 ⇒ a positive min_gain is LIVE (reachable), unlike the
    # reference's 1.0 self-jaccard floor where jaccard>1.0 was required.
    genesis = ReplayReport(mean_jaccard=0.5, top1_stability=1.0, latency_delta_ms=0.0,
                           n=4, regressions=[])
    assert admit_decision(genesis, min_gain=0.1) is True       # 0.5 >= 0.0 + 0.1
    # with a measured incumbent floor of 0.6 and min_gain 0.1 ⇒ threshold 0.7
    hi = ReplayReport(0.8, 1.0, 0.0, 4, [])
    lo = ReplayReport(0.6, 1.0, 0.0, 4, [])
    assert admit_decision(hi, min_gain=0.1, champion_floor=0.6) is True   # 0.8 >= 0.7
    assert admit_decision(lo, min_gain=0.1, champion_floor=0.6) is False  # 0.6 < 0.7


def test_admit_zero_signal_fails_closed():
    empty = ReplayReport(1.0, 1.0, 0.0, 0, [])         # n == 0
    assert admit_decision(empty, min_gain=0.0, champion_floor=0.0) is False


def test_admit_regressions_over_budget_rejected():
    rep = ReplayReport(1.0, 1.0, 0.0, 4, [{"jaccard": 0.4}])   # one regressing query
    assert admit_decision(rep, max_regressions=0) is False
    assert admit_decision(rep, max_regressions=1) is True


# ── capture → replay ──────────────────────────────────────────────────────────
def _replay_corpus_svc(d=128):
    svc = _EvalService(embedder=HashEmbedder(d=d), d=d, h_frac_max=_RANKER, beta=16.0,
                       recall_top_n=8)
    for t in ("postgres connection pool exhausted under load",
              "redis eviction dropped queued video jobs",
              "kafka rebalanced partitions during ingestion"):
        svc.write(t)
    return svc


_RANKER = 1.0   # gate-off so replay measures the ranker deterministically


def test_replay_identical_store_is_perfectly_stable(tmp_path):
    svc = _replay_corpus_svc()
    queries = ["postgres connection pool exhausted",
               "redis eviction dropped video jobs",
               "kafka rebalanced partitions ingestion"]
    bl = str(tmp_path / "baseline.ndjson")
    export_baseline(bl, queries, svc, k=5)
    # replay against the SAME served state ⇒ jaccard 1.0, top-1 stable, no regressions
    rep = replay(bl, svc, k=5)
    assert rep.n == 3
    assert rep.mean_jaccard == pytest.approx(1.0)
    assert rep.top1_stability == pytest.approx(1.0)
    assert rep.regressions == []
    # admit against itself is True (identical candidate clears genesis floor)
    assert admit(bl, svc, k=5, min_gain=0.0) is True


def test_replay_empty_baseline_reports_zero_queries(tmp_path):
    bl = str(tmp_path / "empty.ndjson")
    open(bl, "w").close()                              # empty baseline
    rep = replay(bl, _replay_corpus_svc(), k=5)
    assert rep.n == 0
    # admit fails closed on the zero-signal baseline
    assert admit(bl, _replay_corpus_svc(), k=5) is False
