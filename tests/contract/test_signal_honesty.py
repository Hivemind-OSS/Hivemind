"""CT-18 — an emitted signal states only what its emitter measured.

Two boundaries share one defect class: a component reports a verdict about a fact it
never observed. This suite pins the honest form of each.

  - the boot env WARN (I6/I7): the config layer sees only its OWN groups/fields, so a
    ``HIVE_*`` key it does not recognize is "not a config field", NEVER "ignored" — two
    of those keys (``HIVE_SYNC__TOKEN``, ``HIVE_STORE__DB_PATH``) are read straight from
    the environment by the sync daemon and the tool entry points. A genuinely mistyped
    knob still WARNs loudly (the level is the loudness contract), and no WARN ever
    echoes a value.
  - the fleet-default credential (I8): the subject of that WARN is provably live — a
    registry row with an empty ``token_env`` fetches with ``HIVE_SYNC__TOKEN``'s value,
    asserted through the mirror's own recorded remote, never through a log line.
  - the tracked operator template (I10): ``.env.example`` advertises only keys that
    something actually reads.

Real surfaces only: ``hive.tools.entrypoint.main`` over a real ``build_container``,
``SyncService.tick`` over a REAL local git origin, and the tracked file itself.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from hive.app.config import Config
from tests.contract.conftest import (  # noqa: F401 — Origin/git are pytest-fixture kin
    Origin,
    git,
    make_syncer,
    meta_value,
    mirror_is_git,
    register_repo,
)

FLEET_TOKEN_VAR = "HIVE_SYNC__TOKEN"
FLEET_TOKEN = "ct18-fleet-default-token-aaaa"

# Every HIVE_* var consumed OUTSIDE the config groups, with the site that reads it.
# This literal is the test-local substitute for a runtime allowlist: config.py cannot
# import its consumers (they import it), so the derivation lives HERE, where drift is a
# red suite rather than a mislabelled log line.
EXTERNALLY_CONSUMED = {
    # hive/app/sync.py:_DEFAULT_TOKEN_ENV — the fleet-default git credential
    "HIVE_SYNC__TOKEN",
    # hive/tools/entrypoint.py:_resolve_env, healthcheck/backupctl/censusctl/repoctl
    "HIVE_STORE__DB_PATH",
    # hive/tools/entrypoint.py:_resolve_env
    "HIVE_TENANT_ID",
    "HIVE_AGENT_ID",
    # hive/tools/entrypoint.py:_resolve_max_body
    "HIVE_HTTP_MAX_BODY_BYTES",
    # hive/app/container.py — the engine home the sync loop spawns against
    "HIVE_EDGE_HOME",
}

_ENV_KEY = re.compile(r"^\s*#?\s*(HIVE_[A-Z0-9_]+)\s*=")


def _boot(db: str, env: dict) -> int:
    """Boot the REAL entrypoint state machine with an injected env. Serve is injected
    so boot returns instead of blocking. The boot's own observability owns the "hive"
    logger (``propagate=False`` + its JSON stderr handler), so the rows are read back
    off stderr as EMITTED — the operator's actual view, not an in-process proxy."""
    from hive.app.container import build_container
    from hive.tools import entrypoint
    from tests.fakes._fakes import FakeWarmProvider

    def build_boot(cfg, *, tenant_id, agent_id):
        return build_container(
            cfg, tenant_id=tenant_id, agent_id=agent_id, embedder=FakeWarmProvider(d=8)
        )

    full_env = {"HIVE_STORE__DB_PATH": db, **env}
    return entrypoint.main(
        [], env=full_env, build_boot=build_boot, serve=lambda s: None
    )


def _emitted(capsys) -> list[dict]:
    """Every structured row the boot emitted, newest handler format."""
    rows = []
    for line in capsys.readouterr().err.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and "message" in row:
            rows.append(row)
    return rows


def _config_rows(rows: list[dict]) -> list[str]:
    return [str(r["message"]) for r in rows if r.get("logger") == "hive.config"]


# ── I6: no boot row calls an externally-consumed var "ignored" ────────────────


def test_boot_never_calls_an_externally_consumed_var_ignored(tmp_path, capsys):
    """Both branches of the unknown-key handler are exercised at once:
    ``HIVE_SYNC__TOKEN`` is an unknown FIELD of a live group, ``HIVE_STORE__DB_PATH``
    an unknown GROUP — and both are read elsewhere in the process. The config layer
    may say it does not read them; it may not say nothing does."""
    db = str(tmp_path / "hive.db")
    rc = _boot(
        db,
        {
            FLEET_TOKEN_VAR: FLEET_TOKEN,
            "HIVE_SYNC__REPO_URL": "https://example.invalid/o/r.git",
            "HIVE_CENSUS__CANONICAL_REF": "main",
        },
    )
    assert rc == 0, f"a leftover env key never fails boot: rc={rc}"
    rows = _config_rows(_emitted(capsys))
    live = [r for r in rows if FLEET_TOKEN_VAR in r or "HIVE_STORE__DB_PATH" in r]
    assert len(live) == 2, f"the unknown-key rows must NAME the key they saw: {rows}"
    for row in live:
        assert "ignored" not in row.lower(), (
            f"the config layer cannot know another component ignores a var — "
            f"it may only report what IT reads: {row!r}"
        )


