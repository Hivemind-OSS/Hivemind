"""End-to-end resolution of schema anchors through resolve_anchor / fingerprint_anchor.

Proves the unchanged resolution pipeline carries schema verdicts: a schema source
flows through resolve_anchor -> _to_anchor_result verbatim, mapping the schema
status set onto the shipped reason codes. The capture seam mints no fingerprint
for a schema anchor (Layer B is inert for schema in v1), and a fingerprint hand-set
on a schema anchor degrades to unverifiable, never stale.
"""

from __future__ import annotations

from pathlib import Path

from hive.combdrift.resolution import fingerprint_anchor, resolve_anchor
from hive.combdrift.types import (
    REASON_FINGERPRINT_VERSION_MISMATCH,
    REASON_OK,
    REASON_PARSE_ERROR,
    REASON_SYMBOL_INDIRECT,
    REASON_SYMBOL_MISSING,
    Anchor,
)


def test_resolve_sql_table_found(tmp_schema_repo: Path):
    res = resolve_anchor(str(tmp_schema_repo), Anchor("db/schema.sql", "users"))
    assert res.found is True
    assert res.reason == REASON_OK
    assert res.location == "db/schema.sql:1"


def test_resolve_sql_column_found(tmp_schema_repo: Path):
    res = resolve_anchor(str(tmp_schema_repo), Anchor("db/schema.sql", "users.email"))
    assert res.found is True
    assert res.reason == REASON_OK


def test_resolve_sql_altered_column_found(tmp_schema_repo: Path):
    # created_at is added by an ALTER statement; the fold must surface it.
    res = resolve_anchor(
        str(tmp_schema_repo), Anchor("db/schema.sql", "users.created_at")
    )
    assert res.found is True


def test_resolve_sql_absent_column_is_symbol_missing(tmp_schema_repo: Path):
    res = resolve_anchor(str(tmp_schema_repo), Anchor("db/schema.sql", "users.phone"))
    assert res.found is False
    assert res.location is None
    assert res.reason.startswith(REASON_SYMBOL_MISSING)
    assert "users.phone" in res.reason


def test_resolve_sql_open_view_member_is_symbol_indirect(tmp_schema_repo: Path):
    res = resolve_anchor(
        str(tmp_schema_repo), Anchor("db/schema.sql", "active_users.x")
    )
    assert res.found is False
    assert res.reason.startswith(REASON_SYMBOL_INDIRECT)
    assert "open_schema" in res.reason


def test_resolve_sql_malformed_is_parse_error(tmp_schema_repo: Path):
    res = resolve_anchor(str(tmp_schema_repo), Anchor("db/broken.sql", "users"))
    assert res.found is False
    assert res.reason.startswith(REASON_PARSE_ERROR)


def test_resolve_json_field_found(tmp_schema_repo: Path):
    res = resolve_anchor(
        str(tmp_schema_repo), Anchor("db/users.schema.json", "users.email")
    )
    assert res.found is True
    assert res.reason == REASON_OK


def test_resolve_json_absent_field_is_symbol_missing(tmp_schema_repo: Path):
    res = resolve_anchor(
        str(tmp_schema_repo), Anchor("db/users.schema.json", "users.phone")
    )
    assert res.found is False
    assert res.reason.startswith(REASON_SYMBOL_MISSING)


# --- capture seam: schema anchors are inert to Layer B ------------------------


def test_fingerprint_anchor_returns_none_for_schema(tmp_schema_repo: Path):
    repo = str(tmp_schema_repo)
    assert fingerprint_anchor(repo, Anchor("db/schema.sql", "users.email")) is None
    assert (
        fingerprint_anchor(repo, Anchor("db/users.schema.json", "users.email")) is None
    )


def test_fingerprint_set_on_schema_anchor_is_unverifiable_never_stale(
    tmp_schema_repo: Path,
):
    # A schema lookup carries interface=None, so a hand-set fingerprint compares
    # as version-mismatch (unverifiable) — the symbol exists and is never stale.
    token = "combdrift-fp/1:func(req=0,max=0,star=0,kw=0,kwo=0,gen=0,dec=,base=0)"
    res = resolve_anchor(
        str(tmp_schema_repo), Anchor("db/schema.sql", "users.email", token)
    )
    assert res.found is True
    assert res.reason.startswith(REASON_FINGERPRINT_VERSION_MISMATCH)
