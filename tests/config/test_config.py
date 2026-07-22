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
def test_defaults_match_spec():
    cfg = Config.load(db_path=":memory:")
    assert cfg.recall.tau_serve == 0.70
    assert cfg.recall.k_min == 1
    assert cfg.recall.recall_top_n == 10
    assert cfg.embedding.model == "Qwen/Qwen3-Embedding-0.6B"
    assert cfg.retention.backup_keep == 30


# ── env namespacing: the `__` split keeps two groups' fields distinct ──────────
def test_env_namespacing_no_collision():
    env = {"HIVE_RECALL__TAU_SERVE": "0.5", "HIVE_AUTONOMY__DEMAND_M": "5"}
    cfg = Config.load(db_path=":memory:", env=env)
    assert cfg.recall.tau_serve == 0.5    # field on the RECALL group
    assert cfg.autonomy.demand_m == 5     # field on the AUTONOMY group — distinct, no collapse


def test_env_unknown_key_ignored_not_crash():
    env = {"HIVE_NOPE__DOES_NOT_EXIST": "x", "HIVE_RECALL__NOSUCH": "y"}
    cfg = Config.load(db_path=":memory:", env=env)   # must not raise
    assert cfg.recall.tau_serve == 0.70


def test_env_noncoercible_value_skipped_with_warn(caplog):
    # a non-int for an int field is skipped (logged), never crashes; default survives
    env = {"HIVE_RECALL__RECALL_TOP_N": "not-an-int"}
    cfg = Config.load(db_path=":memory:", env=env)
    assert cfg.recall.recall_top_n == 10


# ── 3-layer precedence: defaults < HIVE_* env < explicit overrides ────────────
def test_layering_precedence():
    # explicit beats env; env beats the default.
    env = {"HIVE_AUTONOMY__DEMAND_M": "7", "HIVE_RECALL__RECALL_TOP_N": "11"}
    cfg = Config.load(db_path=":memory:", env=env, recall={"recall_top_n": 13})
    assert cfg.recall.recall_top_n == 13   # explicit override beats env
    assert cfg.autonomy.demand_m == 7      # env value applied (no explicit override)


def test_env_applies_every_knob():
    # one source per knob (env only): every HIVE_<group>__<field> applies — no authority
    # partition silently dropping a "wrong-layer" knob.
    env = {"HIVE_RECALL__TAU_SERVE": "0.5", "HIVE_RECALL__K_MIN": "2"}
    cfg = Config.load(db_path=":memory:", env=env)
    assert cfg.recall.tau_serve == 0.5
    assert cfg.recall.k_min == 2


def test_provider_field_routes_to_embedding_group():
    # `provider` lives only in the embedding group; pin that the group-scoped env matcher
    # routes it there, not elsewhere.
    cfg = Config.load(db_path=":memory:", env={"HIVE_EMBEDDING__PROVIDER": "local_st"})
    assert cfg.embedding.provider == "local_st"


def test_unknown_override_field_raises():
    # a typo in the highest-precedence layer must fail fast, not silently leave the floor default
    with pytest.raises(ValueError, match=r"tau_servee|unknown config override"):
        Config.load(recall={"tau_servee": 0.4})


def test_isolation_frac_is_cut():
    # the held-out-eval slice (guardrail-2) was cut: the whole utility config group is gone.
    with pytest.raises(ValueError, match=r"isolation_frac|unknown config"):
        Config.load(utility={"isolation_frac": 0.05})


def test_autonomy_verified_promotion_defaults_on_and_env_opts_out():
    # the verified-promotion rung GRADUATED to default-ON on the L4 powered gate (+0.243
    # success, FSR 0.0 at power; the §9.9 graduation exception). The operator opts OUT via
    # env, which coerces like every bool field. Receipt authenticity is out of scope by
    # design (receipts are unsigned; the ingest door verifies no signature).
    assert Config.load(db_path=":memory:").autonomy.verified_promotion is True
    off = Config.load(db_path=":memory:",
                      env={"HIVE_AUTONOMY__VERIFIED_PROMOTION": "false"})
    assert off.autonomy.verified_promotion is False


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
        cfg.recall.tau_serve = 0.9   # type: ignore[misc]


