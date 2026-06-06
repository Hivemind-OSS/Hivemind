"""GitCliSource — the DEFAULT adapter behind the ``OutcomeSource`` port [D6/M09§3].

This is the swappability-mandate adapter for the outcome producer: ALL git /
subprocess / blame / squash / locale impurity is sealed here, so the pure
``OutcomeJoiner`` (which owns 100% of the §11 credit policy) and the
``OutcomeProducer.step`` tick stay byte-unchanged when the source is swapped for a
webhook/sidecar producer (the §12 second adapter) — one config key + one file.

Per-repo: the built port is ``poll(self) -> SourcePoll`` and ``SourcePoll`` carries a
single ``repo_remote``, so one ``GitCliSource`` watches one repo; multi-repo fan-out
is the M11 registry's job. The cursor (last-seen newest SHA) is held in-memory and
exposed via ``.cursor`` so M11 can persist/restore it to the ``SqliteMeta`` kv
(``producer_repo_cursor:<repo>``) across process restarts.

INVARIANTS:
  * poll-never-raises (I1 at the source) — any boundary failure (missing repo,
    subprocess error, hard timeout, parse drift) returns ``SourcePoll(ok=False,
    error=...)``; the cursor is NOT advanced, and a per-commit ``show``/``blame``
    failure drops only that commit (the poll stays ``ok``).
  * hard subprocess timeout per call (``timeout_s``) — ``TimeoutExpired`` is caught.
  * secret-safe (§6/§9) — log lines and ``SourcePoll.error`` carry ONLY shas / repo /
    counts / command-class / exception-type. A commit subject/body is NEVER logged
    and NEVER persisted (it rides only on the in-memory ``CommitFact.message``).
  * ``introduced_lines`` (post-image added lines) and ``touched_blame`` (a bugfix's
    pre-image lines blamed back to their ORIGINAL line numbers) live in the SAME
    coordinate space, so the joiner's ``touched_blame & introduced_lines`` overlap
    (Decision B, blame-line not same-file) is meaningful.

Complexity: one ``git log`` per poll + one ``git show`` per commit + one ``git
blame`` per bugfix touched-file-hunk. Bounded by ``max_commits`` (default 200) and,
after the first poll, by the cursor delta (typically a handful per tick) — a
background, non-user-facing tick, so the per-commit fan-out is acceptable (§7).
"""
from __future__ import annotations

import logging
import re
import subprocess
from typing import Callable, Optional

from hive.domain.models import CommitFact, SourcePoll

_log = logging.getLogger("hive.source")

# ASCII RS / US — record + field separators for the git-log format. These bytes are
# effectively never present in commit metadata, so the parse cannot be spoofed by
# content (a newline-delimited format would break on multi-line bodies).
_RS = "\x1e"
_US = "\x1f"

_DEFAULT_BUGFIX = re.compile(r"^(fix|bug|hotfix|patch):", re.IGNORECASE)
_BUGFIX_HINTS = re.compile(r"\b(BUG-\d+|regression|crash|race)\b", re.IGNORECASE)
# git's default revert body line: "This reverts commit <sha>."
_REVERTS_RE = re.compile(r"This reverts commit ([0-9a-fA-F]{7,40})")
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
# a `git blame --porcelain` group/line header: "<sha> <origline> <finalline> [count]"
_BLAME_HDR_RE = re.compile(r"^([0-9a-f]{7,40}) (\d+) (\d+)(?: (\d+))?$")


def _default_runner(args, *, cwd: str, timeout: float) -> subprocess.CompletedProcess:
    """Run ``git -C <cwd> <args>`` with a HARD timeout, never raising on a non-zero
    exit (callers branch on ``returncode``). The single real-subprocess seam; tests
    inject a fake ``runner`` with the same shape."""
    return subprocess.run(
        ["git", "-C", cwd, *args],
        capture_output=True, text=True, timeout=timeout, check=False,
    )


