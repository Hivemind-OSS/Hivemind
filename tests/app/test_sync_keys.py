"""The writer↔reader seam for the sync meta keys — per-repo (BUG-059) and fleet
(BUG-062).

`tests/sync/` proves the daemon writes what it writes; `tests/app/test_census_health.py`
proves the reader serves what it reads — from meta rows the test itself seeds. Neither
can see the gap between them, and that gap is exactly where BUG-059 lived: the reader
advertised four fields whose writers had been deleted or never re-keyed, while both
suites stayed green.

These tests close it STATICALLY — no git, no fixture repo, no daemon run. They read
`hive/app/sync.py`'s AST and assert every advertised field's key builder is actually
CALLED there. Delete a writer and keep its field, or add a field with no writer, and
this module goes red before the block can lie to an operator again.

The fleet half carries the same guarantee one notch tighter: both fleet keys are
written by a literal `meta_set(<builder>(), …)`, so the check can demand the builder
reach a real STORE WRITE rather than merely appear in some call. That tightening is
available only because the fleet writers are inline; the per-repo writers reach
`meta_set` through a local (`tip_key`) and through `_bump_counter_locked`, so their half
stays on the called-anywhere check.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from hive.app import census_health, sync, sync_keys
from hive.app.sync_keys import (
    COUNTER_FIELDS,
    FLEET_KEY_BUILDERS,
    FLEET_STR_FIELDS,
    KEY_BUILDERS,
    STR_FIELDS,
)


def _module_ast(module) -> ast.Module:
    return ast.parse(Path(inspect.getsourcefile(module)).read_text())


def _called_names(module) -> set[str]:
    """Every plain-function name CALLED anywhere in ``module``'s source. AST, not a
    text search: a builder named only in a comment or a docstring is not a writer."""
    return {
        node.func.id
        for node in ast.walk(_module_ast(module))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }


def _meta_set_key_builders(module) -> set[str]:
    """Every plain-function name called as the KEY argument of a ``…meta_set(k, v)`` in
    ``module`` — i.e. the builders that reach an actual store write, not merely a call
    site somewhere."""
    return {
        node.args[0].func.id
        for node in ast.walk(_module_ast(module))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "meta_set"
        and node.args
        and isinstance(node.args[0], ast.Call)
        and isinstance(node.args[0].func, ast.Name)
    }


# ── the grammar is the single source of the served field set ──────────────────
def test_served_fields_are_exactly_the_fields_with_key_builders():
    # the reader may not advertise a field the grammar cannot key, and the grammar
    # may not carry a builder no one serves.
    assert set(STR_FIELDS) | set(COUNTER_FIELDS) == set(KEY_BUILDERS)
    assert not set(STR_FIELDS) & set(COUNTER_FIELDS)


def test_keys_are_three_part_and_repo_scoped():
    # the reader groups on `sync:<repo>:<field>` and skips 2-part globals — every
    # builder must produce the 3-part shape or its field is silently dropped.
    for field, build in KEY_BUILDERS.items():
        assert build("alpha") == f"sync:alpha:{field}"


@pytest.mark.parametrize("field", sorted(KEY_BUILDERS))
def test_every_served_field_has_a_writer_in_the_daemon(field):
    # THE regression gate for BUG-059: a served field whose builder the daemon never
    # calls is a structurally empty field on a healthy feed.
    assert KEY_BUILDERS[field].__name__ in _called_names(sync), (
        f"census_health serves '{field}' but hive/app/sync.py never calls "
        f"{KEY_BUILDERS[field].__name__}() — the field would read null/absent forever"
    )


def test_reader_takes_its_field_set_from_the_grammar_not_its_own_literals():
    # the literal-drift the bug was made of: census_health must not redeclare the
    # field names it serves.
    assert census_health.STR_FIELDS is sync_keys.STR_FIELDS
    assert census_health.COUNTER_FIELDS is sync_keys.COUNTER_FIELDS


def test_candidates_evaluated_is_gone_with_its_machinery():
    # the PR-candidate leg was deleted; the counter it fed must not linger as a
    # permanently-zero field.
    assert "candidates_evaluated" not in KEY_BUILDERS


# ── the fleet keys are a different namespace with the same guarantee ──────────
def test_fleet_served_fields_are_exactly_the_fields_with_key_builders():
    assert set(FLEET_STR_FIELDS) == set(FLEET_KEY_BUILDERS)


def test_fleet_keys_are_two_part_and_never_collide_with_a_repo_block():
    # `sync:last_sync_ts` / `sync:last_error` are stamped with no repo in scope. The
    # 2-part shape is what keeps the reader's 3-part per-repo grouping from
    # misattributing them to some repo — and is why the fleet facts need their own slot
    # in the report rather than a reserved key inside the repo-keyed map (BUG-062:
    # `_fleet`, `daemon` and `sync` are all names the registry slug gate admits).
    for field, build in FLEET_KEY_BUILDERS.items():
        assert build() == f"sync:{field}"
        assert build().count(":") == 1


@pytest.mark.parametrize("field", sorted(FLEET_KEY_BUILDERS))
def test_every_fleet_field_has_a_writer_in_the_daemon(field):
    # THE regression gate for BUG-062's second half: the fleet block may only advertise
    # what the daemon actually stamps. A field whose builder never reaches a meta_set is
    # a permanently-null fleet field — which reads as "the daemon has never faulted".
    assert FLEET_KEY_BUILDERS[field].__name__ in _meta_set_key_builders(sync), (
        f"census_health serves fleet '{field}' but hive/app/sync.py never writes "
        f"{FLEET_KEY_BUILDERS[field].__name__}() through meta_set — the field would "
        f"read null forever, and a null there reads as a healthy daemon"
    )


def test_reader_takes_the_fleet_field_set_from_the_grammar_too():
    assert census_health.FLEET_STR_FIELDS is sync_keys.FLEET_STR_FIELDS
    assert census_health.FLEET_KEY_BUILDERS is sync_keys.FLEET_KEY_BUILDERS


def test_daemon_declares_no_sync_key_literals_of_its_own():
    # the daemon once owned two `sync:*` string constants beside the grammar; a second
    # definition of a key is exactly how the two sides drift apart again.
    source = Path(inspect.getsourcefile(sync)).read_text()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert not node.value.startswith("sync:"), (
                f"hive/app/sync.py builds a sync meta key from its own literal "
                f"{node.value!r} — every key comes from hive/app/sync_keys.py"
            )
