"""M07 / [B4] / [C5] — corpus import + geometry re-embed (off the hot path).

Two DISTINCT paths with a deliberate asymmetry in the secret floor:

  - ``import_corpus`` ([B4]) ingests the UNTRUSTED archived old store. Every row is
    run through the ``SecretScanner`` BEFORE it is staged — refuse → drop (+n_refused),
    redact → stage the masked body (hash re-derived over the redaction), clean → stage
    the raw text. Rows land ``status='pending'`` under ``proposed_by='import-admin'``
    (value NULL, structurally unrecallable) — §6.1#5b extended to the import boundary.
    The reference's scan-LESS ``put(fresh)`` is NOT ported: a credential in an old-store
    row must never reach an episodes row, a blob, the content_hash, or a log line.

  - ``reembed_from_text`` ([C5]) is the clean-store geometry rewrite: a W_version bump
    re-projects every APPROVED row's value from its blob text through the new head. It
    does NOT re-scan — the store was already scanned at admission, and the text /
    content_hash / status are left untouched; only ``value`` is rewritten. Re-scanning a
    trusted store would be a redundant floor pass that the [C5] decision rejects.

Idempotent resume: ``import_corpus`` dedups by content_hash through the store's
``stage`` UPSERT, so re-running over the same corpus double-inserts nothing
(``n_deduped`` counts the resume hits) — no separate watermark needed in v-min.
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from dataclasses import dataclass
from typing import Any, Callable, Iterable

import numpy as np

from hive.domain.errors import ReembedError
from hive.domain.secret_scan import REDACT, REFUSE

_log = logging.getLogger("hive.migration")


@dataclass(frozen=True, slots=True)
class ImportReport:
    """Audit record of one ``import_corpus`` pass. ``n_refused`` is the secret floor's
    drop count; ``n_deduped`` is the idempotent-resume hit count."""
    n_total: int
    n_imported: int
    n_redacted: int
    n_refused: int
    n_deduped: int


def _row_text(row: Any) -> str:
    if isinstance(row, dict):
        return str(row.get("text", ""))
    return str(getattr(row, "text", row))


def _row_field(row: Any, key: str, default):
    if isinstance(row, dict):
        val = row.get(key, default)
    else:
        val = getattr(row, key, default)
    return default if val is None else val


def import_corpus(rows: Iterable[Any], *, store: Any, scanner: Any,
                  import_admin: str = "import-admin",
                  now: Callable[[], int] = lambda: 0) -> ImportReport:
    """Scan-before-stage import of an untrusted corpus [B4]. Each row is a dict (or any
    object) carrying at least ``text`` (optional ``weight``/``source``/``tags``). Returns
    an ``ImportReport``. Never raises on a single refused row — the scan floor drops it
    and the import continues over the whole corpus. // O(R · scan) time, R = #rows."""
    n_total = n_imported = n_redacted = n_refused = n_deduped = 0
    for row in rows:
        n_total += 1
        text = _row_text(row)
        verdict = scanner.scan(text)                 # the secret floor — BEFORE any persistence
        if verdict.action == REFUSE:
            # rule NAMES + count only — never the matched bytes (the finding carries none)
            n_refused += 1
            _log.warning("import.refused rules=%s n_findings=%d proposed_by=%s",
                         [f.rule for f in verdict.findings], len(verdict.findings),
                         import_admin)
            continue
        staged_text = verdict.redacted_text if verdict.action == REDACT else text
        if verdict.action == REDACT:
            n_redacted += 1
            _log.info("import.redacted rules=%s n_spans=%d proposed_by=%s",
                      [f.rule for f in verdict.findings], len(verdict.findings),
                      import_admin)
        weight = float(_row_field(row, "weight", 1.0))
        source = str(_row_field(row, "source", ""))
        tags = str(_row_field(row, "tags", ""))
        eid, deduped = store.stage(
            text=staged_text, weight=weight, source=source, tags=tags,
            proposed_by=import_admin, ts=now())
        if deduped:
            n_deduped += 1                           # idempotent resume — no second row
        else:
            n_imported += 1
    _log.info("import.complete n_total=%d n_imported=%d n_redacted=%d n_refused=%d n_deduped=%d",
              n_total, n_imported, n_redacted, n_refused, n_deduped)
    return ImportReport(n_total=n_total, n_imported=n_imported, n_redacted=n_redacted,
                        n_refused=n_refused, n_deduped=n_deduped)


