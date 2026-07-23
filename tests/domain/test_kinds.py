"""The kind registry is the single source for the structured-memory vocabulary; these pin
its shape so the projections (tool_defs enum + glosses, store CHECK, models
validation) cannot silently lose a kind or a required field."""
from __future__ import annotations

from hive.domain.kinds import (
    DEFAULT_KIND, KINDS, KIND_NAMES, QUERY_GUIDANCE,
)

_POLARITIES = {"do", "dont", "neutral"}


def test_default_kind_is_a_real_kind():
    assert DEFAULT_KIND in KIND_NAMES


def test_kind_names_matches_the_registry():
    assert KIND_NAMES == frozenset(KINDS)


def test_expected_kinds_are_all_present():
    # the locked covering set — a dropped kind is a silent coverage regression
    assert KIND_NAMES == {
        "bug", "gotcha", "convention", "design_choice",
        "contract", "dead_end", "env_fact", "note",
    }


def test_every_kind_has_gloss_template_and_valid_default_polarity():
    for name, spec in KINDS.items():
        assert spec["gloss"], f"{name} missing gloss"
        assert spec["template"], f"{name} missing template"
        assert spec["default_polarity"] in _POLARITIES, f"{name} bad default_polarity"


def test_query_guidance_warns_about_abstention():
    # the load-bearing reason to write dense queries; mutation-visible if gutted
    assert "abstain" in QUERY_GUIDANCE.lower()


def test_query_guidance_mandates_a_single_pointed_query_set():
    # a bundled multi-question query dilutes toward the centroid and abstains; the guidance must
    # direct splitting an information need into single-pointed queries, one hive_recall per intent.
    g = QUERY_GUIDANCE.lower()
    assert "single-pointed" in g
    assert "one intent" in g
    assert "per intent" in g            # one recall per intent, not a bulk multi-question query
