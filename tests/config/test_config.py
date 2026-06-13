"""P1.9 — M11 Config: frozen 4-layer Config.load + fail-fast validation + env
namespacing + reload-tier state machine.

Locked assertions for the config contract.

Grounded deviations (built-decision-wins, documented in the deliverable):
- ``db_path`` defaults to ``":memory:"`` (ephemeral, cannot corrupt a warm store) so the
  locked ``Config.load(producer={...}) succeeds`` assertion holds with no db_path; an EMPTY
  string is the fail-fast trigger for ``test_db_path_required``. The "no silent default"
  prose guards against silently corrupting a persistent store — ``":memory:"`` cannot.
- ``utility.isolation_frac`` is tier **A**, not B.
"""
from __future__ import annotations

import dataclasses

import pytest

from hive.app.config import Config, TierViolation, diff_tier, reload


# ── happy path / defaults ─────────────────────────────────────────────────────
def test_defaults_match_spec_geometry():
    cfg = Config.load(db_path=":memory:")
    assert cfg.geometry.d == 256
    assert cfg.recall.H_frac_max == 0.5
    assert cfg.recall.recall_top_n == 10
    assert cfg.embedding.st_projection_head == "pca"
    assert cfg.embedding.model == "BAAI/bge-small-en-v1.5"
    assert cfg.index.backend == "exhaustive"
    assert cfg.retention.backup_keep == 30


def test_producer_stamp_trailer_defaults_to_credit_v2():
    # The trailer convention v2: agents stamp Hive-Credit (trace + episode ids);
    # the old Hive-Trace key is inert-but-counted by the origin scanner.
    assert Config.load(toml_path=None, env={}).producer.stamp_trailer == "Hive-Credit"


# ── [A4] ε relocation: recall.epsilon_explore validated, producer has none ─────
def test_recall_epsilon_validated_positive():
    with pytest.raises(ValueError, match=r"recall\.epsilon_explore"):
        Config.load(recall={"epsilon_explore": 0.0})
    # ε lives ONLY on recall — the slimmed producer group never had epsilon_explore ([A4])
    assert hasattr(Config.load().producer, "epsilon_explore") is False


def test_recall_epsilon_negative_rejected():
    with pytest.raises(ValueError, match=r"recall\.epsilon_explore"):
        Config.load(recall={"epsilon_explore": -0.1})


# ── [A5] isolation fraction default ───────────────────────────────────────────
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


# ── 4-layer precedence ────────────────────────────────────────────────────────
def test_layering_precedence(tmp_path):
    toml = tmp_path / "hive.toml"
    toml.write_text(
        "[recall]\nrecall_top_n = 11\n"        # field set at layer 2 AND layers 3,4 below
        "[retention]\nbackup_keep = 20\n"      # field set ONLY at layer 2 — must survive
    )
    env = {"HIVE_RECALL__RECALL_TOP_N": "12"}  # layer 3
    cfg = Config.load(
        db_path=":memory:", toml_path=str(toml), env=env,
        recall={"recall_top_n": 13},           # layer 4 (explicit) — wins
    )
    assert cfg.recall.recall_top_n == 13       # explicit override beats env beats toml beats default
    assert cfg.retention.backup_keep == 20     # layer-2-only field survived layers 3/4 being unset


def test_env_beats_toml_no_override(tmp_path):
    # the MIDDLE two layers' order must be pinned: a field set at toml AND env with NO explicit
    # override must take the ENV value (env > toml). A toml<env inversion fails here.
    toml = tmp_path / "hive.toml"
    toml.write_text("[recall]\nrecall_top_n = 11\n")
    env = {"HIVE_RECALL__RECALL_TOP_N": "12"}
    cfg = Config.load(db_path=":memory:", toml_path=str(toml), env=env)
    assert cfg.recall.recall_top_n == 12   # env (layer 3) beats toml (layer 2)


def test_provider_field_routes_to_embedding_group(tmp_path):
    # `provider` now lives only in the embedding group (producer.provider went with the
    # producer strip); pin that the group-scoped env matcher routes it there, not elsewhere.
    cfg = Config.load(db_path=":memory:", env={"HIVE_EMBEDDING__PROVIDER": "local_st"})
    assert cfg.embedding.provider == "local_st"


def test_unknown_override_field_raises():
    # a typo in the highest-precedence layer must fail fast, not silently leave the floor default
    with pytest.raises(ValueError, match=r"H_frac_maxx|unknown config override"):
        Config.load(recall={"H_frac_maxx": 0.4})


def test_layer2_only_field_beats_default(tmp_path):
    toml = tmp_path / "hive.toml"
    toml.write_text("[geometry]\nd = 384\n")
    cfg = Config.load(db_path=":memory:", toml_path=str(toml))
    assert cfg.geometry.d == 384


def test_missing_toml_is_env_only(tmp_path):
    cfg = Config.load(db_path=":memory:", toml_path=str(tmp_path / "absent.toml"))
    assert cfg.geometry.d == 256  # no crash; defaults stand


def test_malformed_toml_ignored_with_warn(tmp_path):
    toml = tmp_path / "hive.toml"
    toml.write_text("this is = = not valid toml [[[")
    cfg = Config.load(db_path=":memory:", toml_path=str(toml))  # must not raise
    assert cfg.geometry.d == 256


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


def test_projection_head_random_rejected():
    with pytest.raises(ValueError, match=r"st_projection_head|pca"):
        Config.load(embedding={"st_projection_head": "random"})


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