def _c_unquote(s: str) -> str:
    """Decode git's C-style path quoting (octal ``\\303`` bytes, ``\\t \\n \\" \\\\``)
    back to a UTF-8 string. git emits this ONLY inside double-quotes (quotePath)."""
    try:
        return (s.encode("latin-1", "backslashreplace")
                 .decode("unicode_escape")
                 .encode("latin-1")
                 .decode("utf-8", "replace"))
    except (UnicodeDecodeError, UnicodeEncodeError):
        return s


def _diff_path(line: str, prefix: str) -> Optional[str]:
    """Resolve a unified-diff file marker (``--- a/foo`` / ``+++ b/foo`` /
    ``+++ /dev/null`` / C-quoted ``+++ "b/caf\\303\\251.py"``) to a path; ``None`` for
    /dev/null. Order matters: git's ``a/``/``b/`` prefix is INSIDE the quotes, so the
    QUOTES are stripped (and C-unescaped) FIRST, then the prefix — otherwise a quoted
    non-ASCII/tab/backslash path keeps its prefix and stays octal-escaped (audit
    P1.8-DIFF-01). The trailing TAB git appends to delimit a space-containing unquoted
    path is also dropped."""
    rest = line[4:].rstrip("\t\n").strip()
    if len(rest) >= 2 and rest[0] == '"' and rest[-1] == '"':
        rest = _c_unquote(rest[1:-1])        # quotes first, then decode the bytes
    if rest == "/dev/null":
        return None
    if rest.startswith(prefix):
        rest = rest[len(prefix):]
    return rest or None


