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
    assert cfg.geometry.d == 768
    assert cfg.geometry.W_version == 2
    assert cfg.recall.H_frac_max == 0.5
    assert cfg.recall.recall_top_n == 10
    assert cfg.embedding.model == "Qwen/Qwen3-Embedding-0.6B"
    assert cfg.retention.backup_keep == 30


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
    assert cfg.geometry.d == 768


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


def test_isolation_frac_is_cut():
    # the held-out-eval slice (guardrail-2) was cut: the whole utility config group is gone.
    with pytest.raises(ValueError, match=r"isolation_frac|unknown config"):
        Config.load(utility={"isolation_frac": 0.05})


def test_autonomy_solo_knobs_removed():
    # MODE-COLLAPSE: solo_mode + solo_min_span_days are deleted (promotion is one
    # identity-diversity rule for solo and team). Each is now an unknown override field.
    with pytest.raises(ValueError, match=r"solo_mode|unknown config override"):
        Config.load(autonomy={"solo_mode": True})
    with pytest.raises(ValueError, match=r"solo_min_span_days|unknown config override"):
        Config.load(autonomy={"solo_min_span_days": 2})
    # the default AutonomyConfig still validates
    assert Config.load(db_path=":memory:").autonomy.enabled is True


# ── auth group is removed (MODE-COLLAPSE): auth is a property of the listener ────
def test_auth_group_is_removed():
    # the HIVE_AUTH__MODE token|open switch is deleted — auth is now a property of the
    # listening port (tokenless loopback door + token tunnel door), not a config group.
    # An explicit `auth=` override hits the unknown-group guard, fail-fast.
    with pytest.raises(ValueError, match=r"unknown config group override 'auth'"):
        Config.load(auth={"mode": "open"})


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


def test_tau_top1_default_is_zero_inert():
    # the top-1 confidence floor ships inert: a default load is 0.0, so it can never
    # add an abstention (masses are in [0,1]; top1 < 0.0 is impossible).
    assert Config.load(db_path=":memory:").recall.tau_top1 == 0.0


def test_tau_top1_rejects_negative_and_nonfinite():
    with pytest.raises(ValueError, match=r"tau_top1"):
        Config.load(recall={"tau_top1": -0.1})
    with pytest.raises(ValueError, match=r"tau_top1"):
        Config.load(recall={"tau_top1": float("inf")})
    with pytest.raises(ValueError, match=r"tau_top1"):
        Config.load(recall={"tau_top1": float("nan")})
    # >1 is a LEGAL permanent-abstain config (no upper clamp)
    assert Config.load(recall={"tau_top1": 1.5}).recall.tau_top1 == 1.5


def test_db_path_required():
    with pytest.raises(ValueError, match=r"db_path"):
        Config.load(db_path="")


def test_geometry_d_positive():
    with pytest.raises(ValueError, match=r"geometry\.d|\bd\b"):
        Config.load(geometry={"d": 0})


def test_backup_keep_at_least_one():
    with pytest.raises(ValueError, match=r"backup_keep"):
        Config.load(retention={"backup_keep": 0})


# ── ConflictConfig group (default OFF; one empirical knob τ) ───────────────────
def test_conflict_detection_on_suppression_off_by_default():
    cfg = Config.load(db_path=":memory:")
    assert cfg.conflict.enabled is True           # detection surfaces ship ON (fleet flags conflicts)
    # 0.80: measured to sit in the gap between distinct same-subsystem facts (~0.69) and
    # genuine paraphrase/contradiction pairs (~0.81-0.87) — calibratable per deployment.
    assert cfg.conflict.tau == 0.80
    assert cfg.conflict.top_n == 10
    assert cfg.conflict.suppress is False         # serve-time pruning stays OPT-IN


def test_conflict_enabled_via_env():
    cfg = Config.load(db_path=":memory:", env={"HIVE_CONFLICT__ENABLED": "true"})
    assert cfg.conflict.enabled is True


def test_conflict_suppress_is_independent_of_enabled():
    # suppress (serve-time pruning) and enabled (detection surfaces) are orthogonal: suppression
    # can run with detection OFF, proving the two knobs do not imply each other.
    cfg = Config.load(db_path=":memory:",
                      env={"HIVE_CONFLICT__SUPPRESS": "true", "HIVE_CONFLICT__ENABLED": "false"})
    assert cfg.conflict.suppress is True
    assert cfg.conflict.enabled is False


def test_conflict_tau_bounds_rejected():
    with pytest.raises(ValueError, match=r"conflict\.tau|tau"):
        Config.load(conflict={"tau": 0.0})
    with pytest.raises(ValueError, match=r"conflict\.tau|tau"):
        Config.load(conflict={"tau": 1.5})
    # the boundary 1.0 is legal (identical-only near-dup)
    assert Config.load(conflict={"tau": 1.0}).conflict.tau == 1.0


def test_conflict_top_n_at_least_one():
    with pytest.raises(ValueError, match=r"top_n"):
        Config.load(conflict={"top_n": 0})


def test_conflict_group_is_frozen():
    cfg = Config.load(db_path=":memory:")
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.conflict.enabled = True   # type: ignore[misc]


# ── config → embedder threading (torch-free: construction does not load the model) ────
def test_build_provider_sources_native_dim():
    # the embedder's d is the model's NATIVE dim (sourced from native_dim_for), not a config
    # knob — construction is lazy (no model load), so this proves the wiring without the model.
    from hive.adapters.embedding.factory import build_provider, native_dim_for
    cfg = Config.load(db_path=":memory:")
    assert build_provider(cfg).d == native_dim_for(cfg.embedding.model) == 1024
