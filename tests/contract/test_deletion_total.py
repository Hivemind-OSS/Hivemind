"""CT-12 — the AGI/vouch deletion is TOTAL (plan §4 intent 12, §7).

``AGI_OVERRIDE`` / ``HIVE_AGI__MODE`` / ``approved_by`` are unreachable on the
runtime surface: the env var is an unknown-group WARN (ignored), an
``approved_by`` tool arg is ignored-extra (never an AGI refusal), the
``provenance``/``approved_by``/``approved_ts``/``anchor``/``tags`` episode
columns are gone from the DDL, the ``hive.domain.agi`` / ``hive.domain.provenance``
modules are deleted, and no runtime source line mentions the sentinel.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib

from hive.app.config import Config
from tests.contract.conftest import call, payload, trust_of, write, write_ok

DEAD_EPISODE_COLUMNS = {"provenance", "approved_by", "approved_ts", "anchor", "tags"}


def test_hive_agi_mode_env_is_unknown_group_ignored():
    cfg = Config.load(env={"HIVE_AGI__MODE": "true"})
    assert not hasattr(cfg, "agi"), (
        "the agi config group is DELETED — HIVE_AGI__MODE must be an "
        "unknown-group WARN, not a live switch"
    )


def test_deleted_sync_and_census_env_vars_are_ignored():
    cfg = Config.load(
        env={
            "HIVE_SYNC__REPO_URL": "https://example.invalid/x.git",
            "HIVE_SYNC__VERIFY_CANDIDATES": "true",
            "HIVE_SYNC__TOKEN": "tok",
            "HIVE_CENSUS__CANONICAL_REF": "master",
        }
    )
    assert not hasattr(cfg, "census"), "the census config group is DELETED"
    for dead in ("repo_url", "token", "verify_candidates"):
        assert not hasattr(cfg.sync, dead), (
            f"sync.{dead} is DELETED (the registry replaced it)"
        )


def test_approved_by_tool_arg_is_ignored_extra_never_an_agi_refusal(rig):
    eid = write_ok(
        rig.server,
        "a memory written with a stale client arg",
        approved_by="AGI_OVERRIDE",
    )
    assert trust_of(rig.store, eid) == "provisional", (
        "an extra approved_by arg is IGNORED — the write lands provisional like "
        "any other"
    )
    env = payload(
        call(
            rig.server,
            "hive_prune",
            {"episode_id": eid, "approved_by": "AGI_OVERRIDE"},
        )
    )
    assert env.get("status") == "noop", (
        f"a healthy target pruned with the dead sentinel is the GATE noop — "
        f"never the AGI refusal: {env}"
    )
    assert "agi_mode" not in env
    assert "AGI" not in json.dumps(env), f"no AGI vocabulary on the wire: {env}"


def test_write_envelope_carries_no_approver(rig):
    env = write(rig.server, "an approver-less memory")
    assert "approved_by" not in env
    assert env.get("trust") == "provisional"


def test_episodes_ddl_dropped_the_dead_columns(rig):
    cols = {r["name"] for r in rig.store.conn.execute("PRAGMA table_info(episodes)")}
    dead = DEAD_EPISODE_COLUMNS & cols
    assert not dead, (
        f"schema v3 drops the anchor/provenance/approver/tags columns — still "
        f"present: {sorted(dead)}"
    )
    ddl = rig.store.conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='episodes'"
    ).fetchone()["sql"]
    assert "provenance" not in ddl and "approved_by" not in ddl


def test_agi_and_provenance_modules_are_deleted():
    assert importlib.util.find_spec("hive.domain.agi") is None, (
        "hive/domain/agi.py must be deleted (plan §7)"
    )
    assert importlib.util.find_spec("hive.domain.provenance") is None, (
        "hive/domain/provenance.py must be deleted (plan §7)"
    )


def test_runtime_surface_greps_clean_of_the_sentinel():
    import hive

    root = pathlib.Path(hive.__file__).parent
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        if "AGI_OVERRIDE" in text or "HIVE_AGI__MODE" in text:
            offenders.append(str(path.relative_to(root)))
    assert offenders == [], (
        f"the reserved sentinel survives on the runtime surface: {offenders}"
    )


def test_purity_surface_intact_no_vouch_writes():
    # the domain purity posture survives the deletion: lifecycle/admission still
    # import clean and expose no approver-shaped write path
    import inspect

    from hive.domain.admission import AdmissionService

    params = inspect.signature(AdmissionService.write).parameters
    assert "approved_by" not in params, (
        "AdmissionService.write must lose its approver parameter (plan §6 step 1)"
    )
