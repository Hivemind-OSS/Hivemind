"""C2(poison) — the adversarial-overlay substrate (DATA layer). ``build_poison_substrate`` overlays
committed-servable falsehoods on the clean ``build_substrate`` pool; ``PoisonSpec``/``PoisonTaskCase``
are the frozen self-asserting carriers; ``_mutate_value`` carves a falsehood out of a real turn;
``_assert_not_telegraphed`` refuses self-announcing content; ``make_credential_text`` synthesizes a
secret-floor-refusable string. Fully offline: the real LongMemEval tiny fixture, no backend, no
model, no network. Backend planting/supersede/falsity_map are a separate chunk and not tested here."""
from __future__ import annotations

from pathlib import Path

import pytest

from hive.research.bench.dataset import load_longmemeval
from hive.research.bench.substrate import build_substrate
from hive.research.bench.task_agent import _content_tokens
from hive.research.bench.poison_substrate import (
    POISON_VERSION,
    PoisonSpec,
    PoisonTaskCase,
    _assert_not_telegraphed,
    _mutate_value,
    build_poison_substrate,
    make_credential_text,
)

_FIX = Path(__file__).parent / "fixtures" / "longmemeval_tiny.json"


def _cases():
    return load_longmemeval(str(_FIX))


def _gold_sources(cases) -> set[str]:
    """Every gold-evidence session id across the corpus — the set a false source must avoid."""
    return {s for c in cases for s in c.question.evidence_session_ids}


# ── build_poison_substrate: plants a distinct non-gold falsehood ────────────────
def test_build_poison_substrate_plants_distinct_nongold_falsehood():
    cases = _cases()
    mems, poison_cases, specs = build_poison_substrate(
        cases, seed=0, poison_frac=1.0, kinds=("mistake", "stale"))
    assert specs, "poison_frac=1.0 over answerable cases must synthesize ≥1 falsehood"
    gold_all = _gold_sources(cases)
    pool_sources = {src for src, _txt in mems}
    for spec in specs:
        # the false source is DISTINCT and absent from every gold set (a competing hard-negative)
        assert spec.false_source_id not in gold_all
        assert spec.false_source_id != spec.gold_source_id
        # the values are token-disjoint and genuinely different
        assert spec.true_value != spec.false_value
        assert not (_content_tokens(spec.true_value) & _content_tokens(spec.false_value))
        # the false tuple is actually present in the augmented pool, carrying the mutated text
        assert (spec.false_source_id, spec.false_text) in mems
        assert spec.false_source_id in pool_sources
        # the mutation actually changed the turn: the false value is present, planted in real prose
        assert spec.false_value in spec.false_text
    corrected = [c for c in poison_cases if c.regime == "corrected"]
    assert corrected, "a poisoned answerable case must surface as a corrected PoisonTaskCase"
    for c in corrected:
        assert c.false_source_ids and not (c.false_source_ids & frozenset(gold_all))


def test_poison_version_is_stamped():
    assert POISON_VERSION == "poison.v1"


# ── PoisonSpec: token-disjoint values are enforced ──────────────────────────────
def test_poison_spec_enforces_token_disjoint_values():
    # a false_value sharing a content token with the true value is an unconstructable state
    with pytest.raises(ValueError, match="token-disjoint|share"):
        PoisonSpec(
            gold_source_id="s1", false_source_id="poison::s1",
            true_value="port 8080", false_value="port 9090",   # shares the token "port"
            false_text="the dev server runs on port 9090", kind="mistake")


def test_poison_spec_rejects_false_source_equal_to_gold():
    with pytest.raises(ValueError, match="DISTINCT|gold"):
        PoisonSpec(
            gold_source_id="s1", false_source_id="s1",
            true_value="8080", false_value="9191",
            false_text="the dev server runs on port 9191", kind="mistake")


def test_poison_spec_accepts_disjoint_values():
    spec = PoisonSpec(
        gold_source_id="s1", false_source_id="poison::s1",
        true_value="8080", false_value="9191",
        false_text="the dev server runs on port 9191", kind="mistake")
    assert spec.kind == "mistake"


# ── PoisonTaskCase: regime invariants ───────────────────────────────────────────
def test_poison_task_case_corrected_requires_false_source_and_disjoint_values():
    with pytest.raises(ValueError, match="false source"):
        PoisonTaskCase(
            query="q", regime="corrected", kind="mistake",
            gold_source_ids=frozenset({"s1"}), false_source_ids=frozenset(),
            true_value="8080", false_value="9191", expected="8080")


def test_poison_task_case_clean_forbids_false_source():
    with pytest.raises(ValueError, match="NO false source"):
        PoisonTaskCase(
            query="q", regime="clean", kind="mistake",
            gold_source_ids=frozenset({"s1"}), false_source_ids=frozenset({"poison::s1"}),
            true_value=None, false_value=None, expected="8080")


# ── _assert_not_telegraphed: refuses a tell or an echo of the true value ────────
def test_substrate_refuses_telegraphed_falsehood():
    # a tell word must raise
    with pytest.raises(ValueError, match="telegraph"):
        _assert_not_telegraphed("this is the WRONG port 9090", true_value="8080")
    # an echo of the true value must raise (the poison row would credit the true answer)
    with pytest.raises(ValueError, match="echo"):
        _assert_not_telegraphed("the port is 8080 and also 9090", true_value="8080")


def test_assert_not_telegraphed_passes_a_clean_falsehood():
    # a plain wrong-valued copy with neither a tell nor the true value must NOT raise
    _assert_not_telegraphed("the dev server runs on port 9090", true_value="8080")


# ── _mutate_value: substitute the value, keep the surrounding prose ─────────────
def test_mutate_value_substitutes_only_the_value_keeping_prose():
    turn = "the dev server runs on port 8080"
    out = _mutate_value(turn, "8080", "9090")
    assert out == "the dev server runs on port 9090"
    assert "9090" in out and "8080" not in out
    # surrounding prose is retained verbatim
    assert "the dev server runs on port" in out


def test_mutate_value_falls_back_when_value_absent_from_turn():
    # a gold turn that does not contain the answer verbatim still ends carrying the false value
    out = _mutate_value("we use postgres for the main store", "postgres on port 5432", "zarquon")
    assert "zarquon" in out
    assert "we use postgres for the main store" in out


# ── make_credential_text: a named-real-prefix secret the floor refuses ──────────
def test_make_credential_text_carries_a_named_real_prefix():
    txt = make_credential_text("staging")
    assert ("sk-proj-" in txt) or ("ghp_" in txt)
    # wrapped in plausible prose, not a bare token
    assert "staging" in txt
    assert len(txt) > len("sk-proj-")


def test_make_credential_text_is_deterministic():
    assert make_credential_text("prod") == make_credential_text("prod")


# ── off-is-inert: kinds=() yields a byte-identical pool to build_substrate ───────
def test_pool_is_byte_inert_when_kinds_empty():
    cases = _cases()
    base_mems, _base_cases = build_substrate(cases, seed=3)
    mems, poison_cases, specs = build_poison_substrate(cases, seed=3, kinds=())
    assert mems == base_mems                 # byte-identical pool — the overlay is fully inert
    assert specs == ()                       # no falsehood synthesized
    assert all(c.regime != "corrected" for c in poison_cases)   # nothing poisoned
    assert all(not c.false_source_ids for c in poison_cases)
