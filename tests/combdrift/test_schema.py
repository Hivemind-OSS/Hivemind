"""Contract tests for the schema-source resolver (combdrift/schema.py) and its dispatch.

Pure, filesystem-free tests against `schema.resolve_symbol` and the `symbols.locate`
routing seam. The dispatch tests pin the suffix gate — including the no-regression
boundary that keeps a bare `.json` file unsupported. The per-source tests pin the
absent-vs-indirect rule that is the zero-false-stale heart for schema targets: a
container/member is `missing` (→ stale) only when it is absent from a closed,
fully-parsed, non-open, non-inheriting, non-uncertain declared schema; every form
of openness or uncertainty routes to `indirect`/`parse_error` (→ unverifiable).
"""

from __future__ import annotations

from hive.combdrift import schema, symbols


# --- dispatch: which file suffixes route to the schema resolver ---------------


def test_sql_extension_routes_to_schema():
    # A .sql file is resolved by the schema seam, never reported unsupported.
    lookup = symbols.locate("CREATE TABLE users (id int);", "db/schema.sql", "users")
    assert lookup.status != "unsupported"


def test_schema_json_suffix_routes_to_schema():
    lookup = symbols.locate(
        '{"properties": {"id": {}}}', "db/users.schema.json", "users"
    )
    assert lookup.status != "unsupported"


def test_bare_json_still_unsupported():
    # The shipped contract: a bare .json file is unsupported (never read as a schema).
    lookup = symbols.locate('{"k": 1}', "src/data.json", None)
    assert lookup.status == "unsupported"


# --- JSON-Schema / Mongo $jsonSchema source -----------------------------------


def _json(source: str, symbol: str | None):
    return schema.resolve_symbol(source, schema.JSON_SCHEMA, symbol)


def test_json_present_field_is_found():
    r = _json('{"properties": {"email": {}, "id": {}}}', "users.email")
    assert r.status == "found"


def test_json_container_named_for_file_stem():
    # A single object schema is one container that matches any anchor name; the
    # member is what is actually resolved.
    src = '{"properties": {"email": {}}, "additionalProperties": false}'
    assert _json(src, "users").status == "found"
    assert _json(src, "users.email").status == "found"


def test_json_additionalprops_false_absent_field_is_missing():
    # A closed document schema: an absent field is provably absent -> missing (stale).
    src = '{"properties": {"id": {}}, "additionalProperties": false}'
    r = _json(src, "users.phone")
    assert r.status == "missing"


def test_json_default_open_absent_field_is_indirect():
    # JSON-Schema additionalProperties DEFAULTS to true: an absent field in a
    # schema with no explicit additionalProperties is NOT provably absent.
    r = _json('{"properties": {"id": {}}}', "users.phone")
    assert r.status == "indirect"
    assert r.detail == "open_schema"


def test_json_dollar_jsonschema_unwrapped():
    # A Mongo validator wraps the schema in $jsonSchema; fields resolve through it.
    src = (
        '{"$jsonSchema": {"bsonType": "object", '
        '"properties": {"email": {}}, "additionalProperties": false}}'
    )
    assert _json(src, "users.email").status == "found"
    assert _json(src, "users.phone").status == "missing"


def test_json_map_of_collections():
    # A {name: schema} map declares one container per key.
    src = (
        '{"users": {"properties": {"email": {}}, "additionalProperties": false}, '
        '"orders": {"properties": {"total": {}}, "additionalProperties": false}}'
    )
    assert _json(src, "users").status == "found"
    assert _json(src, "orders.total").status == "found"
    assert _json(src, "orders.shipping").status == "missing"


def test_json_absent_container_in_map_is_missing():
    src = '{"users": {"properties": {"email": {}}, "additionalProperties": false}}'
    assert _json(src, "customers").status == "missing"
    assert _json(src, "customers.x").status == "missing"


def test_json_no_properties_key_is_open():
    # A schema with no `properties` cannot enumerate members -> absence unprovable.
    r = _json('{"type": "object", "additionalProperties": false}', "users.email")
    assert r.status == "indirect"
    assert r.detail == "open_schema"


def test_json_malformed_is_parse_error():
    assert _json("{not valid json", "users.email").status == "parse_error"


def test_json_non_object_root_is_parse_error():
    # An array / scalar root is not a schema object -> unverifiable, never missing.
    assert _json("[1, 2, 3]", "users.email").status == "parse_error"


def test_json_symbol_none_is_file_presence():
    r = _json('{"properties": {"id": {}}}', None)
    assert r.status == "found"
    assert r.lineno == 1


# --- SQL DDL source -----------------------------------------------------------


def _sql(source: str, symbol: str | None):
    return schema.resolve_symbol(source, schema.SQL, symbol)


def test_sql_table_found():
    r = _sql("CREATE TABLE users (\n  id int,\n  email text\n);", "users")
    assert r.status == "found"
    assert r.lineno == 1  # best-effort: the CREATE line


