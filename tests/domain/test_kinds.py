"""The kind registry is the single source for the structured-memory vocabulary; these pin
its shape so the projections (tool_defs enum, onboard_ref prose, store CHECK, models
validation) cannot silently lose a kind or a required field."""
from __future__ import annotations

from hive.domain.kinds import (
    DEFAULT_KIND, KINDS, KIND_NAMES, QUERY_GUIDANCE, render_taxonomy,
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


def test_render_taxonomy_names_every_kind_and_both_rules():
    rendered = render_taxonomy()
    for name in KIND_NAMES:
        assert name in rendered, f"{name} dropped from served taxonomy"
    assert "POLAR LANGUAGE:" in rendered
    assert "DENSITY:" in rendered


def test_query_guidance_warns_about_abstention():
    # the load-bearing reason to write dense queries; mutation-visible if gutted
    assert "abstain" in QUERY_GUIDANCE.lower()
