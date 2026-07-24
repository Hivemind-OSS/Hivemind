"""Resolve a memory anchor against a STATIC declared database schema.

Parse-only and language-blind to its caller: given schema source text and a source
kind, build a normalized container/member model and resolve a bare ``T`` (container)
or dotted ``T.C`` (member) into the same status vocabulary ``symbols.locate`` uses.
SQL DDL is parsed by ``sqlglot`` (Postgres dialect) into an AST and folded
(CREATE / ALTER / DROP) in textual order; a ``*.schema.json`` source is parsed by
the stdlib ``json``. No database is ever contacted; sources are data, never executed.

This is the single owner of schema-source parse knowledge. It has no dependency on
``combdrift.symbols`` (the dispatch seam maps the small ``SchemaResolution`` returned here
onto a ``SymbolLookup``), so it stays pure and testable in isolation.

The absent-vs-indirect rule is the zero-false-stale heart: a container/member is
``missing`` (→ stale) ONLY when it is absent from a closed, fully-parsed, non-open,
non-inheriting, non-uncertain declared schema. Every form of openness — an
undeclared-members-allowed document schema, a ``SELECT *`` view, a ``LIKE`` clone, a
table that ``INHERITS``/partitions, an unmodeled DDL statement, or a source that will
not parse — routes to ``indirect``/``parse_error`` (→ unverifiable), never ``stale``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

SchemaStatus = Literal["found", "missing", "indirect", "parse_error"]

# Source kinds the dispatch in symbols.py may pass.
SQL = "sql"
JSON_SCHEMA = "json_schema"


@dataclass(frozen=True, slots=True)
class Container:
    """A declared relation/collection and the members provably present in it."""

    name: str
    kind: str  # "table" | "view" | "collection"
    members: frozenset[str]  # column / field names provably present
    lineno: int | None  # best-effort declaration line; evidence only
    open: bool  # undeclared members allowed → member absence NOT provable
    inherits: bool  # unresolved parent (INHERITS / partition) → member maybe inherited
    uncertain: bool  # an unmodeled DDL action touched it → its members not provable


@dataclass(frozen=True, slots=True)
class SchemaModel:
    """The normalized schema: named containers plus namespace-openness flags."""

    containers: dict[str, Container]
    open_namespace: (
        bool  # source admits undeclared CONTAINERS → container absence not provable
    )
    # A single anonymous object schema (one document collection per file) resolves
    # against this lone container for ANY anchor name — container existence for a
    # one-collection file is trivially true; the valuable check is at the member level.
    solitary: Container | None = None


@dataclass(frozen=True, slots=True)
class SchemaResolution:
    """Outcome of resolving one symbol against a schema source.

    `detail` names the indirection mechanism for an `indirect` status:
    `open_schema` | `maybe_inherited` | `uncertain_ddl` | `dynamic_namespace`.
    """

    status: SchemaStatus
    lineno: int | None = None
    detail: str | None = None


def build_model(source: str, source_kind: str) -> SchemaModel | None:
    """Parse a schema source into a normalized model, or None if it cannot parse.

    None routes to `parse_error` (→ unverifiable); a returned model is always a
    clean parse against which absence may be provable. Total — never raises on
    expected conditions.
    """
    if source_kind == JSON_SCHEMA:
        return _build_json_model(source)
    if source_kind == SQL:
        return _build_sql_model(source)
    return None


def resolve_symbol(
    source: str, source_kind: str, symbol: str | None
) -> SchemaResolution:
    """Build the model and resolve `symbol` (None | 'T' | 'T.C').

    Total — never raises on expected conditions. An unparseable source is
    `parse_error`; every uncertainty routes to `indirect`, never `missing`.
    """
    model = build_model(source, source_kind)
    if model is None:
        return SchemaResolution(status="parse_error")
    if symbol is None:
        return SchemaResolution(status="found", lineno=1)
    return _resolve_in_model(model, symbol)


def _resolve_in_model(model: SchemaModel, symbol: str) -> SchemaResolution:
    """Apply the absent-vs-indirect rule for a bare 'T' or dotted 'T.C'."""
    container_name, dot, member = symbol.partition(".")
    container = _lookup_container(model, container_name)
    if not dot:
        # Bare container T.
        if container is not None:
            return SchemaResolution(status="found", lineno=container.lineno)
        if model.open_namespace:
            return SchemaResolution(status="indirect", detail="dynamic_namespace")
        return SchemaResolution(status="missing")
    # Dotted member T.C.
    if container is None:
        if model.open_namespace:
            return SchemaResolution(status="indirect", detail="dynamic_namespace")
        # The container itself is provably absent → the column claim is stale.
        return SchemaResolution(status="missing")
    if member in container.members:
        return SchemaResolution(status="found", lineno=container.lineno)
    if container.open:
        # Undeclared members are permitted (SELECT * view, LIKE clone, open
        # document schema) → member absence is not provable.
        return SchemaResolution(status="indirect", detail="open_schema")
    if container.uncertain:
        # An unmodeled DDL action touched the container → its members are not
        # provably complete.
        return SchemaResolution(status="indirect", detail="uncertain_ddl")
    if container.inherits:
        # A member may come from an unresolved parent/partition.
        return SchemaResolution(status="indirect", detail="maybe_inherited")
    return SchemaResolution(status="missing")


def _lookup_container(model: SchemaModel, name: str) -> Container | None:
    if model.solitary is not None:
        return model.solitary
    return model.containers.get(name)


# --- JSON-Schema / Mongo $jsonSchema source ----------------------------------

# Keys whose presence marks an object as a JSON-Schema (vs a {name: schema} map).
_SCHEMA_KEYWORDS = frozenset(
    {
        "properties",
        "type",
        "bsonType",
        "required",
        "additionalProperties",
        "patternProperties",
        "$schema",
    }
)


def _build_json_model(source: str) -> SchemaModel | None:
    """Parse a JSON-Schema / Mongo $jsonSchema source into a SchemaModel.

    A single object schema becomes one anonymous (solitary) container; a
    `{name: schema}` map becomes one named container per key. A non-object root,
    or text that is neither a schema nor a clean map, is None (→ parse_error) —
    never an empty model that could false-stale.
    """
    try:
        data = json.loads(source)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    inner = data.get("$jsonSchema")
    root = inner if isinstance(inner, dict) else data
    if _looks_like_schema(root):
        return SchemaModel(
            containers={},
            open_namespace=False,
            solitary=_build_json_container("", root, 1),
        )
    if data and all(isinstance(value, dict) for value in data.values()):
        containers = {
            key: _build_json_container(
                key, _unwrap_jsonschema(value), _json_lineno(source, key)
            )
            for key, value in data.items()
        }
        return SchemaModel(containers=containers, open_namespace=False)
    return None


def _looks_like_schema(obj: dict[str, Any]) -> bool:
    return any(keyword in obj for keyword in _SCHEMA_KEYWORDS)


def _unwrap_jsonschema(obj: dict[str, Any]) -> dict[str, Any]:
    inner = obj.get("$jsonSchema")
    return inner if isinstance(inner, dict) else obj


def _build_json_container(
    name: str, obj: dict[str, Any], lineno: int | None
) -> Container:
    """Model one document schema: its declared fields and whether it is open."""
    props = obj.get("properties")
    if isinstance(props, dict):
        members = frozenset(props.keys())
        # JSON-Schema additionalProperties DEFAULTS to true (open). It is closed
        # only when explicitly false AND no patternProperties admits extra keys.
        is_open = not (
            obj.get("additionalProperties") is False
            and not obj.get("patternProperties")
        )
    else:
        # No enumerable `properties` → members cannot be listed → open.
        members = frozenset()
        is_open = True
    return Container(
        name=name,
        kind="collection",
        members=members,
        lineno=lineno,
        open=is_open,
        inherits=False,
        uncertain=False,
    )


def _json_lineno(source: str, name: str) -> int | None:
    """Best-effort 1-based line of the first occurrence of the quoted `name` key."""
    needle = f'"{name}"'
    for index, line in enumerate(source.splitlines(), start=1):
        if needle in line:
            return index
    return None


# --- SQL DDL source (sqlglot) ------------------------------------------------

_SQL_DIALECT = "postgres"

# Top-level statement types that never add or remove a table or column. Listed
# so a schema dump full of them stays resolvable (recall preserved) rather than
# defaulting to uncertain. Everything NOT recognized here or as a modeled DDL
# statement is treated as uncertain (default-deny), so garbage that happens to
# parse can never yield an empty model that false-stales.
_SQL_IGNORED_STATEMENTS = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Select,
    exp.Set,
    exp.Grant,
    exp.Comment,
    exp.Copy,
)


@dataclass
class _MutContainer:
    """Mutable accumulator folded across statements, frozen into a Container."""

    name: str
    kind: str
    members: set[str]
    lineno: int | None
    open: bool
    inherits: bool
    uncertain: bool = False


def _build_sql_model(source: str) -> SchemaModel | None:
    """Parse and fold SQL DDL into a SchemaModel, or None on a ParseError.

    A single unparseable statement makes the WHOLE source parse_error — a
    malformed statement never masquerades as a deleted object (mirrors the
    stdlib-ast whole-file policy). A statement that parses but is not modeled
    DDL routes to uncertain, never to a silent omission.
    """
    try:
        statements = sqlglot.parse(source, dialect=_SQL_DIALECT)
    except ParseError:
        return None
    builder = _SqlModelBuilder(source)
    for statement in statements:
        if statement is not None:
            builder.apply(statement)
    return builder.model()


class _SqlModelBuilder:
    """Folds CREATE/ALTER/DROP in textual order; default-deny on anything else."""

    def __init__(self, source: str) -> None:
        self._source = source
        self._containers: dict[str, _MutContainer] = {}
        # An unmodeled structural statement (an opaque Command that may touch
        # columns, an ALTER on an undeclared table, or any unrecognized node)
        # makes absence unprovable across the whole source.
        self._uncertain = False

    def apply(self, statement: exp.Expr) -> None:
        if isinstance(statement, exp.Create):
            self._apply_create(statement)
        elif isinstance(statement, exp.Alter):
            self._apply_alter(statement)
        elif isinstance(statement, exp.Drop):
            self._apply_drop(statement)
        elif isinstance(statement, exp.Command):
            if _command_threatens_existence(statement):
                self._uncertain = True
        elif isinstance(statement, _SQL_IGNORED_STATEMENTS):
            return  # non-structural: cannot add or remove a table/column
        else:
            self._uncertain = True  # default-deny: not a provable clean DDL fold

    def _apply_create(self, statement: exp.Create) -> None:
        kind = statement.args.get("kind")
        if kind == "TABLE":
            self._register_table(statement)
        elif kind == "VIEW":
            name = _sql_table_name(statement.this)
            if name is None:
                self._uncertain = True
                return
            # A view's columns come from its SELECT, which v1 does not resolve,
            # so a view is always open (member absence never provable).
            self._containers[name] = _MutContainer(
                name=name,
                kind="view",
                members=set(),
                lineno=_sql_lineno(self._source, name),
                open=True,
                inherits=False,
            )
        # CREATE INDEX / SEQUENCE / SCHEMA / FUNCTION / TYPE … are irrelevant to
        # table/column existence and are ignored.

    def _register_table(self, statement: exp.Create) -> None:
        node = statement.this
        if isinstance(node, exp.Schema):
            table_node, expressions = node.this, node.expressions
        else:
            table_node, expressions = node, []
        name = _sql_table_name(table_node)
        if name is None:
            self._uncertain = True
            return
        columns = {e.name for e in expressions if isinstance(e, exp.ColumnDef)}
        has_like = any(isinstance(e, exp.LikeProperty) for e in expressions)
        # CREATE TABLE ... AS SELECT has no column list -> members unknown -> open.
        is_ctas = isinstance(statement.args.get("expression"), exp.Select)
        inherits = (
            statement.find(exp.InheritsProperty) is not None
            or statement.find(exp.PartitionedOfProperty) is not None
        )
        self._containers[name] = _MutContainer(
            name=name,
            kind="table",
            members=columns,
            lineno=_sql_lineno(self._source, name),
            open=has_like or is_ctas,
            inherits=inherits,
        )

    def _apply_alter(self, statement: exp.Alter) -> None:
        if statement.args.get("kind") not in (None, "TABLE"):
            return  # ALTER VIEW / SEQUENCE / INDEX — not a table-column change
        name = _sql_table_name(statement.this)
        if name is None:
            self._uncertain = True
            return
        container = self._containers.get(name)
        if container is None:
            # An ALTER on a table never CREATEd in this file: its full column set
            # is not knowable here, so absence is not provable.
            self._uncertain = True
            return
        for action in statement.args.get("actions") or []:
            _apply_alter_action(container, action)

    def _apply_drop(self, statement: exp.Drop) -> None:
        if statement.args.get("kind") not in ("TABLE", "VIEW"):
            return  # DROP INDEX / SEQUENCE / CONSTRAINT — irrelevant to existence
        for table_node in statement.find_all(exp.Table):
            name = _sql_table_name(table_node)
            if name is not None:
                self._containers.pop(name, None)

    def model(self) -> SchemaModel:
        containers = {
            name: Container(
                name=mut.name,
                kind=mut.kind,
                members=frozenset(mut.members),
                lineno=mut.lineno,
                open=mut.open,
                inherits=mut.inherits,
                uncertain=mut.uncertain or self._uncertain,
            )
            for name, mut in self._containers.items()
        }
        return SchemaModel(containers=containers, open_namespace=self._uncertain)


def _apply_alter_action(container: _MutContainer, action: exp.Expression) -> None:
    """Fold one ALTER action into a container's member set under the 0-FP rule.

    A member becomes provably absent only via a recognized DROP COLUMN / RENAME
    COLUMN; an unrecognized action flips the container uncertain (→ indirect),
    never a false stale.
    """
    if isinstance(action, exp.ColumnDef):
        container.members.add(action.name)
    elif isinstance(action, exp.Drop) and action.args.get("kind") == "COLUMN":
        container.members.discard(_sql_identifier_name(action.this))
    elif isinstance(action, exp.RenameColumn):
        container.members.discard(_sql_identifier_name(action.this))
        container.members.add(_sql_identifier_name(action.args.get("to")))
    elif isinstance(action, (exp.AlterColumn, exp.AddConstraint, exp.Drop)):
        return  # type/nullability/default/constraint change — not column existence
    else:
        container.uncertain = True  # an unmodeled action → members not provable


def _command_threatens_existence(statement: exp.Command) -> bool:
    """True iff an opaque Command may add a table or add/remove a column.

    A Command is sqlglot's fallback for syntax it cannot model (a multi-action
    ALTER, an exotic CREATE). Only those that could ADD something we would
    otherwise call absent threaten the 0-false-stale property; a harmless
    `ALTER ... OWNER TO` / `GRANT` is left to preserve recall.
    """
    text = statement.sql(dialect=_SQL_DIALECT).upper()
    if "CREATE TABLE" in text:
        return True
    return "ALTER TABLE" in text and any(
        kw in text for kw in (" ADD ", " DROP ", " RENAME ")
    )


def _sql_table_name(node: exp.Expression | None) -> str | None:
    """The unqualified table/view name from a Table or Schema(this=Table) node."""
    if isinstance(node, exp.Schema):
        node = node.this
    if isinstance(node, exp.Table):
        return node.name
    return None


def _sql_identifier_name(node: exp.Expression | None) -> str:
    """The bare identifier name of a Column/Identifier node ('' if unreadable)."""
    return node.name if node is not None else ""


def _sql_lineno(source: str, name: str) -> int | None:
    """Best-effort 1-based line of the CREATE/ALTER statement declaring `name`."""
    word = re.compile(r"\b" + re.escape(name) + r"\b", re.IGNORECASE)
    for index, line in enumerate(source.splitlines(), start=1):
        low = line.lower()
        if ("create" in low or "alter" in low) and word.search(line):
            return index
    return None