# ── reload tier state machine ─────────────────────────────────────────────────
def test_tier_A_hot_swap_allowed():
    old = Config.load(db_path=":memory:")
    new = Config.load(db_path=":memory:", retention={"backup_keep": 15})  # tier A
    assert reload(old, new) is new


def test_tier_B_hot_swap_allowed():
    old = Config.load(db_path=":memory:")
    new = Config.load(db_path=":memory:", recall={"H_frac_max": 0.4})     # tier B
    assert reload(old, new) is new


def test_tier_C_restart_refused():
    old = Config.load(db_path=":memory:")
    # runtime.tenant_id is tier C (restart) — a live swap must be refused
    newc = Config.load(db_path=":memory:", runtime={"tenant_id": "other"})
    with pytest.raises(TierViolation, match=r"restart|tier C|tenant_id"):
        reload(old, newc)


def test_tier_D_migration_refused():
    old = Config.load(db_path=":memory:")
    newd = Config.load(db_path=":memory:", geometry={"d": 384})  # tier D
    with pytest.raises(TierViolation, match=r"re-embed|migration|tier D|geometry\.d"):
        reload(old, newd)


def test_diff_tier_strictest_governs():
    old = Config.load(db_path=":memory:")
    # one tier-A change (backup_keep) AND one tier-D change (geometry.d)
    new = Config.load(db_path=":memory:", retention={"backup_keep": 15}, geometry={"d": 384})
    assert diff_tier(old, new) == "D"
    # the SURFACED remediation must be the strictest offender's (tier-D re-embed), not the
    # looser one — pins reload()'s offender-selection sort sign.
    with pytest.raises(TierViolation, match=r"geometry\.d|re-embed|migration"):
        reload(old, new)


def test_mixed_C_and_D_surfaces_D_remedy():
    old = Config.load(db_path=":memory:")
    new = Config.load(db_path=":memory:", runtime={"tenant_id": "x"}, geometry={"d": 384})
    assert diff_tier(old, new) == "D"
    with pytest.raises(TierViolation, match=r"geometry\.d|re-embed|migration"):
        reload(old, new)


def test_isolation_frac_is_tier_A():
    # [A5] locked: utility.isolation_frac is tier A (a B/C/D mistake would change reload semantics)
    from hive.app.config import RELOAD_TIER
    assert RELOAD_TIER["utility.isolation_frac"] == "A"
    old = Config.load(db_path=":memory:")
    new = Config.load(db_path=":memory:", utility={"isolation_frac": 0.10})
    assert diff_tier(old, new) == "A"
    assert reload(old, new) is new   # tier A ⇒ accepted


def test_recall_hybrid_defaults_off_and_is_tier_C():
    # hybrid ships OFF by construction — flipping the default is the live-risk
    # mutation (the lexical channel must never activate without the CI evidence).
    cfg = Config.load(db_path=":memory:")
    assert cfg.recall.hybrid is False
    # tier C: changes index wiring + the read path ⇒ restart, never a hot swap
    from hive.app.config import RELOAD_TIER
    assert RELOAD_TIER["recall.hybrid"] == "C"
    new = Config.load(db_path=":memory:", recall={"hybrid": True})
    assert new.recall.hybrid is True
    assert diff_tier(cfg, new) == "C"
    with pytest.raises(TierViolation, match=r"recall\.hybrid|tier C|restart"):
        reload(cfg, new)


def test_recall_hybrid_env_coercion():
    # the operational activation path: HIVE_RECALL__HYBRID=true + restart
    cfg = Config.load(db_path=":memory:", env={"HIVE_RECALL__HYBRID": "true"})
    assert cfg.recall.hybrid is True


def test_recall_drafts_defaults_off_and_is_tier_C():
    # self-quarantine resurfacing ships OFF (a new read channel ships byte-inert);
    # flipping it changes the read path ⇒ restart, never a hot swap.
    cfg = Config.load(db_path=":memory:")
    assert cfg.recall.drafts is False
    from hive.app.config import RELOAD_TIER
    assert RELOAD_TIER["recall.drafts"] == "C"
    new = Config.load(db_path=":memory:", recall={"drafts": True})
    assert new.recall.drafts is True
    assert diff_tier(cfg, new) == "C"
    with pytest.raises(TierViolation, match=r"recall\.drafts|tier C|restart"):
        reload(cfg, new)


def test_recall_draft_tau_default_and_hot_swappable():
    # the relevance floor mirrors shadow_tau: default 0.6, tier B (hot-swap), so an
    # operator can tune it live without a restart (and without a re-embed).
    cfg = Config.load(db_path=":memory:")
    assert cfg.recall.draft_tau == 0.6
    from hive.app.config import RELOAD_TIER
    assert RELOAD_TIER["recall.draft_tau"] == "B"
    new = Config.load(db_path=":memory:", recall={"draft_tau": 0.8})
    assert diff_tier(cfg, new) == "B"
    assert reload(cfg, new) is new                         # tier B ⇒ accepted live


def test_recall_draft_tau_validated_in_unit_interval():
    for bad in (0.0, -0.1, 1.5, float("nan")):
        with pytest.raises(ValueError, match=r"recall\.draft_tau"):
            Config.load(recall={"draft_tau": bad})


def test_recall_drafts_env_coercion():
    cfg = Config.load(db_path=":memory:", env={"HIVE_RECALL__DRAFTS": "true"})
    assert cfg.recall.drafts is True


def test_diff_tier_no_change_is_A():
    old = Config.load(db_path=":memory:")
    new = Config.load(db_path=":memory:")
    assert diff_tier(old, new) == "A"   # nothing changed ⇒ loosest ⇒ trivially reloadable
    assert reload(old, new) is new
