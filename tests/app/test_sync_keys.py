"""The writer↔reader seam for the per-repo sync meta keys (BUG-059).

`tests/sync/` proves the daemon writes what it writes; `tests/app/test_census_health.py`
proves the reader serves what it reads — from meta rows the test itself seeds. Neither
can see the gap between them, and that gap is exactly where BUG-059 lived: the reader
advertised four fields whose writers had been deleted or never re-keyed, while both
suites stayed green.

These tests close it STATICALLY — no git, no fixture repo, no daemon run. They read
`hive/app/sync.py`'s AST and assert every advertised field's key builder is actually
CALLED there. Delete a writer and keep its field, or add a field with no writer, and
this module goes red before the block can lie to an operator again.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from hive.app import census_health, sync, sync_keys
from hive.app.sync_keys import COUNTER_FIELDS, KEY_BUILDERS, STR_FIELDS


def _called_names(module) -> set[str]:
    """Every plain-function name CALLED anywhere in ``module``'s source. AST, not a
    text search: a builder named only in a comment or a docstring is not a writer."""
    tree = ast.parse(Path(inspect.getsourcefile(module)).read_text())
    return {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
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


# ── the global keys are a different namespace and stay out of per-repo blocks ──
def test_tick_shell_globals_are_two_part_and_unservable():
    # `sync:last_sync_ts` / `sync:last_error` are stamped with no repo in scope; the
    # reader's 3-part grouping must keep ignoring them rather than misattributing.
    for key in (sync.META_LAST_SYNC_TS, sync.META_LAST_ERROR):
        assert key.count(":") == 1