def test_boot_env_warns_never_echo_a_value(tmp_path, capsys):
    """The WARN's subject may be a credential, so the row carries the KEY and never
    the value — for a dead key and a live one alike."""
    db = str(tmp_path / "hive.db")
    _boot(
        db,
        {
            FLEET_TOKEN_VAR: FLEET_TOKEN,
            "HIVE_SYNC__REPO_URL": "https://example.invalid/o/secret-repo.git",
        },
    )
    joined = "\n".join(_config_rows(_emitted(capsys)))
    assert joined, "the boot emitted no config rows at all — parsed wrong?"
    assert FLEET_TOKEN not in joined, "a secret env VALUE never rides a log line"
    assert "secret-repo" not in joined, "no env value is echoed, secret or not"


# ── I7: a genuinely mistyped knob is still loud ──────────────────────────────


def test_boot_still_warns_loudly_on_a_mistyped_config_field(tmp_path, capsys):
    """Making the verdict honest must not make the signal quiet: a typo'd knob is
    still a WARNING naming the key, and the real knob keeps its default."""
    db = str(tmp_path / "hive.db")
    rc = _boot(db, {"HIVE_RECALL__TAU_SERVEE": "0.9"})
    assert rc == 0
    rows = _emitted(capsys)
    warns = [
        r
        for r in rows
        if r.get("logger") == "hive.config"
        and str(r.get("level")) == logging.getLevelName(logging.WARNING)
        and "HIVE_RECALL__TAU_SERVEE" in str(r.get("message"))
    ]
    assert warns, (
        "a mistyped config field must stay a loud WARNING naming the key: "
        f"{_config_rows(rows)}"
    )
    cfg = Config.load(db_path=db, env={"HIVE_RECALL__TAU_SERVEE": "0.9"})
    assert cfg.recall.tau_serve == 0.70, "the typo set nothing"


# ── I8: the WARN's subject is provably live ──────────────────────────────────


def test_fleet_default_token_env_is_what_reaches_the_fetch(
    sync_store, tmp_path, monkeypatch
):
    """A registry row with an EMPTY ``token_env`` (the ``hive repo add`` default)
    authenticates with ``HIVE_SYNC__TOKEN``. Asserted through the URL git itself
    recorded for the mirror's origin — the credential's real destination — with the
    transport redirected to a real local origin so every git call stays real."""
    origin = Origin(tmp_path / "remote")
    https_url = "https://example.invalid/o/r.git"
    authed = f"https://x-access-token:{FLEET_TOKEN}@example.invalid/o/r.git"
    for i, url in enumerate((https_url, authed)):
        monkeypatch.setenv("GIT_CONFIG_COUNT", "2")
        monkeypatch.setenv(f"GIT_CONFIG_KEY_{i}", f"url.{origin.url}.insteadOf")
        monkeypatch.setenv(f"GIT_CONFIG_VALUE_{i}", url)
    monkeypatch.setenv(FLEET_TOKEN_VAR, FLEET_TOKEN)

    register_repo(sync_store, "alpha", https_url, canonical_ref="main")
    syncer = make_syncer(sync_store, tmp_path)
    syncer.service.tick()

    assert mirror_is_git(syncer.mirror_base, "alpha", https_url), (
        "the anonymous-looking registry row must have cloned"
    )
    from hive.app.sync import mirror_dirname

    mirror = syncer.mirror_base / mirror_dirname("alpha", https_url)
    recorded = git(mirror, "config", "--get", "remote.origin.url").stdout.strip()
    assert FLEET_TOKEN in recorded, (
        "the fleet-default var IS the credential that reaches the fetch — it is "
        f"anything but ignored: {recorded.replace(FLEET_TOKEN, '<token>')!r}"
    )


def test_fleet_default_token_absent_ticks_anonymous_and_boots_clean(
    sync_store, tmp_path, monkeypatch
):
    """The declared failure behaviour of the fleet-default var: OPTIONAL. Absent ⇒
    an empty token ⇒ an anonymous fetch, which is exactly right for a public or
    local remote. Never a startup failure, never a per-repo error."""
    monkeypatch.delenv(FLEET_TOKEN_VAR, raising=False)
    origin = Origin(tmp_path / "remote")
    register_repo(sync_store, "alpha", origin.url, canonical_ref="main")
    syncer = make_syncer(sync_store, tmp_path)
    syncer.service.tick()

    assert meta_value(sync_store, "sync:alpha:last_error") is None
    assert meta_value(sync_store, "sync:alpha:last_tip") == origin.origin_sha(
        "refs/heads/main"
    )


# ── I10: the tracked template advertises only live knobs ─────────────────────


def test_env_example_advertises_only_live_hive_keys():
    """Every ``HIVE_*`` key in ``.env.example`` — commented example lines included,
    since that is how the template advertises an optional knob — resolves to a live
    config group+field or to a var some other component actually reads."""
    text = Path(__file__).resolve().parents[2].joinpath(".env.example").read_text()
    keys = [
        m.group(1) for m in (_ENV_KEY.match(line) for line in text.splitlines()) if m
    ]
    assert keys, ".env.example advertises no HIVE_* key at all — parsed wrong?"

    cfg = Config.load(db_path=":memory:", env={})
    dead = []
    for key in keys:
        if key in EXTERNALLY_CONSUMED:
            continue
        body = key[len("HIVE_") :]
        group_tok, _, field_tok = body.partition("__")
        group = getattr(cfg, group_tok.lower(), None)
        if group is None or not hasattr(group, field_tok.lower()):
            dead.append(key)
    assert not dead, (
        f"the operator template advertises knobs that set nothing: {dead} — "
        "a template key must be a live config field or a documented external read"
    )
