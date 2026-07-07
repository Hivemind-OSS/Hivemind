"""censusctl — the in-container census-receipt ingest tool (the S4 kernel feed).

`python -m hive.tools.censusctl ingest <receipt.json|->` decodes a signed census DSSE
receipt, derives the SHA-bound change outcome server-side, joins the receipt's touched
``path::Symbol`` subjects against episode anchors, and appends one ``change_outcome``
evidence row per matched episode — plus, pre-merge under a full version stamp, the
mechanical ``outcome_verified_*`` / ``verify_*`` rider rows and the advisory
``stale_suspect`` rows for the receipt's optional propagation block — in ONE
transaction, idempotent, trust-untouched. The `hive ingest` operator verb execs this in-container
and pipes the receipt over stdin (no host path leaks into the container).

A sibling of `authctl` / `backupctl` (operational tools live in `hive/tools/`). The
db_path resolves from `--db` or `$HIVE_STORE__DB_PATH`, else defaults to
`/data/shared.db` (mirroring entrypoint/healthcheck/authctl/backupctl) — the `hive` CLI
execs this with no `--db`, so the default is what makes the zero-config verb work. The
store is built torch-free/embedder-free (no index, no model) — ingest never touches the
recall path. Injection seams (`connect_fn` / `stdin` / `out` / `now`) keep the wiring
unit-testable without a real DB file.

STDOUT carries exactly ONE machine-readable JSON report line
(inserted/already_recorded/matched/skipped_lines/keyid + the verified_helped/
verified_hurt/verify_current/verify_stale/stale_suspects rider counters); human
notes go to STDERR.
Exit codes are the contract (an exit code cannot lie like a log can): 0 ok — including
an honest matched=0; 65 EX_DATAERR — the receipt is refused (unreadable/not JSON/bad
DSSE shape/digest mismatch/no decided evidence), ZERO rows written; 70 EX_SOFTWARE —
an internal fault (DB locked beyond busy_timeout, store fault), fail fast. Signature
verification stays census-side: the DSSE keyid is surfaced in the report and
authenticity rides the operator channel (the authctl trust tier).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any, Callable, Mapping, Optional, TextIO

from hive.adapters.sqlite_db import connect
from hive.adapters.store_sqlite import SqliteEpisodeStore
from hive.domain.change_evidence import ChangeEvidenceService, ReceiptRefused

# sysexits.h — mirror authctl/entrypoint (an exit code cannot lie like a log can).
EX_OK = 0
EX_DATAERR = 65
EX_SOFTWARE = 70

_DEFAULT_DB_PATH = "/data/shared.db"   # mirrors entrypoint/healthcheck/authctl/backupctl


def main(argv: Optional[list[str]] = None, *, env: Optional[Mapping[str, str]] = None,
         connect_fn: Optional[Callable[[str], Any]] = None,
         stdin: Optional[TextIO] = None, out: Optional[TextIO] = None,
         now: Optional[Callable[[], int]] = None) -> int:
    """Ingest one receipt; return a sysexits exit code. `connect_fn` (default the prod
    `connect`), `stdin`, `out`, and `now` are injection seams. // O(receipt + batch)."""
    import os                                       # local: env default without a module global

    env = os.environ if env is None else env
    connect_fn = connect_fn or connect
    stdin = stdin if stdin is not None else sys.stdin
    out = out if out is not None else sys.stdout
    now = now or (lambda: int(time.time()))

    parser = argparse.ArgumentParser(
        prog="hive.tools.censusctl",
        description="Feed a signed census receipt's change outcome into the "
                    "append-only evidence ledger.")
    parser.add_argument("--db", default=None,
                        help="SQLite store path (default: $HIVE_STORE__DB_PATH)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_ingest = sub.add_parser(
        "ingest", help="decode + validate a DSSE receipt, join touched symbols "
                       "against episode anchors, append change_outcome evidence rows")
    p_ingest.add_argument("receipt", help="path to the receipt JSON, or '-' for stdin")
    p_ingest.add_argument("--post-merge", action="store_true",
                          help="record a post-merge outcome (CI asserts the rollout "
                               "facts; the server derives the tag from --signal)")
    p_ingest.add_argument("--verdict", choices=("pass", "fail"), default=None,
                          help="the post-merge outcome (required with --post-merge; "
                               "pre-merge verdicts are derived from the receipt, "
                               "never asserted)")
    p_ingest.add_argument("--signal", choices=("randomized", "canary", "none"),
                          default="none",
                          help="the post-merge evidence signal — machine-checked "
                               "requires randomized/canary (the §6.2.5 canary rule)")
    args = parser.parse_args(argv)

    if args.post_merge and args.verdict is None:
        parser.error("--post-merge requires --verdict {pass,fail}")

    # default to the in-container store path when unset (mirrors authctl) — `hive ingest`
    # execs this with no --db; $HIVE_STORE__DB_PATH still overrides, --db wins over both.
    db_path = (args.db or env.get("HIVE_STORE__DB_PATH") or "").strip() or _DEFAULT_DB_PATH

    # ── read + parse the receipt text (refuse closed: unreadable/not-JSON ⇒ 65) ──
    if args.receipt == "-":
        text = stdin.read()
    else:
        try:
            with open(args.receipt, encoding="utf-8") as fh:
                text = fh.read()
        except OSError as error:
            print(f"censusctl: cannot read receipt {args.receipt!r}: {error}",
                  file=sys.stderr)
            return EX_DATAERR
    try:
        envelope = json.loads(text)
    except ValueError as error:
        print(f"censusctl: receipt is not JSON: {error}", file=sys.stderr)
        return EX_DATAERR

    # ── wire the kernel-side service and ingest (ONE atomic batch) ───────────────
    try:
        store = SqliteEpisodeStore(connect_fn(db_path), index=None)  # torch/embedder-free
        service = ChangeEvidenceService(reader=store, appender=store, now=now)
        report = service.ingest(
            envelope, phase="post_merge" if args.post_merge else "pre_merge",
            verdict=args.verdict, signal=args.signal)
    except ReceiptRefused as error:
        print(f"censusctl: receipt refused: {error} — 0 rows written", file=sys.stderr)
        return EX_DATAERR
    except Exception as error:                      # noqa: BLE001 — tool edge: fail fast
        print(f"censusctl: internal fault: {error}", file=sys.stderr)
        return EX_SOFTWARE

    # the ONLY stdout line — the machine-readable report (canonical key order)
    print(json.dumps({"already_recorded": report.already_recorded,
                      "inserted": list(report.inserted),
                      "keyid": report.keyid,
                      "matched": report.matched,
                      "skipped_lines": report.skipped_lines,
                      "stale_suspects": report.stale_suspects,
                      "verified_helped": report.verified_helped,
                      "verified_hurt": report.verified_hurt,
                      "verify_current": report.verify_current,
                      "verify_stale": report.verify_stale},
                     sort_keys=True, separators=(",", ":")), file=out)
    print(f"censusctl: matched={report.matched} inserted={len(report.inserted)} "
          f"already_recorded={report.already_recorded} "
          f"skipped_lines={report.skipped_lines} "
          f"stale_suspects={report.stale_suspects} "
          f"verified_helped={report.verified_helped} "
          f"verified_hurt={report.verified_hurt} "
          f"verify_current={report.verify_current} "
          f"verify_stale={report.verify_stale} keyid={report.keyid}",
          file=sys.stderr)
    return EX_OK


if __name__ == "__main__":  # pragma: no cover — module entry (`python -m hive.tools.censusctl`)
    raise SystemExit(main(sys.argv[1:]))
