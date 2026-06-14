"""The agent-config-tuning CONTRACT: who may set each knob, the advisory bounds, and
which KPI each agent knob moves. Pure-data dicts in config.py (beside RELOAD_TIER); the
loader never reads them — these tests are the enforced contract that keeps them honest.

The load-bearing guards:
- CONFIG_AUTHORITY covers EVERY field (a new field cannot be silently un-governed).
- The four GUARANTEE_KNOBS are operator-authority (the firewall set).
- SAFE_BOUNDS / KPI_MOVED describe ONLY agent knobs; KPI_MOVED is TOTAL over them
  (every agent knob is documented — live ones name the KPI, inert ones say so).
- Every SAFE_BOUNDS range is inside the field's hard __post_init__ validation
  (both endpoints load without raising) — the advisory band can never exceed the floor.
"""
from __future__ import annotations

import dataclasses
import math

import pytest

from hive.app import config as C
from hive.app.config import (CONFIG_AUTHORITY, GUARANTEE_KNOBS, KPI_MOVED,
                             SAFE_BOUNDS, Config)


def _all_fields() -> set[str]:
    """Every "group.field" the Config tree actually declares (the authority surface)."""
    return {f"{g}.{f.name}"
            for g, typ in C._GROUP_TYPES.items()
            for f in dataclasses.fields(typ)}


def _agent_knobs() -> set[str]:
    return {k for k, who in CONFIG_AUTHORITY.items() if who == "agent"}


def test_authority_covers_every_field():
    fields = _all_fields()
    missing = fields - set(CONFIG_AUTHORITY)
    extra = set(CONFIG_AUTHORITY) - fields
    assert not missing, f"config fields with no declared authority: {sorted(missing)}"
    assert not extra, f"CONFIG_AUTHORITY names non-existent fields: {sorted(extra)}"


def test_authority_values_are_agent_or_operator():
    bad = {k: v for k, v in CONFIG_AUTHORITY.items() if v not in ("agent", "operator")}
    assert not bad, f"authority must be 'agent'|'operator': {bad}"


def test_guarantee_knobs_exist_and_are_operator():
    fields = _all_fields()
    for k in GUARANTEE_KNOBS:
        assert k in fields, f"GUARANTEE_KNOBS names a non-existent field: {k}"
        assert CONFIG_AUTHORITY[k] == "operator", \
            f"guarantee knob {k} must be operator-authority (it is env-pinned)"


def test_safe_bounds_keys_are_agent_knobs():
    not_agent = {k: CONFIG_AUTHORITY.get(k) for k in SAFE_BOUNDS
                 if CONFIG_AUTHORITY.get(k) != "agent"}
    assert not not_agent, f"SAFE_BOUNDS only describes agent knobs; offenders: {not_agent}"


def test_kpi_moved_is_total_over_agent_knobs():
    # every agent knob is documented (live → names the KPI; inert → says INERT), and
    # KPI_MOVED never documents an operator knob.
    assert set(KPI_MOVED) == _agent_knobs(), (
        f"KPI_MOVED must cover exactly the agent knobs; "
        f"missing={sorted(_agent_knobs() - set(KPI_MOVED))} "
        f"extra={sorted(set(KPI_MOVED) - _agent_knobs())}")


def test_safe_bounds_finite_and_ordered():
    for k, rng in SAFE_BOUNDS.items():
        assert len(rng) == 2, f"{k} bound must be (lo, hi)"
        lo, hi = rng
        assert math.isfinite(lo) and math.isfinite(hi), f"{k} bound not finite"
        assert lo <= hi, f"{k} bound inverted: {lo} > {hi}"


def test_safe_bounds_inside_hard_validation():
    # SAFE_BOUNDS is ADVISORY and strictly within each field's hard validator: loading a
    # Config with the bound's lo OR hi must NOT raise. (SAFE_BOUNDS is tighter than the
    # hard floor, so a value just OUTSIDE the advisory band is still hard-valid — we do
    # NOT assert that raises; we assert the band's endpoints are accepted.)
    for k, (lo, hi) in SAFE_BOUNDS.items():
        group, field = k.split(".", 1)
        for end in (lo, hi):
            # ints declared as ints must be passed as ints (the loader coerces env, but a
            # direct override dict is used as-is).
            decl = {f.name: f for f in dataclasses.fields(C._GROUP_TYPES[group])}[field]
            val = int(end) if "int" in str(decl.type) and "tuple" not in str(decl.type) else end
            try:
                Config.load(**{group: {field: val}})
            except ValueError as exc:               # pragma: no cover - failure path
                pytest.fail(f"SAFE_BOUNDS[{k}] endpoint {val} outside hard validation: {exc}")
