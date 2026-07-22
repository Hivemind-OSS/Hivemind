"""Fit + schema guards for the served v3 surface (BUG-023: every capped MCP metadata field —
``initialize.instructions``, each tool ``description`` — must fit under the measured client
truncation point or it arrives clipped mid-word).

Extended for U4-THIN-AGENT: the same table is also swept for the DELETED apparatus (no
approver, no AGI vocabulary, no onboarding flag — the CT-11/CT-12 schema scenarios, mirrored
at the module level) and pinned to the §3.3 v3 schema shapes."""
from __future__ import annotations

import json

from hive.app.contract import METADATA_FIELD_LIMIT, SERVER_INSTRUCTIONS
from hive.app.tool_defs import TOOL_DEFINITIONS, TOOL_NAMES

V3_VERBS = {
    "hive_write",
    "hive_capture",
    "hive_recall",
    "hive_supersede",
    "hive_prune",
    "hive_flag",
    "hive_outcome",
    "hive_health",
}


def _schema(name: str) -> dict:
    return next(t["inputSchema"] for t in TOOL_DEFINITIONS if t["name"] == name)


def _props(name: str) -> dict:
    return _schema(name)["properties"]


# ── A. fit guards — every capped-metadata field fits under the measured truncation point ──
def test_metadata_field_limit_is_the_measured_truncation_point():
    assert METADATA_FIELD_LIMIT == 2048


def test_server_instructions_fit_metadata_limit():
    assert len(SERVER_INSTRUCTIONS) <= METADATA_FIELD_LIMIT


def test_all_tool_descriptions_fit_metadata_limit():
    over = [(t["name"], len(t["description"])) for t in TOOL_DEFINITIONS
            if len(t["description"]) > METADATA_FIELD_LIMIT]
    assert over == [], f"over the metadata field limit: {over}"


# ── B. the deletion sweep (CT-11/CT-12 mirror): no approver, no AGI, no onboarding ──
def test_table_is_exactly_the_eight_verbs():
    assert TOOL_NAMES == frozenset(V3_VERBS)
    assert len(TOOL_DEFINITIONS) == len(V3_VERBS)          # no duplicate rows


def test_schema_sweep_no_approver_agi_or_onboarding_anywhere():
    dumped = json.dumps(TOOL_DEFINITIONS)
    for dead in ("approved_by", "approver", "AGI", "proposed_by", "include_onboarding",
                 "hive-edge", "contract_version"):
        assert dead not in dumped, f"deleted apparatus survives in the table: {dead!r}"


# ── C. the §3.3 v3 schema shapes ──────────────────────────────────────────────
def test_write_schema_v3():
    s = _schema("hive_write")
    assert s["required"] == ["text"]                       # text ALONE — no approver
    assert set(s["properties"]) == {"text", "replaces", "polarity", "kind",
                                    "anchors", "repos", "meta"}
    assert s["properties"]["replaces"] == {"type": "integer"}


def test_capture_schema_v3():
    s = _schema("hive_capture")
    assert s["required"] == ["text"]
    assert set(s["properties"]) == {"text", "polarity", "kind", "anchors", "repos", "meta"}
    assert "replaces" not in s["properties"]               # capture can retire nothing


def test_recall_schema_v3():
    s = _schema("hive_recall")
    assert s["required"] == ["query"]
    assert set(s["properties"]) == {"query", "repos", "anchor_prefix"}
    assert s["properties"]["repos"] == {"type": "array", "items": {"type": "string"}}
    assert s["properties"]["anchor_prefix"] == {"type": "string"}


def test_supersede_and_prune_are_ids_only():
    sup = _schema("hive_supersede")
    assert sorted(sup["required"]) == ["loser", "winner"]
    assert set(sup["properties"]) == {"loser", "winner"}
    prune = _schema("hive_prune")
    assert prune["required"] == ["episode_id"]
    assert set(prune["properties"]) == {"episode_id"}


def test_outcome_and_flag_shapes_unchanged():
    out = _schema("hive_outcome")
    assert out["required"] == []
    assert set(out["properties"]) == {"helped", "hurt"}
    flag = _schema("hive_flag")
    assert sorted(flag["required"]) == ["a", "b", "kind"]
    assert set(flag["properties"]) == {"a", "b", "kind", "winner", "resolution"}
    assert flag["properties"]["kind"]["enum"] == ["conflict", "supersedes"]


def test_health_flags_v3_without_onboarding():
    props = set(_props("hive_health"))
    assert props == {"include_gaps", "include_trends", "include_conflicts",
                     "include_suspect_consensus", "include_stale_suspects",
                     "include_census_health", "include_meta_versions"}


# ── D. the shared structured-anchor grammar ───────────────────────────────────
def test_anchors_property_is_shared_and_structured():
    """ONE spec rides both write and capture (identity, not equality — the grammar cannot
    fork), and its item shape is the enforced (repo, anchor) row: both fields required,
    nothing extra constructable."""
    w, c = _props("hive_write")["anchors"], _props("hive_capture")["anchors"]
    assert w is c
    item = w["items"]
    assert item["type"] == "object"
    assert item["required"] == ["repo", "anchor"]
    assert set(item["properties"]) == {"repo", "anchor"}
    assert item["additionalProperties"] is False


def test_repos_property_is_shared_by_write_and_capture():
    w, c = _props("hive_write")["repos"], _props("hive_capture")["repos"]
    assert w is c
    assert w["type"] == "array" and w["items"] == {"type": "string"}


def test_meta_property_is_shared_by_write_and_capture():
    w, c = _props("hive_write")["meta"], _props("hive_capture")["meta"]
    assert w is c
    assert w["type"] == "object"
