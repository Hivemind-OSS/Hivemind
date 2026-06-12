"""creditctl — outcome credit: the host-side trailer scan + the in-container ingest.

Two halves around one NDJSON pipe (`hive credit` connects them, the authctl pattern):

  - ``scan_repo`` (HOST side — runs where a clone/mirror lives; the server never reads
    repos): full-history `git log --grep=<trailer>:` over ALL refs, then per trailer
    commit classify by git's own artifacts — ancestor-of-main ⇒ ``win``, named by a
    revert commit ON main ⇒ ``loss``, else unsettled (emitted ``settled=False``, never
    ingested). NEVER diff text: classification rides format fields + rev-list ancestry
    only. Full rescan every run — idempotent ingest makes re-scans free and closes the
    unsettled-then-settled trap a high-water mark would create.
  - ``ingest`` (CONTAINER side — ``python -m hive.tools.creditctl ingest --ndjson -``):
    trace_id → exposure credit set → one ``task_outcomes`` upsert per
    (commit_sha, episode_id); unknown trace_ids count in the report and write nothing.

Module top level imports ONLY stdlib so the host CLI can import the scan half with no
brain runtime installed; the store imports live inside ``main`` (container side).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from typing import Any, Callable, Mapping, Optional, Sequence, TextIO

# The trailer convention (mirrors producer.stamp_trailer / the rules-block contract).
# The host CLI cannot read container config, so this is an overridable default.
TRAILER_KEY_DEFAULT = "Hive-Trace"

# Mirror-aware main-ref ladder: a bare --mirror clone has no origin/* remote-tracking
# refs, so the bare names must be candidates too. An explicit main_ref always wins.
_MAIN_CANDIDATES = ("origin/main", "origin/master", "main", "master")

AGING_THRESHOLD_DAYS = 14         # unsettled older than this flags the squash-leak alarm

# Separators in git --format OUTPUT. The argv carries git's %x.. escapes (an exec
# argv cannot contain a literal NUL byte); git expands them, so the OUTPUT carries
# these real bytes — content-proof, unlike newlines.
_REC = "\x1e"
_FLD = "\x00"
_FORMAT_FULL = "--format=%H%x00%ct%x00%B%x1e"
_FORMAT_BODY = "--format=%B%x1e"

# sysexits, mirroring authctl/cli
EX_OK = 0
EX_SOFTWARE = 70
EX_CONFIG = 78

Run = Callable[..., "subprocess.CompletedProcess"]


def _default_run(argv: Sequence[str], **_kw: Any) -> subprocess.CompletedProcess:
    return subprocess.run(list(argv), capture_output=True, text=True)


def _git(run: Run, repo: str, *args: str) -> subprocess.CompletedProcess:
    return run(("git", "-C", repo, *args))


def resolve_main_ref(repo_path: str, *, main_ref: Optional[str] = None,
                     run: Optional[Run] = None) -> str:
    """The settlement authority: an explicit ``main_ref`` wins; else the first of the
    mirror-aware ladder that resolves to a commit. No candidate ⇒ fail LOUD (a scan
    that silently classified everything unsettled would read as 'no credit yet')."""
    run = run or _default_run
    candidates = (main_ref,) if main_ref else _MAIN_CANDIDATES
    for cand in candidates:
        p = _git(run, repo_path, "rev-parse", "--verify", "--quiet", f"{cand}^{{commit}}")
        if p.returncode == 0:
            return cand
    raise ValueError(f"no main ref found in {repo_path!r} (tried: {', '.join(candidates)})")


def _repo_label(repo_path: str) -> str:
    base = os.path.basename(os.path.normpath(repo_path))
    return base[:-4] if base.endswith(".git") else base


def scan_repo(repo_path: str, *, trailer_key: str = TRAILER_KEY_DEFAULT,
              main_ref: Optional[str] = None, since: Optional[str] = None,
              repo_label: Optional[str] = None,
              run: Optional[Run] = None) -> list[dict]:
    """Scan one clone/mirror for trailer-carrying commits and classify each:
    win (ancestor of main) / loss (named by a revert commit on main) / unsettled.
    Returns plain dict rows (NDJSON-ready). ``since`` is an operator-owned
    ``--since`` optimization, never scan state. // O(history) once per run."""
    run = run or _default_run
    main = resolve_main_ref(repo_path, main_ref=main_ref, run=run)
    label = repo_label or _repo_label(repo_path)
    trailer_rx = re.compile(rf"^{re.escape(trailer_key)}:[ \t]*(.+)$", re.MULTILINE)

    log_args = ["log", "--all", f"--grep={trailer_key}:", _FORMAT_FULL]
    if since:
        log_args.append(f"--since={since}")
    logged = _git(run, repo_path, *log_args)
    if logged.returncode != 0:
        raise ValueError(f"git log failed in {repo_path!r}: {logged.stderr.strip()}")

    commits: list[tuple[str, int, list[str]]] = []
    for rec in (logged.stdout or "").split(_REC):
        if not rec.strip():
            continue
        sha, ts, body = rec.split(_FLD, 2)
        tokens: list[str] = []
        for m in trailer_rx.finditer(body):
            tokens.extend(m.group(1).split())
        seen: set[str] = set()
        traces = [t for t in tokens if not (t in seen or seen.add(t))]
        if traces:
            commits.append((sha.strip(), int(ts), traces))

    # Losses: shas named on MAIN via git's own revert body ("This reverts commit <sha>.")
    # — format fields only, never diff text.
    reverts = _git(run, repo_path, "log", main, "--grep=This reverts commit",
                   _FORMAT_BODY)
    reverted = set(re.findall(r"This reverts commit ([0-9a-f]{40})",
                              reverts.stdout or "")) if reverts.returncode == 0 else set()

    rows: list[dict] = []
    for sha, ts, traces in commits:
        on_main = _git(run, repo_path, "merge-base", "--is-ancestor", sha, main
                       ).returncode == 0
        if sha in reverted:
            settled, outcome = True, "loss"
        elif on_main:
            settled, outcome = True, "win"
        else:
            settled, outcome = False, None
        rows.append({"commit_sha": sha, "commit_ts": ts, "repo": label,
                     "trace_ids": traces, "settled": settled, "outcome": outcome})
    return rows


