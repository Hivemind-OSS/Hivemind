"""originctl — outcome credit over the GitHub API: the host-side stateless scan
+ the in-container settled-credit ingest.

Two halves around one NDJSON pipe (`hive origin sync` connects them — the authctl
seam pattern):

  - ``scan_github`` (HOST side): a stateless poller over MERGED REALITY only,
    inside a sliding ``lookback_days`` window. Win sources: merged PRs (trailers
    harvested from ``/pulls/{n}/commits`` — GitHub keeps ``refs/pull/N/head``, so
    a squash-merge with a deleted branch still serves the constituent trailers;
    win rows keyed ``merge_commit_sha``) and default-branch direct pushes (rows
    keyed their own sha). Losses: revert commits naming a sha. Rebase-merge twins
    are dropped by TRACE-claim dedup — a rebase rewrites constituent shas, so the
    trace ids are the only stable join. No mirror, no cursor state; the idempotent
    ingest makes hourly full-window rescans free.
  - ``ingest`` (CONTAINER side — ``python -m hive.tools.originctl ingest``):
    credits ``claimed ∩ exposures_by_trace(trace_id)`` — only memories actually
    served on the named trace can be credited (anti-gaming); loss rows flip that
    sha's win rows one-way via ``store.settle_loss`` (monotone under rescans).

Module top level imports ONLY stdlib so the host CLI can import the scan half
with no brain runtime installed; the store imports live inside ``main``.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable, Iterator, Mapping, Optional, Sequence, TextIO

# The trailer convention v2: ``<key>: <trace32hex> <episode_id> [...]`` — the agent
# names ONLY the memories that materially shaped the committed code. The key is
# single-sourced from producer.stamp_trailer; this is the host-side overridable
# default (the host CLI cannot read container config).
TRAILER_KEY_DEFAULT = "Hive-Credit"
# The v1 key — inert for credit (v1 wording promised "changes no reward") but
# COUNTED every sync as the re-onboard nudge.
LEGACY_TRAILER_KEY = "Hive-Trace"

DEFAULT_LOOKBACK_DAYS = 14
FIRST_SYNC_LOOKBACK_DAYS = 90

API_BASE = "https://api.github.com"
PER_PAGE = 100
_DAY_S = 86_400

# sysexits, mirroring authctl/cli
EX_OK = 0
EX_SOFTWARE = 70
EX_CONFIG = 78

# (url, headers) -> (status, response_headers, body) — the injection seam that
# keeps every scan test hermetic (zero network).
Fetch = Callable[[str, Mapping[str, str]], tuple[int, Mapping[str, str], bytes]]

# Loss source: git's own revert body format — never diff text.
_REVERT_RX = re.compile(r"This reverts commit ([0-9a-f]{40})")
_TRACE_RX = re.compile(r"[0-9a-fA-F]{32}")
_EPISODE_RX = re.compile(r"[1-9][0-9]*")
_REPO_PART_RX = re.compile(r"[A-Za-z0-9_.-]+")


class GithubApiError(RuntimeError):
    """A non-200 GitHub answer. ``status`` lets the CLI map 401/403/404 onto
    EX_CONFIG (token/scope problems) without parsing prose."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = int(status)


def _default_fetch(url: str, headers: Mapping[str, str]
                   ) -> tuple[int, Mapping[str, str], bytes]:
    req = urllib.request.Request(url, headers=dict(headers))
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return int(resp.status), dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:        # a non-2xx answer IS the response
        return int(e.code), dict(e.headers or {}), e.read() or b""


