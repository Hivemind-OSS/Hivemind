"""The polyglot pin: a renamed verb, a widened drift tier or a new status
literal is a FAILING TEST in the server's own suite, not silent client rot.

The agent-loop harness cannot import a Python constant, so its copy of the
server's names is machine-written by ``scripts/gen_harness_constants.py``.
These tests re-render it and fail on any byte of drift, then check the emitted
sets against their sources in both directions so the generator itself cannot
quietly stop reading the server.
"""

from __future__ import annotations

import re
from pathlib import Path

from hive.app.tool_defs import TOOL_NAMES
from hive.domain.retirement import QUALIFYING_DRIFT
from scripts.gen_harness_constants import (
    AFFIRMATIVE_STATUS,
    NON_AFFIRMATIVE_STATUS,
    OTHER_STATUS,
    OUTPUT,
    _status_literals,
    render,
)

_TS_LIST = re.compile(r"export const (\w+) = \[(.*?)\n\] as const", re.DOTALL)
_TS_SCALAR = re.compile(r'export const (\w+) = "([^"]*)"')


def _emitted() -> tuple[dict[str, list[str]], dict[str, str]]:
    body = OUTPUT.read_text(encoding="utf-8")
    lists = {
        name: re.findall(r'"([^"]+)"', items) for name, items in _TS_LIST.findall(body)
    }
    scalars = dict(_TS_SCALAR.findall(body))
    return lists, scalars


def test_the_generated_module_exists_and_is_marked_do_not_edit() -> None:
    assert OUTPUT.exists(), (
        f"{OUTPUT} is missing — run python scripts/gen_harness_constants.py"
    )
    head = OUTPUT.read_text(encoding="utf-8").splitlines()[0]
    assert "DO NOT EDIT" in head, (
        "the generated module must announce itself as machine-written; a "
        f"hand-editable copy is a second contract. got: {head!r}"
    )


def test_regenerating_produces_no_diff() -> None:
    """The drift gate itself. A hand-edit, a rename, or a widened tier reds here."""
    assert OUTPUT.read_text(encoding="utf-8") == render(), (
        f"{OUTPUT} is stale or hand-edited — run "
        "python scripts/gen_harness_constants.py"
    )


def test_the_emitted_verbs_equal_the_advertised_tool_names_both_ways() -> None:
    lists, scalars = _emitted()
    assert set(lists["VERBS"]) == set(TOOL_NAMES)
    named = {v for k, v in scalars.items() if k.startswith("VERB_")}
    assert named <= set(TOOL_NAMES), (
        f"the harness names verbs the server does not advertise: {named - set(TOOL_NAMES)}"
    )


def test_the_emitted_drift_tier_equals_the_retirement_tier_both_ways() -> None:
    lists, _ = _emitted()
    assert set(lists["QUALIFYING_DRIFT"]) == set(QUALIFYING_DRIFT), (
        "the harness's actionable tier must be the server's retirement tier — "
        "a widened one demands a retirement the gate structurally declines"
    )


def test_the_status_partition_is_disjoint_and_covers_the_whole_boundary() -> None:
    lists, _ = _emitted()
    sides = {
        "AFFIRMATIVE_STATUS": (
            set(lists["AFFIRMATIVE_STATUS"]),
            set(AFFIRMATIVE_STATUS),
        ),
        "NON_AFFIRMATIVE_STATUS": (
            set(lists["NON_AFFIRMATIVE_STATUS"]),
            set(NON_AFFIRMATIVE_STATUS),
        ),
    }
    for name, (emitted, declared) in sides.items():
        assert emitted == declared, name
    assert "OTHER_STATUS" not in lists, (
        "the non-maintenance statuses are classified here but never handed to the "
        "harness — it decides a store landed by the ABSENCE of a refusal, so those "
        "names would be lifecycle vocabulary it has no rule for"
    )
    partition = [emitted for emitted, _ in sides.values()] + [set(OTHER_STATUS)]
    total = set().union(*partition)
    assert sum(len(s) for s in partition) == len(total), "the status partition overlaps"
    in_boundary = _status_literals()
    assert in_boundary <= total, (
        f"the boundary emits a status no side classifies: {sorted(in_boundary - total)}"
    )
    assert "noop" in sides["NON_AFFIRMATIVE_STATUS"][0], (
        "crediting a benign no-op would teach agents to fire ritual retirements"
    )


def test_the_advisory_verb_status_is_the_one_the_service_really_returns() -> None:
    """The advisory verb echoes ``FlagResult.status`` — ``flagged``. Crediting the
    outcome verb's ``recorded`` instead would leave the advisory close dead."""
    lists, _ = _emitted()
    assert "flagged" in set(lists["AFFIRMATIVE_STATUS"])
    assert "recorded" in set(OTHER_STATUS)
    body = (
        Path(__file__).resolve().parent.parent.parent
        / "hive"
        / "domain"
        / "conflict.py"
    ).read_text(encoding="utf-8")
    assert '"flagged"' in body, (
        "the advisory service stopped spelling its own affirmative status"
    )


def test_the_refused_status_is_named_and_non_affirmative() -> None:
    lists, scalars = _emitted()
    assert scalars["STATUS_REFUSED"] == "refused"
    assert scalars["STATUS_REFUSED"] in set(lists["NON_AFFIRMATIVE_STATUS"])


def test_the_generated_module_lives_under_the_pure_core() -> None:
    assert OUTPUT.parent.name == "core"
    assert Path(OUTPUT).suffix == ".ts"


def test_the_emitted_argument_keys_exist_on_the_advertised_schemas() -> None:
    """The harness reads ids and carriers out of a call's arguments, so those key
    names are coupling too. Reading them off the schema is what makes a rename a
    generation failure instead of a silently-dead check."""
    from scripts.gen_harness_constants import (
        CARRIER_ARGS,
        QUERY_ARG,
        REPLACES_ARG,
        _schema,
        id_args,
    )

    lists, scalars = _emitted()
    assert set(lists["CARRIER_ARGS"]) == set(CARRIER_ARGS)
    assert scalars["QUERY_ARG"] == QUERY_ARG
    assert scalars["REPLACES_ARG"] == REPLACES_ARG

    for verb in ("hive_write", "hive_capture"):
        props = dict(_schema(verb).get("properties") or {})
        for arg in CARRIER_ARGS:
            assert arg in props, f"{verb} no longer advertises {arg}"
    assert QUERY_ARG in dict(_schema("hive_recall").get("properties") or {})
    assert REPLACES_ARG in dict(_schema("hive_write").get("properties") or {})

    body = OUTPUT.read_text(encoding="utf-8")
    for verb, args in id_args().items():
        assert args, f"{verb} advertises no integer argument to name a memory with"
        rendered = f'"{verb}": [{", ".join(chr(34) + a + chr(34) for a in args)}]'
        assert rendered in body, f"{verb} id arguments drifted from the schema"