def test_frozen_root_raises():
    cfg = Config.load(db_path=":memory:")
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.recall = None   # type: ignore[misc]


# ── fail-fast validation ──────────────────────────────────────────────────────
def test_tau_serve_default_is_product_posture():
    # the absolute-relevance serve floor ships ACTIVE at 0.70 (the calibration-grounded
    # prior posture): fields with a top-1 cosine ≥ 0.70 serve, weaker fields abstain.
    assert Config.load(db_path=":memory:").recall.tau_serve == 0.70


def test_tau_serve_bounds():
    with pytest.raises(ValueError, match=r"tau_serve"):
        Config.load(recall={"tau_serve": 0.0})
    with pytest.raises(ValueError, match=r"tau_serve"):
        Config.load(recall={"tau_serve": 1.5})
    for bad in (float("inf"), float("nan")):
        with pytest.raises(ValueError, match=r"tau_serve"):
            Config.load(recall={"tau_serve": bad})
    # the boundary 1.0 is legal (only an exact-match field serves)
    assert Config.load(recall={"tau_serve": 1.0}).recall.tau_serve == 1.0


def test_k_min_default_and_bounds():
    # the quorum floor ships at 1 (a single relevant hit serves); k_min < 1 is rejected.
    assert Config.load(db_path=":memory:").recall.k_min == 1
    with pytest.raises(ValueError, match=r"k_min"):
        Config.load(recall={"k_min": 0})
    with pytest.raises(ValueError, match=r"k_min"):
        Config.load(recall={"k_min": -2})
    assert Config.load(recall={"k_min": 3}).recall.k_min == 3


def test_tau_serve_env_coerces_to_float():
    # HIVE_RECALL__TAU_SERVE is coerced to float by the existing _annotation_base("float") path
    # (no coercer change), proving the operator can calibrate the floor from the environment.
    cfg = Config.load(db_path=":memory:", env={"HIVE_RECALL__TAU_SERVE": "0.55"})
    assert cfg.recall.tau_serve == 0.55


def test_db_path_required():
    with pytest.raises(ValueError, match=r"db_path"):
        Config.load(db_path="")


def test_geometry_group_is_gone():
    # the compression-dim group was removed (the embedder emits the model's native dim) — an
    # override naming it is now an unknown-group error, proving the knob can't be set.
    with pytest.raises(ValueError, match=r"unknown config group|geometry"):
        Config.load(geometry={"d": 768})


def test_backup_keep_at_least_one():
    with pytest.raises(ValueError, match=r"backup_keep"):
        Config.load(retention={"backup_keep": 0})


# ── ConflictConfig group (detection ON; one empirical knob τ; no suppress knob) ──
def test_conflict_detection_defaults_and_no_suppress_knob():
    cfg = Config.load(db_path=":memory:")
    assert cfg.conflict.enabled is True           # detection surfaces ship ON (fleet flags conflicts)
    # 0.80: measured to sit in the gap between distinct same-subsystem facts (~0.69) and
    # genuine paraphrase/contradiction pairs (~0.81-0.87) — calibratable per deployment.
    assert cfg.conflict.tau == 0.80
    assert cfg.conflict.top_n == 10
    # serve-time pruning is no longer a ConflictConfig switch — it is folded into the
    # unconditional select_served stage (an unknown override field now fails fast).
    assert not hasattr(cfg.conflict, "suppress")
    with pytest.raises(ValueError, match=r"suppress|unknown config override"):
        Config.load(conflict={"suppress": True})


def test_conflict_enabled_via_env():
    cfg = Config.load(db_path=":memory:", env={"HIVE_CONFLICT__ENABLED": "true"})
    assert cfg.conflict.enabled is True


# ── SuspectConsensusConfig group (detection-only worklist; request-flag gated) ──
def test_suspect_consensus_defaults():
    cfg = Config.load(db_path=":memory:")
    assert cfg.suspect_consensus.n_eff_frac_max == 0.5
    assert cfg.suspect_consensus.top_n == 10


