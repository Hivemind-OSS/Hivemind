"""B1: LongMemEval loader + gold mapping. Tested against a hand-built fixture (every branch)
plus a smoke load of the REAL longmemeval_s_cleaned.json (the true schema-drift guard) when the
HF-cached file is present."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hive.research.bench.dataset import (
    Case, dev_test_split, gold_relevant, load_longmemeval,
)

_FIX = Path(__file__).parent / "fixtures" / "longmemeval_tiny.json"


def _cases():
    return load_longmemeval(str(_FIX))


def _by_id(cases):
    return {c.question.question_id: c for c in cases}


# ── parsing + the abstention / evidence branches ───────────────────────────────
def test_load_fixture_parses_all_three():
    cases = _cases()
    assert len(cases) == 3 and all(isinstance(c, Case) for c in cases)
    assert {c.question.question_id for c in cases} == {"q1", "q2", "q3_abs"}


def test_abstention_is_flagged_by_abs_suffix_only():
    by = _by_id(_cases())
    assert by["q3_abs"].question.is_unanswerable is True
    assert by["q3_abs"].question.evidence_session_ids == frozenset()   # no evidence
    assert by["q1"].question.is_unanswerable is False
    assert by["q2"].question.is_unanswerable is False


def test_evidence_session_ids_parsed():
    by = _by_id(_cases())
    assert by["q1"].question.evidence_session_ids == frozenset({"s1"})
    assert by["q2"].question.evidence_session_ids == frozenset({"s3", "s4"})


def test_has_answer_turn_marking_preserved():
    by = _by_id(_cases())
    s1 = by["q1"].sessions[0]
    assert s1.session_id == "s1" and s1.turns[0].has_answer is True
    assert by["q1"].sessions[1].turns[0].has_answer is False           # default when absent


# ── gold mapping ───────────────────────────────────────────────────────────────
def test_gold_relevant_maps_source_session_to_evidence():
    by = _by_id(_cases())
    mem_source = {"m1": "s1", "m2": "s2", "m3": "s1"}                  # m1,m3 from evidence s1
    assert gold_relevant(by["q1"], mem_source) == {"m1", "m3"}
    # an abstention question has no evidence ⇒ nothing is gold-relevant
    assert gold_relevant(by["q3_abs"], mem_source) == set()


# ── fail-fast contracts ────────────────────────────────────────────────────────
def test_schema_drift_names_the_missing_field(tmp_path):
    bad = [{"question_id": "x", "question": "?", "answer": "a"}]      # missing 6 fields
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(bad))
    with pytest.raises(ValueError, match="missing field 'question_type'"):
        load_longmemeval(str(p))


def test_haystack_length_mismatch_raises(tmp_path):
    inst = json.loads(_FIX.read_text())[0]
    inst["haystack_session_ids"] = ["only-one"]                       # now 1 id vs 2 sessions
    p = tmp_path / "mm.json"
    p.write_text(json.dumps([inst]))
    with pytest.raises(ValueError, match="length mismatch"):
        load_longmemeval(str(p))


def test_missing_file_raises():
    with pytest.raises(FileNotFoundError, match="HIVE_BENCH_LME_PATH"):
        load_longmemeval("/nonexistent/longmemeval.json")


# ── deterministic subsample + dev/test split ───────────────────────────────────
def test_subsample_is_seed_deterministic():
    a = [c.question.question_id for c in load_longmemeval(str(_FIX), n=2, seed=7)]
    b = [c.question.question_id for c in load_longmemeval(str(_FIX), n=2, seed=7)]
    assert a == b and len(a) == 2


def test_dev_test_split_is_disjoint_and_exhaustive():
    cases = _cases()
    dev, test = dev_test_split(cases, seed=1, dev_frac=0.34)          # 0.34*3 → 1 dev
    dev_ids = {c.question.question_id for c in dev}
    test_ids = {c.question.question_id for c in test}
    assert dev_ids and test_ids and dev_ids.isdisjoint(test_ids)
    assert dev_ids | test_ids == {"q1", "q2", "q3_abs"}


# ── the real-file schema-drift guard (skips if the HF cache isn't present) ──────
def _real_path():
    try:
        from huggingface_hub import hf_hub_download
        return hf_hub_download(repo_id="xiaowu0162/longmemeval-cleaned",
                               filename="longmemeval_s_cleaned.json",
                               repo_type="dataset", local_files_only=True)
    except Exception:
        return None


@pytest.mark.skipif(_real_path() is None, reason="LongMemEval-S not in the HF cache")
def test_real_longmemeval_s_loads_without_drift():
    cases = load_longmemeval(_real_path())
    assert len(cases) == 500
    assert sum(1 for c in cases if c.question.is_unanswerable) == 30   # the 30 _abs questions
    # every answerable question has at least one evidence session
    for c in cases:
        if not c.question.is_unanswerable:
            assert c.question.evidence_session_ids
