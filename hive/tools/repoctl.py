"""repoctl — the admin CLI for the durable synced-repo registry (the authctl twin).

`python -m hive.tools.repoctl add <url> [--name --branch --token-env]` registers a repo
for the server-side census sync: the row lands in the store's `repos` table and the sync
daemon picks it up on its next tick — no restart. `remove <name>` deregisters it (the
daemon prunes the mirror next tick; episode scope rows are kept, so a re-registered repo
picks its memories straight back up). `list` prints the registry, one repo per line.
`report` prints the SAME registry plus what the daemon has observed about it as ONE JSON
document — the machine-readable projection the operator console reads.

Exit codes split a refusal the OPERATOR can fix from a fault they cannot (the censusctl
convention): `EX_DATAERR` (65) is "the request is refused, nothing written" — a bad slug,
a duplicate name, an empty url, an unknown name on remove — and carries a reason a caller
may safely surface, because no credential value ever appears in this tool's output.
`EX_SOFTWARE` (70) stays the INTERNAL fault (e.g. the pre-v3 store's no-migration
refusal), whose text a caller must not forward blindly.

Registry rows are OPERATIONAL data (the authctl-seat pattern), never boot config, and
store NO secret byte: `--token-env` is the NAME of the env var holding that repo's git
token (unset ⇒ the fleet-default `HIVE_SYNC__TOKEN` at tick time), resolved by the sync
daemon per tick and probed present at boot by the entrypoint. Names are slugs
([a-z0-9._-]+ — they ride mirror paths and scope labels); the store's `repo_add` owns
the validation (single-sourced) and this ctl surfaces its refusal as a non-zero exit.

A sibling of `authctl` / `censusctl` (operational tools live in `hive/tools/`). The
db_path resolves from `--db` or `$HIVE_STORE__DB_PATH`, else defaults to
`/data/shared.db` (mirroring entrypoint/healthcheck/backupctl — the BUG-013 zero-config
posture: the `hive` CLI execs this with no `--db`). Injection seams (`connect_fn` /
`out` / `now`) keep the wiring unit-testable without a real DB file. No credential value
is ever read, stored, or printed — only env var NAMES appear anywhere.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import time
from typing import Any, Callable, Mapping, Optional, TextIO

from hive.adapters.sqlite_db import connect
from hive.adapters.store_sqlite import SqliteEpisodeStore
from hive.app.census_health import census_health_report

# sysexits.h — mirror authctl/entrypoint/censusctl (an exit code cannot lie like a log
# can). The 65/70 split is the WHOLE distinction between "you can fix this" and "the
# server is broken": a caller may forward a 65's reason to a human and must not forward
# a 70's. Widening 65 to cover an internal fault would leak an internal message.
EX_OK = 0
EX_DATAERR = 65
EX_SOFTWARE = 70

_DEFAULT_DB_PATH = (
    "/data/shared.db"  # mirrors entrypoint/healthcheck/backupctl (BUG-013)
)


def _derive_name(url: str) -> str:
    """Default registry name from the URL basename (`…/alpha.git` → `alpha`). No
    silent normalization: a derivation that is not a valid slug is REFUSED by the
    store's validator with a message pointing at --name. // O(1)."""
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    return tail[: -len(".git")] if tail.endswith(".git") else tail


def _report(store: Any) -> dict[str, Any]:
    """The registry AND what the sync daemon observed about it, as ONE document:
    ``{"repos": [row + observed state, …], "fleet": {…}}``.

    Neither half is restated here. The registry columns come from the store's own
    ``RepoRow`` (via ``dataclasses.asdict``), and the per-repo live state plus the
    always-present fleet block come from ``census_health_report`` — whose served field
    set is itself owned by ``hive.app.sync_keys``. So a new sync field reaches every
    consumer with NO edit to this file, and a field with no writer cannot be advertised
    at all (BUG-059). A repo the daemon has left no meta for carries no ``sync`` key —
    honest absence, never a confident zero. // O(n) rows."""
    health = census_health_report(store)
    observed = health.get("repos", {})
    return {
        "repos": [
            {**dataclasses.asdict(row), **observed.get(row.name, {})}
            for row in store.repo_registry()
        ],
        "fleet": health.get("fleet", {}),
    }