def test_suspect_consensus_has_no_enabled_toggle():
    # the worklist is gated SOLELY by the hive_health(include_suspect_consensus) request flag —
    # there is no config `enabled` bool (it was removed; the default is baked always-on). An
    # override naming it fails fast as an unknown field.
    cfg = Config.load(db_path=":memory:")
    assert not hasattr(cfg.suspect_consensus, "enabled")
    with pytest.raises(ValueError, match=r"enabled|unknown config override"):
        Config.load(suspect_consensus={"enabled": True})


def test_suspect_consensus_bounds():
    with pytest.raises(ValueError, match=r"n_eff_frac_max"):
        Config.load(suspect_consensus={"n_eff_frac_max": 0.0})
    with pytest.raises(ValueError, match=r"n_eff_frac_max"):
        Config.load(suspect_consensus={"n_eff_frac_max": 1.5})
    with pytest.raises(ValueError, match=r"top_n"):
        Config.load(suspect_consensus={"top_n": 0})


# ── decorrelated selection: overscan knob (select is unconditional, no knob) ───
def test_overscan_default_and_bounds_and_no_select_knob():
    cfg = Config.load(db_path=":memory:")
    assert cfg.recall.overscan == 3
    # decorrelated selection is UNCONDITIONAL — the `select` config knob was removed; an
    # override naming it fails fast as an unknown field.
    assert not hasattr(cfg.recall, "select")
    with pytest.raises(ValueError, match=r"select|unknown config override"):
        Config.load(recall={"select": False})
    # overscan must be >= 1, and is env-coercible.
    with pytest.raises(ValueError, match=r"overscan"):
        Config.load(recall={"overscan": 0})
    cfg2 = Config.load(db_path=":memory:", env={"HIVE_RECALL__OVERSCAN": "5"})
    assert cfg2.recall.overscan == 5


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


# ── the agi group is DELETED (U4: no AGI mode, no sentinel — CT-12 mirror) ─────
def test_agi_group_is_deleted_and_env_degrades_to_unknown_group_warn():
    # HIVE_AGI__MODE is no longer a live switch: it hits the unknown-group WARN path and is
    # ignored — the loaded Config carries NO agi group at all.
    cfg = Config.load(db_path=":memory:", env={"HIVE_AGI__MODE": "true"})
    assert not hasattr(cfg, "agi")


def test_agi_explicit_override_raises_unknown_group():
    # the highest-precedence layer stays fail-fast: naming the deleted group is a caller bug.
    with pytest.raises(ValueError, match=r"unknown config group override 'agi'"):
        Config.load(agi={"mode": True})


# ── SecretScanConfig group (the secret-floor operator opt-OUT; default ON, byte-inert) ─
def test_secret_scan_enabled_defaults_on():
    # the credential floor is ON by default — disabling it LOOSENS a safety gate, so the
    # loosened (bypass) state is the one that takes an explicit opt-in. Default = the SAFE
    # direction (floor on), the same posture as AGI_MODE with inverted polarity (THEORY §9.9).
    cfg = Config.load(db_path=":memory:")
    assert cfg.secret_scan.enabled is True


def test_secret_scan_enabled_via_env_coerces_bool():
    # the operator opt-OUT: HIVE_SECRET_SCAN__ENABLED coerces through the existing bool path.
    for falsy in ("false", "0", "no", "off"):
        cfg = Config.load(db_path=":memory:", env={"HIVE_SECRET_SCAN__ENABLED": falsy})
        assert cfg.secret_scan.enabled is False
    for truthy in ("true", "1", "yes", "on"):
        cfg = Config.load(db_path=":memory:", env={"HIVE_SECRET_SCAN__ENABLED": truthy})
        assert cfg.secret_scan.enabled is True


def test_secret_scan_explicit_override():
    assert Config.load(
        db_path=":memory:", secret_scan={"enabled": False}).secret_scan.enabled is False


def test_secret_scan_unknown_field_typo_raises():
    # a typo in the highest-precedence layer fails fast (never silently leaves the floor at a
    # posture the operator did not mean).
    with pytest.raises(ValueError, match=r"unknown config override|enabledd"):
        Config.load(secret_scan={"enabledd": False})