def reembed_from_text(store: Any, *, embedder: Any, scanner: Any = None) -> int:
    """Clean-store geometry rewrite [C5]: re-embed every APPROVED row from its blob text
    through ``embedder`` and rewrite ``value`` ONLY. Returns the count re-projected.
    Does NOT re-scan (``scanner`` is accepted for call-site symmetry but is NEVER
    invoked — a trusted store does not get a second floor pass); text / content_hash /
    status are untouched. // O(A · encode) time, A = #approved rows.

    Each re-projected vector is validated FINITE + 1-D BEFORE it is written, and the whole
    rewrite runs in ONE transaction: a non-finite embedder output (e.g. a zero vector
    normalized to NaN) raises ``ReembedError`` and rolls the batch back UN-mutated rather
    than persisting a NaN/Inf BLOB that would later crash ``rebuild_index_from_store``'s
    finiteness guard and strand the store with a cleared index over corrupt rows."""
    conn = store.conn
    rows = conn.execute(
        "SELECT id, text FROM episodes WHERE status='approved'").fetchall()
    n = 0
    with store.transaction():                        # atomic: a bad vector rolls back the rewrite
        for r in rows:
            value = np.asarray(embedder.encode(r["text"]), dtype=np.float32)
            if value.ndim != 1 or not bool(np.all(np.isfinite(value))):
                _log.error("reembed.nonfinite_vector episode_id=%s ndim=%d w_version=%s",
                           r["id"], value.ndim, getattr(embedder, "w_version", "?"))
                raise ReembedError(
                    f"embedder returned a non-finite or ill-shaped vector for episode "
                    f"{r['id']} (ndim={value.ndim}) — refusing to persist a corrupt value")
            vbytes = np.ascontiguousarray(value).tobytes()
            conn.execute("UPDATE episodes SET value=? WHERE id=?", (vbytes, r["id"]))
            n += 1
    store.rebuild_index_from_store()                 # warm-cache rebuild from approved-only [B3]
    _log.info("reembed.complete n_reprojected=%d w_version=%s", n,
              getattr(embedder, "w_version", "?"))
    return n


# ── CLI (import.sh wraps this; NOT executed in v-min — the corpus import is gated) ──

def _read_old_rows(old_db_path: str, table: str = "episodes") -> list[dict]:
    """Best-effort reader for the archived old store. Pulls ``text`` (+ optional
    ``weight``/``source``/``tags`` when present) — the bi-temporal / supersession
    columns are DROPPED on the way in (§12 one-time import). Untested/unexecuted in
    v-min; the secret floor in ``import_corpus`` is what the test contract pins."""
    conn = sqlite3.connect(old_db_path)
    conn.row_factory = sqlite3.Row
    try:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if "text" not in cols:
            raise ValueError(f"old store table {table!r} has no 'text' column (cols={sorted(cols)})")
        sel = ["text"] + [c for c in ("weight", "source", "tags") if c in cols]
        return [dict(r) for r in conn.execute(f"SELECT {', '.join(sel)} FROM {table}")]
    finally:
        conn.close()


def _cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="hive.ops.migration")
    sub = parser.add_subparsers(dest="cmd", required=True)
    imp = sub.add_parser("import-corpus", help="scan + stage an archived old store as pending")
    imp.add_argument("--old-db", required=True)
    imp.add_argument("--new-db", required=True)
    imp.add_argument("--import-admin", default="import-admin")
    imp.add_argument("--scan-secrets", action="store_true",
                     help="run every row through the SecretScanner before stage [B4]")
    imp.add_argument("--old-table", default="episodes")
    args = parser.parse_args(argv)

    if args.cmd == "import-corpus":
        # Lazy imports so a bare `--help` never pulls the adapter stack.
        from hive.adapters.scanner_regex import DefaultSecretScanner
        from hive.adapters.sqlite_db import connect
        from hive.adapters.store_sqlite import SqliteEpisodeStore

        if not args.scan_secrets:                    # the floor is not optional on untrusted import
            _log.error("import.refusing_unscanned_import: --scan-secrets is mandatory [B4]")
            print("refusing to import without --scan-secrets (the §9 floor is mandatory)",
                  file=sys.stderr)
            return 2
        rows = _read_old_rows(args.old_db, table=args.old_table)
        store = SqliteEpisodeStore(connect(args.new_db))
        report = import_corpus(rows, store=store, scanner=DefaultSecretScanner(),
                               import_admin=args.import_admin)
        print(f"import: total={report.n_total} imported={report.n_imported} "
              f"redacted={report.n_redacted} refused={report.n_refused} "
              f"deduped={report.n_deduped}", file=sys.stderr)
        return 0
    return 2


if __name__ == "__main__":  # pragma: no cover — CLI entry (import.sh); not executed in v-min
    raise SystemExit(_cli(sys.argv[1:]))
