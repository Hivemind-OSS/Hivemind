"""authctl — the admin CLI for per-device bearer tokens (AUTH-PLAN §4/§10).

`python -m hive.tools.authctl create <label>` mints a token for a teammate's device, stores
ONLY its sha256, and prints the 256-bit plaintext to STDOUT exactly once (the operator hands
it over via a secret manager — it is never recoverable again). `revoke <label>` deletes the
token so that device's next request returns 401.

A sibling of `healthcheck` / `bake_model` (operational tools live in `hive/tools/`). The
db_path resolves from `--db` or `$HIVE_STORE__DB_PATH` and FAILS FAST (EX_CONFIG) if absent —
mirroring the entrypoint's "a required value missing is a non-zero exit, never a silent
default". Injection seams (`connect_fn` / `out`) keep the wiring + the fail-fast unit-testable
without a real DB file. STDOUT carries only the token (machine-parseable); human metadata
goes to STDERR so a pipe captures just the credential.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from typing import Any, Callable, Mapping, Optional, TextIO

from hive.adapters.auth_store_sqlite import SqliteTokenStore
from hive.adapters.sqlite_db import connect

# sysexits.h — mirror the entrypoint's boot contract (an exit code cannot lie like a log can).
EX_OK = 0
EX_SOFTWARE = 70
EX_CONFIG = 78


def main(argv: Optional[list[str]] = None, *, env: Optional[Mapping[str, str]] = None,
         connect_fn: Optional[Callable[[str], Any]] = None,
         out: Optional[TextIO] = None) -> int:
    """Mint or revoke a device token; return a sysexits exit code. `connect_fn` (default the
    prod `connect`) and `out` (default stdout) are injection seams. NEVER echoes the env
    value of a missing var (secret-safe). // O(1) DB ops."""
    import os                                       # local: env default without a module global

    env = os.environ if env is None else env
    connect_fn = connect_fn or connect
    out = out if out is not None else sys.stdout

    parser = argparse.ArgumentParser(
        prog="hive.tools.authctl",
        description="Mint / revoke per-device bearer tokens for the Hive HTTP daemon.")
    parser.add_argument("--db", default=None,
                        help="SQLite store path (default: $HIVE_STORE__DB_PATH)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_create = sub.add_parser("create", help="mint a token for a device label (printed ONCE)")
    p_create.add_argument("label", help="device identity → proposed_by, e.g. alice-laptop")
    p_revoke = sub.add_parser("revoke", help="delete a device's token (next request → 401)")
    p_revoke.add_argument("label", help="the device label to revoke")
    args = parser.parse_args(argv)

    db_path = (args.db or env.get("HIVE_STORE__DB_PATH") or "").strip()
    if not db_path:                                 # fail fast — no silent default (mirrors entrypoint)
        print("authctl: --db or $HIVE_STORE__DB_PATH is required", file=sys.stderr)
        return EX_CONFIG

    store = SqliteTokenStore(connect_fn(db_path))

    if args.cmd == "create":
        try:
            token = store.create(args.label)
        except sqlite3.IntegrityError:              # duplicate label (PK) — already provisioned
            print(f"authctl: a token already exists for {args.label!r} "
                  "(revoke it first to re-issue)", file=sys.stderr)
            return EX_SOFTWARE
        print(token, file=out)                      # the ONLY stdout line — the plaintext, shown once
        print(f"authctl: created token for {args.label!r} (shown once above; "
              "hand it over via a secret manager)", file=sys.stderr)
        return EX_OK

    # args.cmd == "revoke" (subparsers are required, so no other value reaches here)
    removed = store.revoke(args.label)              # ← claiming success without deleting is RULE-2 mut
    if not removed:
        print(f"authctl: no token for {args.label!r} (nothing revoked)", file=sys.stderr)
        return EX_SOFTWARE
    print(f"authctl: revoked {args.label!r}", file=sys.stderr)
    return EX_OK


if __name__ == "__main__":  # pragma: no cover — module entry (`python -m hive.tools.authctl`)
    raise SystemExit(main(sys.argv[1:]))
