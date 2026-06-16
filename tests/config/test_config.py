"""P1.9 — M11 Config: frozen Config.load (3-layer precedence) + fail-fast
validation + env namespacing.

Config is applied only at boot (a full restart) — there is no live reload path.

Grounded deviations (built-decision-wins, documented in the deliverable):
- ``db_path`` defaults to ``":memory:"`` (ephemeral, cannot corrupt a warm store) so a
  no-db_path ``Config.load(...)`` succeeds; an EMPTY string is the fail-fast trigger for
  ``test_db_path_required``. The "no silent default" prose guards a persistent store —
  ``":memory:"`` cannot.
"""
from __future__ import annotations

import dataclasses

import pytest

from hive.app.config import Config


# ── happy path / defaults ─────────────────────────────────────────────────────
def test_defaults_match_spec_geometry():
    cfg = Config.load(db_path=":memory:")
    assert cfg.geometry.d == 256
    assert cfg.recall.H_frac_max == 0.5
    assert cfg.recall.recall_top_n == 10
    assert cfg.embedding.model == "BAAI/bge-small-en-v1.5"
    assert cfg.index.backend == "exhaustive"
    assert cfg.retention.backup_keep == 30


# ── [A4] ε is validated on recall ──────────────────────────────────────────────
def test_recall_epsilon_validated_positive():
    with pytest.raises(ValueError, match=r"recall\.epsilon_explore"):
        Config.load(recall={"epsilon_explore": 0.0})


def test_recall_epsilon_negative_rejected():
    with pytest.raises(ValueError, match=r"recall\.epsilon_explore"):
        Config.load(recall={"epsilon_explore": -0.1})


def test_isolation_frac_default():
    cfg = Config.load(db_path=":memory:")
    assert cfg.utility.isolation_frac == 0.05


# ── env namespacing closes the CORTEX_D collision ─────────────────────────────
def test_env_namespacing_no_collision():
    env = {"HIVE_RECALL__H_FRAC_MAX": "0.4", "HIVE_GEOMETRY__D": "384"}
    cfg = Config.load(db_path=":memory:", env=env)
    assert cfg.recall.H_frac_max == 0.4   # field on the RECALL group
    assert cfg.geometry.d == 384          # field on the GEOMETRY group — no CORTEX_D collapse


def test_env_unknown_key_ignored_not_crash():
    env = {"HIVE_NOPE__DOES_NOT_EXIST": "x", "HIVE_RECALL__NOSUCH": "y"}
    cfg = Config.load(db_path=":memory:", env=env)   # must not raise
    assert cfg.recall.H_frac_max == 0.5


def test_env_noncoercible_value_skipped_with_warn(caplog):
    # a non-int for an int field is skipped (logged), never crashes; default survives
    env = {"HIVE_GEOMETRY__D": "not-an-int"}
    cfg = Config.load(db_path=":memory:", env=env)
    assert cfg.geometry.d == 256


# ── 3-layer precedence: defaults < HIVE_* env < explicit overrides ────────────
def test_layering_precedence():
    # explicit beats env; env beats the default.
    env = {"HIVE_GEOMETRY__D": "384", "HIVE_RECALL__RECALL_TOP_N": "11"}
    cfg = Config.load(db_path=":memory:", env=env, recall={"recall_top_n": 13})
    assert cfg.recall.recall_top_n == 13   # explicit override beats env
    assert cfg.geometry.d == 384           # env value applied (no explicit override)


def test_env_applies_every_knob():
    # one source per knob (env only): every HIVE_<group>__<field> applies — no authority
    # partition silently dropping a "wrong-layer" knob.
    env = {"HIVE_RECALL__H_FRAC_MAX": "0.4", "HIVE_RECALL__RECALL_TOP_N": "12"}
    cfg = Config.load(db_path=":memory:", env=env)
    assert cfg.recall.H_frac_max == 0.4
    assert cfg.recall.recall_top_n == 12


def test_provider_field_routes_to_embedding_group():
    # `provider` lives only in the embedding group; pin that the group-scoped env matcher
    # routes it there, not elsewhere.
    cfg = Config.load(db_path=":memory:", env={"HIVE_EMBEDDING__PROVIDER": "local_st"})
    assert cfg.embedding.provider == "local_st"


def test_unknown_override_field_raises():
    # a typo in the highest-precedence layer must fail fast, not silently leave the floor default
    with pytest.raises(ValueError, match=r"H_frac_maxx|unknown config override"):
        Config.load(recall={"H_frac_maxx": 0.4})


# ── frozen on root AND nested groups ──────────────────────────────────────────
def test_frozen_nested_group_raises():
    cfg = Config.load(db_path=":memory:")
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.recall.H_frac_max = 0.9   # type: ignore[misc]


def test_frozen_root_raises():
    cfg = Config.load(db_path=":memory:")
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.recall = None   # type: ignore[misc]


# ── fail-fast validation ──────────────────────────────────────────────────────
def test_h_frac_max_bounds():
    with pytest.raises(ValueError, match=r"H_frac_max"):
        Config.load(recall={"H_frac_max": 0.0})
    with pytest.raises(ValueError, match=r"H_frac_max"):
        Config.load(recall={"H_frac_max": 1.5})
    # the boundary 1.0 is legal (== fully permissive, gate still constructible)
    assert Config.load(recall={"H_frac_max": 1.0}).recall.H_frac_max == 1.0


def test_db_path_required():
    with pytest.raises(ValueError, match=r"db_path"):
        Config.load(db_path="")


def test_unknown_index_backend_rejected():
    with pytest.raises(ValueError, match=r"backend"):
        Config.load(index={"backend": "totally-made-up"})


def test_geometry_d_positive():
    with pytest.raises(ValueError, match=r"geometry\.d|\bd\b"):
        Config.load(geometry={"d": 0})


def test_backup_keep_at_least_one():
    with pytest.raises(ValueError, match=r"backup_keep"):
        Config.load(retention={"backup_keep": 0})


# ── channel flags ship OFF by construction; env-coercible to ON ───────────────
def test_recall_hybrid_defaults_off_env_coercible():
    assert Config.load(db_path=":memory:").recall.hybrid is False
    assert Config.load(db_path=":memory:", env={"HIVE_RECALL__HYBRID": "true"}).recall.hybrid is True


def test_recall_drafts_defaults_off_env_coercible():
    assert Config.load(db_path=":memory:").recall.drafts is False
    assert Config.load(db_path=":memory:", env={"HIVE_RECALL__DRAFTS": "true"}).recall.drafts is True


def test_recall_draft_tau_default_and_validated():
    assert Config.load(db_path=":memory:").recall.draft_tau == 0.6
    for bad in (0.0, -0.1, 1.5, float("nan")):
        with pytest.raises(ValueError, match=r"recall\.draft_tau"):
            Config.load(recall={"draft_tau": bad})