class GitCliSource:
    """Default ``OutcomeSource``: shells ``git log/show/blame`` behind one ``poll()``."""

    def __init__(
        self, repo_path: str, *,
        repo_remote: str = "",
        language: str = "",
        bugfix_pattern=_DEFAULT_BUGFIX,
        stamp_trailer: str = "Hive-Trace",
        timeout_s: float = 10.0,
        max_commits: int = 200,
        since_sha: Optional[str] = None,
        runner: Optional[Callable] = None,
    ) -> None:
        self._repo = str(repo_path)
        self._repo_remote = str(repo_remote)
        self._language = str(language)
        self._bugfix = (bugfix_pattern if hasattr(bugfix_pattern, "search")
                        else re.compile(bugfix_pattern, re.IGNORECASE))
        self._trailer = str(stamp_trailer)
        self._timeout_s = float(timeout_s)
        self._max_commits = int(max_commits)
        self._cursor: Optional[str] = since_sha
        self._run = runner or _default_runner

    @property
    def cursor(self) -> Optional[str]:
        """Last-seen newest SHA (the watermark M11 persists to the meta kv)."""
        return self._cursor

    # ── the port ─────────────────────────────────────────────────────────────────
    def poll(self) -> SourcePoll:
        """One per-repo poll. NEVER raises: any boundary failure → ok=False (cursor
        held). The newest SHA becomes the cursor only on a successful enumeration."""
        remote = self._resolve_remote()
        try:
            records = self._log_records()
        except Exception as e:                          # I1: missing repo / timeout / drift
            _log.warning("source.poll_failed repo=%s err=%s", self._repo, type(e).__name__)
            return SourcePoll(repo_remote=remote, language=self._language,
                              ok=False, error=f"{type(e).__name__}: {e}")
        if records is None:
            # git log returned non-zero (e.g. not-a-repo) — fail-closed, cursor held.
            return SourcePoll(repo_remote=remote, language=self._language,
                              ok=False, error="git log returned non-zero")

        newest = records[0][0] if records else None
        commits: list[CommitFact] = []
        n_bugfix = dropped = 0
        for meta in reversed(records):                  # oldest → newest (natural ts order)
            fact = self._build_fact(meta, remote)
            if fact is None:
                dropped += 1                            # per-commit show/ts failure: skip it
                continue
            commits.append(fact)
            if fact.kind == "bugfix":
                n_bugfix += 1

        # Advance the cursor ONLY when EVERY enumerated commit built into a CommitFact.
        # If any was dropped (a transient show/timeout), HOLD the cursor so the whole
        # cursor..HEAD range is retried next tick — advancing to `newest` would jump the
        # watermark past the dropped (older) commit, excluding it forever (audit
        # P18-CURSOR-01: permanent credit loss). Re-emit of the rebuilt commits is safe
        # (the producer's task_outcomes PK upsert + watermark are idempotent).
        if newest is not None and dropped == 0:
            self._cursor = newest
        elif dropped:
            _log.warning("source.cursor_held repo=%s dropped=%d built=%d — "
                         "range retried next poll", self._repo, dropped, len(commits))
        _log.info("source.polled repo=%s commits=%d bugfix=%d dropped=%d", self._repo,
                  len(commits), n_bugfix, dropped)
        return SourcePoll(repo_remote=remote, language=self._language,
                          commits=tuple(commits), ok=True)

    # ── phase A: git log → metadata records ──────────────────────────────────────
    def _log_records(self):
        """Return a list of (sha, parents, ts, subject, trailers, body) tuples,
        newest-first. ``None`` if git log exits non-zero (after the cursor fallback).
        Raises only on a subprocess-level failure (caught by poll)."""
        fmt = _RS + _US.join([
            "%H", "%P", "%ct", "%s",
            f"%(trailers:key={self._trailer},valueonly,separator=%x20)", "%b",
        ])
        rng = f"{self._cursor}..HEAD" if self._cursor else "HEAD"
        cp = self._git("log", rng, "--first-parent", "-n", str(self._max_commits),
                       "--no-color", f"--pretty=format:{fmt}")
        if cp.returncode != 0 and self._cursor:
            # cursor rebased/squashed away ⇒ range invalid; fall back to last-N.
            _log.warning("source.cursor_range_invalid repo=%s — full rescan", self._repo)
            cp = self._git("log", "HEAD", "--first-parent", "-n", str(self._max_commits),
                           "--no-color", f"--pretty=format:{fmt}")
        if cp.returncode != 0:
            return None
        out: list[tuple] = []
        for raw in cp.stdout.split(_RS):
            rec = raw.strip("\n")
            if not rec:
                continue
            f = rec.split(_US)
            if len(f) < 6:
                continue                                # porcelain drift: skip record
            out.append((f[0], f[1], f[2], f[3], f[4], f[5]))
        return out

    # ── phase B+C: build one CommitFact (show diff + bugfix blame) ───────────────
    def _build_fact(self, meta, remote: str) -> Optional[CommitFact]:
        sha, parents, ts_s, subject, trailers, body = meta
        try:
            ts = int(ts_s)
        except ValueError:
            return None
        reverts = None
        m = _REVERTS_RE.search(body) or _REVERTS_RE.search(subject)
        if m:
            reverts = m.group(1)
        is_revert = reverts is not None or subject.startswith('Revert "')
        is_bugfix = bool(self._bugfix.match(subject) or _BUGFIX_HINTS.search(subject))
        n_parents = len([p for p in parents.split() if p])
        if is_revert:
            kind = "revert"
        elif is_bugfix:
            kind = "bugfix"
        elif n_parents > 1:
            kind = "merge"
        else:
            kind = "commit"

        diff = self._show(sha)
        if diff is None:
            return None                                  # show failed: drop this commit
        introduced, files, pre_ranges = self._parse_diff(diff)

        touched_blame: set[int] = set()
        if kind == "bugfix":
            touched_blame = self._blame_originals(sha, pre_ranges)

        trace_ids = tuple(t for t in trailers.split() if t)
        return CommitFact(
            sha=sha, ts=ts, kind=kind, message=subject, repo_remote=remote,
            files_touched=tuple(sorted(files)),
            introduced_lines=frozenset(introduced),
            touched_blame=frozenset(touched_blame),
            reverts=reverts, trailer_traces=trace_ids,
        )

    def _show(self, sha: str) -> Optional[str]:
        try:
            # core.quotePath=false ⇒ non-ASCII paths arrive literal UTF-8 (the common
            # case); _diff_path's C-unescape is the belt for tab/quote/newline names.
            cp = self._git("-c", "core.quotePath=false", "show", sha,
                           "--first-parent", "--no-color", "--unified=0", "--format=")
        except Exception as e:
            _log.warning("source.show_failed repo=%s sha=%s err=%s",
                         self._repo, sha, type(e).__name__)
            return None
        if cp.returncode != 0:
            _log.warning("source.show_nonzero repo=%s sha=%s rc=%d",
                         self._repo, sha, cp.returncode)
            return None
        return cp.stdout

    @staticmethod
    def _parse_diff(diff: str):
        """Parse a ``--unified=0`` diff → (introduced_lines, files, pre_ranges).
        introduced_lines = the POST-image added ranges (``+c,d`` → c..c+d-1);
        pre_ranges = {old_path: [(start, count)…]} the PRE-image lines a hunk removes
        or changes (the blame targets). files = every touched path.

        A ``--- ``/``+++ `` line is a FILE HEADER only in the per-file preamble (after
        ``diff --git`` and BEFORE the first ``@@`` of that file). Once inside hunks,
        a removed/added CONTENT line whose text begins with ``-- ``/``++ `` renders as
        ``--- <content>``/``+++ <content>`` and must NOT be mistaken for a header
        (audit P18-001: a SQL ``--`` comment / YAML ``---`` would corrupt old_path and
        drop the blame target). ``in_hunks`` resets only on a new ``diff --git`` line."""
        introduced: set[int] = set()
        files: set[str] = set()
        pre_ranges: dict[str, list[tuple[int, int]]] = {}
        old_path: Optional[str] = None
        new_path: Optional[str] = None
        in_hunks = False
        for line in diff.splitlines():
            if line.startswith("diff --git "):
                old_path = new_path = None               # new file section
                in_hunks = False
            elif not in_hunks and line.startswith("--- "):
                old_path = _diff_path(line, "a/")
            elif not in_hunks and line.startswith("+++ "):
                new_path = _diff_path(line, "b/")
                if new_path:
                    files.add(new_path)
                elif old_path:
                    files.add(old_path)                  # pure deletion: record old path
            elif line.startswith("@@"):
                in_hunks = True                          # any '--- '/'+++ ' after this is content
                hm = _HUNK_RE.match(line)
                if not hm:
                    continue
                a, b = int(hm.group(1)), int(hm.group(2) or 1)   # pre-image start,count
                c, d = int(hm.group(3)), int(hm.group(4) or 1)   # post-image start,count
                if d > 0 and new_path:
                    introduced.update(range(c, c + d))
                if b > 0 and old_path:
                    pre_ranges.setdefault(old_path, []).append((a, b))
        return introduced, files, pre_ranges

    def _blame_originals(self, sha: str, pre_ranges) -> set[int]:
        """Blame each pre-image range at the PARENT (``<sha>^``) → the ORIGINAL line
        numbers the bugfix touched. A blame failure on one file/hunk yields nothing
        for it (logged) but never crashes the poll."""
        parent = f"{sha}^"
        originals: set[int] = set()
        for path, ranges in pre_ranges.items():
            for (start, count) in ranges:
                try:
                    cp = self._git("blame", parent, "-L", f"{start},+{count}",
                                   "--porcelain", "--", path)
                except Exception as e:
                    _log.warning("source.blame_failed repo=%s sha=%s err=%s",
                                 self._repo, sha, type(e).__name__)
                    continue
                if cp.returncode != 0:
                    _log.warning("source.blame_nonzero repo=%s sha=%s rc=%d",
                                 self._repo, sha, cp.returncode)
                    continue
                for bl in cp.stdout.splitlines():
                    hb = _BLAME_HDR_RE.match(bl)
                    if hb:
                        originals.add(int(hb.group(2)))   # the ORIGINAL line number
        return originals

    # ── helpers ──────────────────────────────────────────────────────────────────
    def _git(self, *args) -> subprocess.CompletedProcess:
        return self._run(list(args), cwd=self._repo, timeout=self._timeout_s)

    def _resolve_remote(self) -> str:
        """Best-effort canonical remote (configured value wins). Never fails the
        poll — an unresolvable remote collapses family axis-0 to '*' downstream."""
        if self._repo_remote:
            return self._repo_remote
        try:
            cp = self._git("remote", "get-url", "origin")
            if cp.returncode == 0:
                return cp.stdout.strip()
        except Exception as e:
            _log.warning("source.remote_unresolved repo=%s err=%s",
                         self._repo, type(e).__name__)
        return ""