def parse_repo_arg(target: str) -> str:
    """Normalize ``owner/repo`` | ``https://github.com/owner/repo[.git][/]`` |
    ``git@github.com:owner/repo[.git]`` → ``owner/repo``. Any other host raises
    ValueError — this scanner speaks ONLY the GitHub API. // O(len)."""
    t = (target or "").strip()
    if t.startswith("git@"):
        host, _, path = t[len("git@"):].partition(":")
        if host.lower() != "github.com":
            raise ValueError(f"not a github.com repo: {target!r}")
        t = path
    elif "://" in t or t.lower().startswith("github.com"):
        rest = t.split("://", 1)[-1]
        host, _, path = rest.partition("/")
        if host.lower() != "github.com":
            raise ValueError(f"not a github.com repo: {target!r}")
        t = path
    t = t.strip("/")
    if t.lower().endswith(".git"):
        t = t[:-4]
    parts = t.split("/")
    if len(parts) != 2 or not all(_REPO_PART_RX.fullmatch(p) for p in parts):
        raise ValueError(f"cannot parse {target!r} as owner/repo "
                         "(use owner/repo or a github.com URL)")
    return "/".join(parts)


def parse_trailer_lines(body: str, *, trailer_key: str
                        ) -> tuple[list[tuple[str, list[int]]], int]:
    """STRICT v2 trailer grammar over one commit message:
    ``<key>: <trace32hex> <episode_id> [...]`` — commas tolerated as separators,
    key matched case-insensitively (git's own trailer semantics), hex normalized
    to lowercase. Returns ``(groups, malformed)``: groups in message order;
    a line naming the key but failing the grammar is COUNTED malformed and never
    guessed at. Multiple lines per message allowed. // O(len(body))."""
    pat = re.compile(rf"^{re.escape(trailer_key)}:[ \t]*(.*)$",
                     re.IGNORECASE | re.MULTILINE)
    groups: list[tuple[str, list[int]]] = []
    malformed = 0
    for m in pat.finditer(body or ""):
        tokens = [t for t in re.split(r"[\s,]+", m.group(1).strip()) if t]
        if (len(tokens) < 2 or not _TRACE_RX.fullmatch(tokens[0])
                or not all(_EPISODE_RX.fullmatch(t) for t in tokens[1:])):
            malformed += 1
            continue
        groups.append((tokens[0].lower(), [int(t) for t in tokens[1:]]))
    return groups, malformed


def _iso_ts(s: str) -> int:
    """GitHub ISO-8601 (``2026-06-01T12:00:00Z``) → epoch seconds."""
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


def _ts_iso(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc
                                  ).strftime("%Y-%m-%dT%H:%M:%SZ")


def _get_json(fetch: Fetch, url: str, headers: Mapping[str, str]) -> Any:
    status, _hdrs, body = fetch(url, headers)
    if status != 200:
        snippet = (body or b"")[:200].decode("utf-8", "replace")
        raise GithubApiError(status, f"GitHub API {status} on {url}: {snippet}")
    return json.loads(body or b"null")


def _paged(fetch: Fetch, url_base: str, headers: Mapping[str, str]
           ) -> Iterator[list]:
    """Yield successive pages of a list endpoint; a short or empty page ends the
    walk (lazy — a consumer breaking early never costs the next request)."""
    page = 1
    while True:
        items = _get_json(fetch, f"{url_base}&page={page}", headers)
        if not isinstance(items, list):
            raise GithubApiError(
                0, f"GitHub API shape surprise (expected a list) on {url_base}")
        if items:
            yield items
        if len(items) < PER_PAGE:
            return
        page += 1


def _headers(token: Optional[str]) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "hive-origin",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def probe_repo(repo: str, *, token: Optional[str],
               fetch: Optional[Fetch] = None) -> None:
    """The link verb's access validation: GET ``/repos/{owner}/{repo}`` and raise
    ``GithubApiError`` on any non-200. 404 means missing OR private-without-scope
    (GitHub deliberately conflates them) — the caller owns the PAT hint. // O(1)."""
    fetch = fetch or _default_fetch
    _get_json(fetch, f"{API_BASE}/repos/{repo}", _headers(token))


def _win_row(sha: str, ts: int, repo: str,
             groups: Sequence[tuple[str, list[int]]]) -> dict:
    return {"kind": "win", "commit_sha": sha, "commit_ts": int(ts), "repo": repo,
            "credits": [{"trace_id": t, "episode_ids": list(eids)}
                        for t, eids in groups]}