def test_secret_scan_group_is_frozen():
    cfg = Config.load(db_path=":memory:")
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.secret_scan.enabled = False   # type: ignore[misc]


# ── config → embedder threading (torch-free: construction does not load the model) ────
def test_build_provider_sources_native_dim():
    # the embedder's d is the model's NATIVE dim (sourced from native_dim_for), not a config
    # knob — construction is lazy (no model load), so this proves the wiring without the model.
    from hive.adapters.embedding.factory import build_provider, native_dim_for
    cfg = Config.load(db_path=":memory:")
    assert build_provider(cfg).d == native_dim_for(cfg.embedding.model) == 1024

# ── the census group is DELETED (U4: canonical refs live on the repo registry) ────
def test_census_group_is_deleted_and_env_degrades_to_unknown_group_warn():
    cfg = Config.load(db_path=":memory:", env={"HIVE_CENSUS__CANONICAL_REF": "master"})
    assert not hasattr(cfg, "census")


def test_census_explicit_override_raises_unknown_group():
    with pytest.raises(ValueError, match=r"unknown config group override 'census'"):
        Config.load(census={"canonical_ref": "main"})


# ── SyncConfig group (v3: loop knobs only — the repo registry owns WHICH repos) ──
def test_sync_defaults():
    cfg = Config.load(db_path=":memory:")
    assert cfg.sync.interval_s == 60
    assert cfg.sync.webhook_secret == ""
    assert cfg.sync.mirror_dir == ""             # "" ⇒ /data/sync/mirror at the service


def test_sync_env_and_override():
    env = {"HIVE_SYNC__INTERVAL_S": "30", "HIVE_SYNC__WEBHOOK_SECRET": "hooksecret"}
    cfg = Config.load(db_path=":memory:", env=env)
    assert cfg.sync.interval_s == 30
    assert cfg.sync.webhook_secret == "hooksecret"
    assert Config.load(db_path=":memory:",
                       sync={"mirror_dir": "/m"}).sync.mirror_dir == "/m"


def test_sync_interval_floor():
    with pytest.raises(ValueError):
        Config.load(db_path=":memory:", sync={"interval_s": 4})
    assert Config.load(db_path=":memory:",
                       sync={"interval_s": 5}).sync.interval_s == 5


def test_sync_webhook_secret_is_standalone_valid():
    # the partial-config guard is DELETED: the webhook nudge is GLOBAL (one nudge wakes the
    # loop for every registered repo), so a secret with no other sync setting is a complete,
    # valid config — never a refused half-install.
    cfg = Config.load(db_path=":memory:", sync={"webhook_secret": "hook-value"})
    assert cfg.sync.webhook_secret == "hook-value"


def test_sync_deleted_fields_raise_as_unknown_overrides():
    # repo_url / token / verify_candidates left the group (the store's repo registry +
    # per-row token_env indirection replaced them): an explicit override naming one is a
    # caller bug and fails fast.
    for dead in ({"repo_url": "x"}, {"token": "t"}, {"verify_candidates": True}):
        with pytest.raises(ValueError, match=r"unknown config override"):
            Config.load(sync=dead)


def test_sync_deleted_env_vars_are_ignored_not_crash():
    # leftover env from a v2 deployment degrades to the unknown-field WARN — ignored, and
    # never echoed (the value could be a credential).
    env = {"HIVE_SYNC__REPO_URL": "https://example.invalid/x.git",
           "HIVE_SYNC__TOKEN": "tok-value",
           "HIVE_SYNC__VERIFY_CANDIDATES": "true"}
    cfg = Config.load(db_path=":memory:", env=env)
    for dead in ("repo_url", "token", "verify_candidates"):
        assert not hasattr(cfg.sync, dead)
    assert cfg.sync.interval_s == 60             # the surviving knobs still default


def test_sync_group_is_frozen():
    cfg = Config.load(db_path=":memory:")
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.sync.interval_s = 99   # type: ignore[misc]