def main(
    argv: Optional[list[str]] = None,
    *,
    env: Optional[Mapping[str, str]] = None,
    connect_fn: Optional[Callable[[str], Any]] = None,
    out: Optional[TextIO] = None,
    now: Optional[Callable[[], int]] = None,
) -> int:
    """Register / deregister / list synced repos; return a sysexits exit code.
    `connect_fn` (default the prod `connect`), `out` (default stdout) and `now`
    (default wall clock — the row's added_ts) are injection seams. NEVER echoes an
    env *value* (secret-safe: only var NAMES ride any output). // O(1) DB ops."""
    import os  # local: env default without a module global

    env = os.environ if env is None else env
    connect_fn = connect_fn or connect
    out = out if out is not None else sys.stdout
    now = now or (lambda: int(time.time()))

    parser = argparse.ArgumentParser(
        prog="hive.tools.repoctl",
        description="Manage the durable synced-repo registry the sync daemon feeds from.",
    )
    parser.add_argument(
        "--db", default=None, help="SQLite store path (default: $HIVE_STORE__DB_PATH)"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_add = sub.add_parser(
        "add", help="register a repo (picked up next tick; the row stores NO secret)"
    )
    p_add.add_argument("url", help="the git remote URL (https for token auth)")
    p_add.add_argument(
        "--name",
        default=None,
        help="registry slug [a-z0-9._-]+ (default: the URL basename)",
    )
    p_add.add_argument(
        "--branch",
        default="",
        help="canonical branch (default: the origin default branch)",
    )
    p_add.add_argument(
        "--token-env",
        dest="token_env",
        default="",
        help="NAME of the env var holding this repo's git token — the "
        "name is stored, never the token (default: HIVE_SYNC__TOKEN)",
    )
    p_remove = sub.add_parser(
        "remove",
        help="deregister a repo (stops feeding; the mirror is pruned next tick)",
    )
    p_remove.add_argument("name", help="the registry slug to remove")
    sub.add_parser(
        "list",
        help="print registered repos, one per line "
        "(name url branch token-env; '-' = default)",
    )
    sub.add_parser(
        "report",
        help="print the registry plus the sync daemon's observed state for each, "
        "as ONE JSON document (the operator console reads this)",
    )
    args = parser.parse_args(argv)

    # default to the in-container store path when unset (mirrors entrypoint/healthcheck/
    # backupctl — BUG-013): the CLI execs repoctl with no --db, so this default is what
    # makes the zero-config repo verbs work; $HIVE_STORE__DB_PATH overrides, --db wins.
    db_path = (
        args.db or env.get("HIVE_STORE__DB_PATH") or ""
    ).strip() or _DEFAULT_DB_PATH

    try:
        store = SqliteEpisodeStore(connect_fn(db_path))
    except RuntimeError as exc:  # e.g. the v2-store no-migration refusal
        print(f"repoctl: {exc}", file=sys.stderr)
        return EX_SOFTWARE

    if args.cmd == "add":
        name = (args.name or _derive_name(args.url)).strip()
        try:
            store.repo_add(
                name=name,
                url=args.url,
                canonical_ref=args.branch or "",
                token_env=args.token_env or "",
                added_ts=int(now()),
            )
        except ValueError as exc:  # bad slug / duplicate / empty url — nothing written
            # An OPERATOR-FIXABLE refusal: the reason names what to change and carries
            # no credential value, so a caller may surface it to a human verbatim. ←
            # returning EX_SOFTWARE here is a deliberate mutation: the refusal would be
            # indistinguishable from an internal fault and the operator would be told
            # only "upstream failed" for a name they could simply retype.
            print(f"repoctl: {exc}", file=sys.stderr)
            return EX_DATAERR
        print(
            f"repoctl: registered {name!r} (branch: "
            f"{args.branch or 'origin default'}; token env var: "
            f"{args.token_env or 'HIVE_SYNC__TOKEN (fleet default)'}) — the sync "
            "daemon picks it up on its next tick",
            file=sys.stderr,
        )
        return EX_OK

    if args.cmd == "list":
        for row in store.repo_registry():  # name-ordered by the adapter
            print(
                f"{row.name}\t{row.url}\t{row.canonical_ref or '-'}\t"
                f"{row.token_env or '-'}",
                file=out,
            )
        return EX_OK

    if args.cmd == "report":
        try:
            document = _report(store)
        except Exception as exc:  # an INTERNAL fault — never an operator's to fix
            print(f"repoctl: cannot build the report: {exc}", file=sys.stderr)
            return EX_SOFTWARE
        print(json.dumps(document), file=out)  # ONE document, one line
        return EX_OK

    # args.cmd == "remove" (subparsers are required, so no other value reaches here)
    removed = store.repo_remove(args.name)
    if not removed:
        # operator-fixable: they named a repo that is not registered (see the add path).
        print(
            f"repoctl: no repo named {args.name!r} (nothing removed)", file=sys.stderr
        )
        return EX_DATAERR
    print(
        f"repoctl: removed {args.name!r} — the mirror is pruned next tick; episode "
        "scope rows are kept (a re-registered repo picks them back up)",
        file=sys.stderr,
    )
    return EX_OK


if (
    __name__ == "__main__"
):  # pragma: no cover — module entry (`python -m hive.tools.repoctl`)
    raise SystemExit(main(sys.argv[1:]))
