"""Emit ``harnesses/core/hive-constants.ts`` from the server source.

The agent-loop harness is TypeScript and the server is Python, so the only
coupling between them — the verb names, the drift tier that qualifies a
retirement, and the maintenance-status vocabulary — cannot be an import. It is
generation plus a drift gate instead: this module reads the names out of the
running server package and writes them as a machine-owned TypeScript module,
and ``tests/harness/test_constants_drift.py`` regenerates and fails on any
diff. A hand-copied constant is exactly what the pair exists to prevent.

The status partition is the one classification declared here rather than read:
the server has no single object naming which statuses mean the call did
something. Its COMPLETENESS is still machine-checked — every ``"status"``
literal in the MCP boundary is scanned out of the source and must fall in one
side of the partition, so a new status cannot appear unclassified.

Run: ``python scripts/gen_harness_constants.py`` (writes the file), or
``python scripts/gen_harness_constants.py --check`` (exit 1 on drift).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from hive.app.tool_defs import TOOL_DEFINITIONS, TOOL_NAMES
from hive.domain.retirement import QUALIFYING_DRIFT

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT = REPO_ROOT / "harnesses" / "core" / "hive-constants.ts"
BOUNDARY = REPO_ROOT / "hive" / "app" / "mcp_server.py"

#: A maintenance call the server actually honored. The harness credits on this
#: ALLOW-LIST, so an unrecognized future status fails safe by not crediting.
#: ``flagged`` — not ``recorded`` — is what the advisory verb returns; ``recorded``
#: belongs to the outcome verb, which is a different leg entirely.
AFFIRMATIVE_STATUS = ("flagged", "pruned", "superseded")

#: Nothing the caller asked for happened. Crediting any of these would teach
#: agents to fire ritual no-op calls at a client-side gate.
NON_AFFIRMATIVE_STATUS = ("disabled", "noop", "refused", "rejected")

#: A call that did its own non-maintenance job — the store verbs' landing states
#: and the outcome verb's acknowledgement. Classified here so the partition is
#: total, but deliberately NOT emitted: the harness decides a store landed by the
#: ABSENCE of a refusal, so handing it these names would hand it lifecycle
#: vocabulary it has no rule for — the second contract the whole pair refuses.
OTHER_STATUS = ("approved", "quarantined", "recorded", "redacted")

#: The verbs the harness reasons about by role. Each is asserted to be a real
#: advertised tool below, so a rename in ``tool_defs`` breaks generation.
NAMED_VERBS = {
    "RECALL": "hive_recall",
    "WRITE": "hive_write",
    "CAPTURE": "hive_capture",
    "OUTCOME": "hive_outcome",
    "SUPERSEDE": "hive_supersede",
    "PRUNE": "hive_prune",
    "FLAG": "hive_flag",
    "HEALTH": "hive_health",
}

#: The argument keys that carry a memory's code binding or repo scope. Declared
#: here, but each is asserted to exist on both store verbs' advertised schema, so
#: a rename in ``tool_defs`` breaks generation rather than silently un-checking it.
CARRIER_ARGS = ("anchors", "repos")

#: The argument key a recall's question rides on.
QUERY_ARG = "query"

#: The argument key a store rides its own retirement rider on.
REPLACES_ARG = "replaces"

_STATUS_LITERAL = re.compile(r'"status"\s*:\s*"([a-z_]+)"')


def _schema(verb: str) -> dict:
    for tool in TOOL_DEFINITIONS:
        if tool["name"] == verb:
            return dict(tool.get("inputSchema") or {})
    raise SystemExit(f"gen_harness_constants: {verb!r} is not an advertised tool")


def _id_args(verb: str) -> tuple[str, ...]:
    """Every integer-typed argument on a verb's advertised schema — read, not
    listed, so a renamed or newly-added id argument travels with the schema."""
    props = dict(_schema(verb).get("properties") or {})
    return tuple(
        sorted(k for k, v in props.items() if dict(v).get("type") == "integer")
    )


def id_args() -> dict[str, tuple[str, ...]]:
    """The id-bearing arguments of every verb that can name a memory to retire."""
    return {
        verb: _id_args(verb)
        for verb in (
            NAMED_VERBS["SUPERSEDE"],
            NAMED_VERBS["PRUNE"],
            NAMED_VERBS["FLAG"],
        )
    }


HEADER = """// GENERATED FROM THE SERVER SOURCE — DO NOT EDIT BY HAND.
//
// Regenerate with:  python scripts/gen_harness_constants.py
// A hand-edit, a renamed verb, a widened drift tier or a new status literal is
// caught by tests/harness/test_constants_drift.py in the server's own suite.
"""


def _status_literals() -> set[str]:
    """Every ``"status": "<literal>"`` the MCP boundary can emit."""
    return set(_STATUS_LITERAL.findall(BOUNDARY.read_text(encoding="utf-8")))


def _ts_list(name: str, values: tuple[str, ...] | list[str]) -> str:
    body = "".join(f'\n  "{v}",' for v in values)
    return f"export const {name} = [{body}\n] as const\n"


def render() -> str:
    """The generated module's exact bytes."""
    verbs = tuple(sorted(TOOL_NAMES))
    for label, verb in NAMED_VERBS.items():
        if verb not in TOOL_NAMES:
            raise SystemExit(
                f"gen_harness_constants: VERB_{label} names {verb!r}, which is not an "
                f"advertised tool — the server renamed it; update NAMED_VERBS."
            )
    for verb in (NAMED_VERBS["WRITE"], NAMED_VERBS["CAPTURE"]):
        props = dict(_schema(verb).get("properties") or {})
        missing = [a for a in CARRIER_ARGS if a not in props]
        if missing:
            raise SystemExit(
                f"gen_harness_constants: {verb} no longer advertises {missing} — the "
                f"carrier check would silently stop checking anything."
            )
    if QUERY_ARG not in (dict(_schema(NAMED_VERBS["RECALL"]).get("properties") or {})):
        raise SystemExit(
            f"gen_harness_constants: hive_recall no longer advertises {QUERY_ARG!r}"
        )
    if REPLACES_ARG not in (
        dict(_schema(NAMED_VERBS["WRITE"]).get("properties") or {})
    ):
        raise SystemExit(
            f"gen_harness_constants: hive_write no longer advertises {REPLACES_ARG!r}"
        )
    ids = id_args()
    for verb, args in ids.items():
        if not args:
            raise SystemExit(
                f"gen_harness_constants: {verb} advertises no integer argument, so a "
                f"maintenance call could never name the memory it acted on."
            )
    sides = (AFFIRMATIVE_STATUS, NON_AFFIRMATIVE_STATUS, OTHER_STATUS)
    partition = set().union(*(set(s) for s in sides))
    if sum(len(s) for s in sides) != len(partition):
        raise SystemExit("gen_harness_constants: the status partition overlaps")
    unclassified = _status_literals() - partition
    if unclassified:
        raise SystemExit(
            "gen_harness_constants: the MCP boundary emits status literals this "
            f"generator does not classify: {sorted(unclassified)}. Add each to the "
            "affirmative, non-affirmative or other side before regenerating."
        )

    parts = [
        HEADER,
        "\n/** Every advertised tool name. */\n",
        _ts_list("VERBS", verbs),
        "\n/** The verbs this harness reasons about by role. */\n",
        *(
            f'export const VERB_{label} = "{verb}"\n'
            for label, verb in sorted(NAMED_VERBS.items())
        ),
        "\n/** The drift verdicts that mean the anchor MOVED — the tier the server acts on. */\n",
        _ts_list("QUALIFYING_DRIFT", tuple(sorted(QUALIFYING_DRIFT))),
        "\n/** A maintenance call the server honored. An allow-list: unknown fails safe. */\n",
        _ts_list("AFFIRMATIVE_STATUS", AFFIRMATIVE_STATUS),
        "\n/** Nothing the caller asked for happened. */\n",
        _ts_list("NON_AFFIRMATIVE_STATUS", NON_AFFIRMATIVE_STATUS),
        '\n/** The one status that means nothing was stored. */\nexport const STATUS_REFUSED = "refused"\n',
        "\n/** The argument keys a memory's code binding or repo scope rides on. */\n",
        _ts_list("CARRIER_ARGS", CARRIER_ARGS),
        f'\n/** The argument key a recall\'s question rides on. */\nexport const QUERY_ARG = "{QUERY_ARG}"\n',
        "\n/** The id-bearing arguments of each verb that can name a memory to retire. */\n",
        "export const ID_ARGS: Readonly<Record<string, readonly string[]>> = {\n",
        *(
            f'  "{verb}": [{", ".join(chr(34) + a + chr(34) for a in args)}],\n'
            for verb, args in sorted(ids.items())
        ),
        "}\n",
        f'\n/** The argument key a store rides its own retirement rider on. */\nexport const REPLACES_ARG = "{REPLACES_ARG}"\n',
    ]
    return "".join(parts)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--check",
        action="store_true",
        help="exit 1 when the committed file differs from a fresh render",
    )
    args = ap.parse_args(argv)
    fresh = render()
    if args.check:
        current = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
        if current != fresh:
            print(f"{OUTPUT} is stale — run python scripts/gen_harness_constants.py")
            return 1
        return 0
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(fresh, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
