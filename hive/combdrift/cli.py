"""CLI orchestrator: read records JSON, verify against a repo, write verdicts JSON.

Exit codes:
  0  no record is stale
  1  at least one record is stale
  2  usage / input error (missing required env, missing repo, unreadable or
     malformed records) — a problem with how the tool was invoked, distinct
     from a target-repo condition, which always becomes a verdict, never an exit.

A target-repo condition (missing file, syntax error, path outside repo) never
raises to the shell; it is reported as part of a verdict.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from hive.combdrift.env import require_env
from hive.combdrift.serialization import records_from_json, run_to_json
from hive.combdrift.verdict import verify_records

_EXIT_OK = 0
_EXIT_STALE = 1
_EXIT_USAGE = 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="comb-drift",
        description="Verify the currency of code-anchored memory records against a "
        "local Python, JavaScript, TypeScript, Go, Rust, C, or C++ repo, or declared "
        "database schemas (.sql / *.schema.json) — parse-only, no execution, no "
        "database connection.",
    )
    parser.add_argument(
        "--records",
        required=True,
        help="Path to a JSON file of records, or '-' to read from stdin.",
    )
    parser.add_argument(
        "--repo",
        required=True,
        help="Path to the local repository to verify against (Python, JavaScript, "
        "TypeScript, Go, Rust, C, C++, or declared-schema .sql / *.schema.json "
        "sources).",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Path to write the verdicts JSON (default: stdout).",
    )
    parser.add_argument(
        "--require-env",
        action="append",
        default=[],
        metavar="NAME",
        help="Required environment variable; checked fail-fast before any work. "
        "Repeatable.",
    )
    return parser


def _read_records_text(records_arg: str) -> str:
    if records_arg == "-":
        return sys.stdin.read()
    with open(records_arg, "r", encoding="utf-8") as handle:
        return handle.read()


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    # Fail-fast on declared required secrets BEFORE any I/O or verification.
    try:
        for name in args.require_env:
            require_env(name)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return _EXIT_USAGE

    if not os.path.isdir(args.repo):
        print(f"repo path is not a directory: {args.repo}", file=sys.stderr)
        return _EXIT_USAGE

    try:
        records_text = _read_records_text(args.records)
    except OSError as exc:
        print(f"cannot read records: {exc}", file=sys.stderr)
        return _EXIT_USAGE

    try:
        records = records_from_json(records_text)
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"invalid records input: {exc}", file=sys.stderr)
        return _EXIT_USAGE

    verdicts, summary = verify_records(args.repo, records)
    output = run_to_json(verdicts, summary)

    if args.out is None:
        print(output)
    else:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(output)

    return _EXIT_STALE if summary.stale > 0 else _EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