def scan_github(repo: str, *, token: Optional[str], now: int,
                lookback_days: int = DEFAULT_LOOKBACK_DAYS,
                trailer_key: str = TRAILER_KEY_DEFAULT,
                fetch: Optional[Fetch] = None) -> tuple[list[dict], dict]:
    """One stateless sweep of merged reality inside the lookback window. Returns
    ``(rows, summary)`` — win rows before loss rows; summary carries the integrity
    counters ``{win_rows, loss_rows, credit_groups, malformed_trailer_lines,
    legacy_trailers_seen, deduped_rebase_copies, prs_scanned}``.
    // O(PRs + commits in window) API pages."""
    fetch = fetch or _default_fetch
    cutoff = int(now) - int(lookback_days) * _DAY_S
    headers = _headers(token)

    wins: list[dict] = []
    losses: list[dict] = []
    claimed: set[str] = set()        # trace ids credited via a PR row this scan
    s = {"win_rows": 0, "loss_rows": 0, "credit_groups": 0,
         "malformed_trailer_lines": 0, "legacy_trailers_seen": 0,
         "deduped_rebase_copies": 0, "prs_scanned": 0}

    legacy_rx = re.compile(rf"^{re.escape(LEGACY_TRAILER_KEY)}[ \t]*:",
                           re.IGNORECASE | re.MULTILINE)
    count_legacy = trailer_key.lower() != LEGACY_TRAILER_KEY.lower()

    def harvest(body: str) -> list[tuple[str, list[int]]]:
        if count_legacy:
            s["legacy_trailers_seen"] += len(legacy_rx.findall(body or ""))
        groups, malformed = parse_trailer_lines(body, trailer_key=trailer_key)
        s["malformed_trailer_lines"] += malformed
        return groups

    # ── a. merged PRs (primary). Sorted by updated DESC ⇒ the first pre-cutoff
    #      item ends the walk (a merge updates the PR, so updated_at >= merged_at:
    #      nothing merged in-window can hide behind it).
    pulls_base = (f"{API_BASE}/repos/{repo}/pulls"
                  f"?state=closed&sort=updated&direction=desc&per_page={PER_PAGE}")
    done = False
    for items in _paged(fetch, pulls_base, headers):
        for pr in items:
            if _iso_ts(pr["updated_at"]) < cutoff:
                done = True
                break
            merged_at = pr.get("merged_at")
            if not merged_at or _iso_ts(merged_at) < cutoff:
                continue            # closed-unmerged, or merged before the window
            merge_sha = pr.get("merge_commit_sha")
            if not merge_sha:
                continue            # no settled artifact to key a row on
            s["prs_scanned"] += 1
            pr_commits_base = (f"{API_BASE}/repos/{repo}/pulls/{pr['number']}"
                               f"/commits?per_page={PER_PAGE}")
            credits: list[tuple[str, list[int]]] = []
            for cpage in _paged(fetch, pr_commits_base, headers):
                for c in cpage:
                    credits.extend(harvest(c["commit"]["message"]))
            if credits:
                wins.append(_win_row(merge_sha, _iso_ts(merged_at), repo, credits))
                claimed.update(t for t, _ in credits)
        if done:
            break

    # ── b. default-branch commits listing: direct-push wins + revert losses.
    commits_base = (f"{API_BASE}/repos/{repo}/commits"
                    f"?since={_ts_iso(cutoff)}&per_page={PER_PAGE}")
    for items in _paged(fetch, commits_base, headers):
        for c in items:
            body = c["commit"]["message"]
            ts = _iso_ts(c["commit"]["committer"]["date"])
            for named in _REVERT_RX.findall(body or ""):
                losses.append({"kind": "loss", "commit_sha": named,
                               "commit_ts": ts, "repo": repo})
            groups = harvest(body)
            if not groups:
                continue
            if {t for t, _ in groups} & claimed:
                # the rebase-merge twin: the same trace ids under rewritten shas
                s["deduped_rebase_copies"] += 1
                continue
            wins.append(_win_row(c["sha"], ts, repo, groups))

    s["win_rows"] = len(wins)
    s["loss_rows"] = len(losses)
    s["credit_groups"] = sum(len(w["credits"]) for w in wins)
    return wins + losses, s