def test_sql_column_found():
    assert (
        _sql("CREATE TABLE users (id int, email text);", "users.email").status
        == "found"
    )


def test_sql_symbol_none_is_file_presence():
    r = _sql("CREATE TABLE users (id int);", None)
    assert r.status == "found"
    assert r.lineno == 1


def test_sql_absent_table_is_missing_stale():
    # A table never declared in a cleanly-parsed schema is provably absent.
    assert _sql("CREATE TABLE users (id int);", "orders").status == "missing"


def test_sql_absent_column_closed_table_is_missing_stale():
    # A plain CREATE TABLE fully enumerates columns -> an absent column is stale.
    assert (
        _sql("CREATE TABLE users (id int, email text);", "users.phone").status
        == "missing"
    )


def test_sql_alter_add_column_is_found():
    src = "CREATE TABLE users (id int);\nALTER TABLE users ADD COLUMN phone text;"
    assert _sql(src, "users.phone").status == "found"


def test_sql_alter_drop_column_is_missing_stale():
    src = (
        "CREATE TABLE users (id int, email text);\nALTER TABLE users DROP COLUMN email;"
    )
    assert _sql(src, "users.email").status == "missing"


def test_sql_rename_column_old_name_is_missing_new_is_found():
    src = "CREATE TABLE users (email text);\nALTER TABLE users RENAME COLUMN email TO addr;"
    assert _sql(src, "users.email").status == "missing"
    assert _sql(src, "users.addr").status == "found"


def test_sql_drop_table_is_missing():
    src = "CREATE TABLE users (id int);\nDROP TABLE users;"
    assert _sql(src, "users").status == "missing"


def test_sql_view_bare_is_found():
    assert _sql("CREATE VIEW v AS SELECT id FROM users;", "v").status == "found"


def test_sql_select_star_view_member_is_indirect():
    # A view's columns come from its SELECT, which v1 does not resolve -> open.
    r = _sql("CREATE VIEW v AS SELECT * FROM users;", "v.anything")
    assert r.status == "indirect"
    assert r.detail == "open_schema"


def test_sql_ctas_member_is_indirect():
    # CREATE TABLE ... AS SELECT has no column list -> members unknown -> open.
    r = _sql("CREATE TABLE snap AS SELECT * FROM users;", "snap.anything")
    assert r.status == "indirect"
    assert r.detail == "open_schema"


def test_sql_like_clone_absent_member_is_indirect():
    src = "CREATE TABLE t (id int);\nCREATE TABLE c (LIKE t);"
    r = _sql(src, "c.whatever")
    assert r.status == "indirect"
    assert r.detail == "open_schema"


def test_sql_inherits_absent_member_is_indirect():
    src = "CREATE TABLE child (extra int) INHERITS (parent);"
    r = _sql(src, "child.from_parent")
    assert r.status == "indirect"
    assert r.detail == "maybe_inherited"


def test_sql_inherits_present_member_is_found():
    # Presence is always provable, even in an inheriting table.
    src = "CREATE TABLE child (extra int) INHERITS (parent);"
    assert _sql(src, "child.extra").status == "found"


def test_sql_partition_of_member_is_indirect():
    src = "CREATE TABLE part PARTITION OF parent FOR VALUES IN (1);"
    r = _sql(src, "part.from_parent")
    assert r.status == "indirect"
    assert r.detail == "maybe_inherited"


def test_sql_unmodeled_alter_action_is_indirect():
    # A multi-action ALTER falls back to an opaque Command; the touched table's
    # member set is no longer provably complete -> absent member is uncertain.
    src = "CREATE TABLE users (id int);\nALTER TABLE users ADD COLUMN a int, DROP COLUMN b;"
    r = _sql(src, "users.phone")
    assert r.status == "indirect"
    assert r.detail == "uncertain_ddl"


def test_sql_owner_to_command_does_not_taint_recall():
    # ALTER ... OWNER TO is an opaque Command but cannot add/remove a column, so
    # the table stays closed and absent-column detection is preserved.
    src = "CREATE TABLE users (id int);\nALTER TABLE users OWNER TO bob;"
    assert _sql(src, "users.phone").status == "missing"
    assert _sql(src, "users.id").status == "found"


def test_sql_create_index_does_not_taint():
    # CREATE INDEX is irrelevant to table/column existence; the table stays closed.
    src = "CREATE TABLE users (id int);\nCREATE INDEX idx ON users (id);"
    assert _sql(src, "users.phone").status == "missing"
    assert _sql(src, "users.id").status == "found"


def test_sql_garbage_nonddl_is_not_false_stale():
    # Text that parses to a non-DDL node (not a clean DDL fold) must never yield an
    # empty model that false-stales; default-deny routes it to an open namespace.
    r = _sql("this is not sql ;", "users")
    assert r.status != "missing"


def test_sql_malformed_is_parse_error():
    assert _sql("CREATE TABLE (", "users").status == "parse_error"
    assert _sql("%%% garbage %%%", "users.x").status == "parse_error"
