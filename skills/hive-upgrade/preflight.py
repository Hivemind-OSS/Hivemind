"""Schema pre-flight for `hive upgrade`: will the TARGET ref accept the store running NOW?

`hive upgrade` is already snapshot-gated and auto-rolls-back, so a schema-incompatible ref
cannot destroy data. What it cannot tell you is that the upgrade was *never going to work* —
you find out after a full rebuild + health-wait + rollback cycle. This answers that first,
in seconds, without touching anything.

The compatibility contract is exactly what the target ref's own code asserts at boot:
  - `_LEGACY_EPISODE_COLUMNS` (store_sqlite) — none may be present on `episodes`
  - `_V3_EPISODE_COLUMNS`     (store_sqlite) — all must be present on `episodes`
  - `_REQUIRED_TABLES`        (container)    — all must exist

Both halves are read-only: the target ref is read via `git show` and parsed with `ast`
(never imported, never executed), and the live store is opened `mode=ro`.

FAILS CLOSED. Any verdict other than PASS means "do not upgrade yet" — including UNKNOWN,
which is what you get when the target ref's schema contract cannot be located (it predates
these constants, or they were renamed). UNKNOWN is never treated as compatible.

Exit: 0 PASS · 1 SCHEMA BREAK · 2 UNKNOWN / could not determine.

Usage:  python3 skills/hive-upgrade/preflight.py [ref]        # ref defaults to `release`
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys

_UNWRAP = {"frozenset", "set", "tuple", "list"}


def _const(ref: str, path: str, name: str) -> set[str]:
    """A module-level literal from a git ref, via ast — the file is never executed."""
    shown = subprocess.run(
        ["git", "show", f"{ref}:{path}"], capture_output=True, text=True
    )
    if shown.returncode != 0:
        raise LookupError(f"cannot read {path} at {ref!r}: {shown.stderr.strip()}")
    for node in ast.parse(shown.stdout).body:
        targets = list(getattr(node, "targets", []))
        if isinstance(node, ast.AnnAssign):
            targets = [node.target]
        for target in targets:
            if isinstance(target, ast.Name) and target.id == name:
                value = node.value
                # `frozenset({...})` is a Call, which literal_eval refuses — unwrap it
                if (
                    isinstance(value, ast.Call)
                    and getattr(value.func, "id", "") in _UNWRAP
                ):
                    value = value.args[0]
                return set(ast.literal_eval(value))
    raise LookupError(f"{name} not found in {ref}:{path}")


def _live_shape() -> tuple[set[str], set[str]]:
    """The running store's episodes columns + table set, read-only, via the container."""
    probe = subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "hive-server",
            "python",
            "-c",
            "import sqlite3,json\n"
            "c=sqlite3.connect('file:/data/shared.db?mode=ro',uri=True)\n"
            "print(json.dumps({"
            "'cols':[r[1] for r in c.execute('PRAGMA table_info(episodes)')],"
            "'tabs':[r[0] for r in c.execute("
            "\"SELECT name FROM sqlite_master WHERE type='table'\")]}))",
        ],
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0:
        raise LookupError(
            f"cannot read the live store — is the server up? {probe.stderr.strip()}"
        )
    payload = json.loads(probe.stdout.strip().splitlines()[-1])
    return set(payload["cols"]), set(payload["tabs"])


def main(argv: list[str]) -> int:
    ref = argv[1] if len(argv) > 1 else "release"
    try:
        legacy = _const(ref, "hive/adapters/store_sqlite.py", "_LEGACY_EPISODE_COLUMNS")
        needed = _const(ref, "hive/adapters/store_sqlite.py", "_V3_EPISODE_COLUMNS")
        tables = _const(ref, "hive/app/container.py", "_REQUIRED_TABLES")
        cols, tabs = _live_shape()
    except LookupError as exc:
        print(f"UNKNOWN — {exc}")
        print(
            "Compatibility could not be determined, so it is NOT proven safe. Treat this "
            "as a schema break: snapshot first and expect to start clean."
        )
        return 2

    if not cols:
        print(f"PASS — the store is empty; {ref} will create its own schema.")
        return 0

    breaks = []
    if legacy & cols:
        breaks.append(f"retired column(s) still present: {sorted(legacy & cols)}")
    if needed - cols:
        breaks.append(f"required column(s) absent: {sorted(needed - cols)}")
    if tables - tabs:
        breaks.append(f"required table(s) absent: {sorted(tables - tabs)}")

    if breaks:
        print(f"SCHEMA BREAK — {ref} will REFUSE this store at boot (exit 70):")
        for item in breaks:
            print(f"  - {item}")
        print(
            "There is no in-place migration in this build. `hive upgrade` will snapshot, "
            "try, fail the health gate, and roll back — your data survives, but the ref "
            "cannot be adopted without starting from a clean store."
        )
        return 1

    print(f"PASS — {ref} accepts the live store's schema. Safe to upgrade.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