def scan_summary(rows: Sequence[Mapping[str, Any]], *, now: int,
                 aging_days: int = AGING_THRESHOLD_DAYS) -> dict:
    """Host-side report numbers. ``aged_unsettled`` is the squash-leak alarm: trailer
    commits that never reached main past the threshold — report-only, never a gate."""
    unsettled = [r for r in rows if not r["settled"]]
    return {
        "trailer_commits": len(rows),
        "wins": sum(1 for r in rows if r.get("outcome") == "win"),
        "losses": sum(1 for r in rows if r.get("outcome") == "loss"),
        "unsettled": len(unsettled),
        "aged_unsettled": sum(1 for r in unsettled
                              if now - int(r["commit_ts"]) > aging_days * 86400),
        "aging_days": aging_days,
    }


def ingest(store: Any, rows: Sequence[Mapping[str, Any]], *, now: int) -> dict:
    """Upsert settled rows' credits: each trace's exposure set becomes one
    (commit_sha, episode_id) outcome row. Unsettled rows are skipped (the scan emits
    them for reporting only); unknown trace_ids count and write nothing. Idempotent
    by the store's PK-scoped upsert — re-ingesting the same scan is free. // O(rows)."""
    ingested = duplicates = unknown = skipped = 0
    for row in rows:
        if not row.get("settled"):
            skipped += 1
            continue
        for trace_id in row.get("trace_ids", ()):
            credit_set = store.exposures_by_trace(str(trace_id))
            if not credit_set:
                unknown += 1
                continue
            for episode_id, margin in credit_set:
                landed = store.record_outcome(
                    commit_sha=str(row["commit_sha"]), episode_id=int(episode_id),
                    trace_id=str(trace_id), repo=row.get("repo"),
                    outcome=str(row["outcome"]), recall_margin=float(margin),
                    commit_ts=int(row["commit_ts"]), ingested_ts=int(now))
                if landed:
                    ingested += 1
                else:
                    duplicates += 1
    return {"ingested": ingested, "duplicates": duplicates, "unknown_traces": unknown,
            "unsettled_skipped": skipped, "totals": store.outcome_totals()}


def main(argv: Optional[list[str]] = None, *, env: Optional[Mapping[str, str]] = None,
         connect_fn: Optional[Callable[[str], Any]] = None,
         out: Optional[TextIO] = None, stdin: Optional[TextIO] = None) -> int:
    """The container-side entry: read NDJSON rows, upsert credits, print one JSON
    report line to stdout (machine-parseable; diagnostics go to stderr). db_path
    resolves from --db / $HIVE_STORE__DB_PATH and FAILS FAST if absent — the
    authctl contract exactly. // O(rows)."""
    env = os.environ if env is None else env
    out = out if out is not None else sys.stdout
    stdin = stdin if stdin is not None else sys.stdin

    parser = argparse.ArgumentParser(
        prog="hive.tools.creditctl",
        description="Ingest settled outcome-credit rows (NDJSON) into task_outcomes.")
    parser.add_argument("--db", default=None,
                        help="SQLite store path (default: $HIVE_STORE__DB_PATH)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_ingest = sub.add_parser("ingest", help="read NDJSON rows and upsert credits")
    p_ingest.add_argument("--ndjson", default="-", help="rows file, or - for stdin")
    args = parser.parse_args(argv)

    db_path = (args.db or env.get("HIVE_STORE__DB_PATH") or "").strip()
    if not db_path:
        print("creditctl: --db or $HIVE_STORE__DB_PATH is required", file=sys.stderr)
        return EX_CONFIG

    # Container-side imports, deliberately NOT module-level: the host CLI imports this
    # module for the scan half and must not pull the brain runtime (numpy et al).
    from hive.adapters.sqlite_db import connect
    from hive.adapters.store_sqlite import SqliteEpisodeStore

    store = SqliteEpisodeStore((connect_fn or connect)(db_path))

    fh = stdin if args.ndjson == "-" else open(args.ndjson, encoding="utf-8")
    rows: list[dict] = []
    try:
        for lineno, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                print(f"creditctl: bad NDJSON on line {lineno}", file=sys.stderr)
                return EX_SOFTWARE
    finally:
        if fh is not stdin:
            fh.close()

    report = ingest(store, rows, now=int(time.time()))
    print(json.dumps(report, sort_keys=True), file=out)
    return EX_OK


if __name__ == "__main__":  # pragma: no cover — module entry (`python -m hive.tools.creditctl`)
    raise SystemExit(main(sys.argv[1:]))