def ingest(store: Any, rows: Sequence[Mapping[str, Any]], *, now: int) -> dict:
    """Land one scan batch: win rows FIRST (claimed ∩ served per trace — only
    memories the named trace actually exposed can be credited, with the REAL
    recall_margin recovered from the exposure row), then loss rows (one-way
    ``settle_loss`` flips). Win-before-loss ordering + the PK-scoped DO-NOTHING
    insert + the one-way flip keep the ledger MONOTONE under stateless hourly
    rescans — a merge+revert arriving in one batch settles loss, and a later
    re-scan re-offering the win cannot resurrect it. Unknown traces and unserved
    claims are counted, written nowhere. // O(rows · credit-set size)."""
    wins_ingested = duplicates = unknown = unserved = losses_flipped = 0
    win_rows = [r for r in rows if r.get("kind") == "win"]
    loss_rows = [r for r in rows if r.get("kind") == "loss"]
    for row in win_rows:
        for group in row.get("credits", ()):
            trace_id = str(group.get("trace_id", ""))
            served = dict(store.exposures_by_trace(trace_id))
            if not served:
                unknown += 1
                continue
            for eid in group.get("episode_ids", ()):
                if int(eid) not in served:
                    unserved += 1          # claimed but never served on this trace
                    continue
                landed = store.record_outcome(
                    commit_sha=str(row["commit_sha"]), episode_id=int(eid),
                    trace_id=trace_id, repo=row.get("repo"),
                    outcome="win", recall_margin=float(served[int(eid)]),
                    commit_ts=int(row["commit_ts"]), ingested_ts=int(now))
                if landed:
                    wins_ingested += 1
                else:
                    duplicates += 1
    for row in loss_rows:
        losses_flipped += int(store.settle_loss(str(row["commit_sha"]),
                                                ts=int(now)))
    return {"wins_ingested": wins_ingested, "duplicates": duplicates,
            "unknown_traces": unknown, "unserved_claims": unserved,
            "losses_flipped": losses_flipped, "totals": store.outcome_totals()}


def main(argv: Optional[list[str]] = None, *, env: Optional[Mapping[str, str]] = None,
         connect_fn: Optional[Callable[[str], Any]] = None,
         out: Optional[TextIO] = None, stdin: Optional[TextIO] = None) -> int:
    """The container-side entry: read NDJSON rows, land the batch, print ONE JSON
    report line to stdout (machine-parseable; diagnostics go to stderr). db_path
    resolves from --db / $HIVE_STORE__DB_PATH and FAILS FAST if absent — the
    authctl contract exactly. // O(rows)."""
    env = os.environ if env is None else env
    out = out if out is not None else sys.stdout
    stdin = stdin if stdin is not None else sys.stdin

    parser = argparse.ArgumentParser(
        prog="hive.tools.originctl",
        description="Ingest win/loss credit rows (NDJSON) into task_outcomes.")
    parser.add_argument("--db", default=None,
                        help="SQLite store path (default: $HIVE_STORE__DB_PATH)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_ingest = sub.add_parser("ingest", help="read NDJSON rows and land win/loss credit")
    p_ingest.add_argument("--ndjson", default="-", help="rows file, or - for stdin")
    args = parser.parse_args(argv)

    db_path = (args.db or env.get("HIVE_STORE__DB_PATH") or "").strip()
    if not db_path:
        print("originctl: --db or $HIVE_STORE__DB_PATH is required", file=sys.stderr)
        return EX_CONFIG

    # Container-side imports, deliberately NOT module-level: the host CLI imports
    # this module for the scan half and must not pull the brain runtime (numpy et al).
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
                print(f"originctl: bad NDJSON on line {lineno}", file=sys.stderr)
                return EX_SOFTWARE
    finally:
        if fh is not stdin:
            fh.close()

    report = ingest(store, rows, now=int(time.time()))
    print(json.dumps(report, sort_keys=True), file=out)
    return EX_OK


if __name__ == "__main__":  # pragma: no cover — module entry (`python -m hive.tools.originctl`)
    raise SystemExit(main(sys.argv[1:]))
