"""P1.8 — GitCliSource: the real OutcomeSource adapter (git-CLI poll behind the
port [D6/M09§3]). Two test layers, same port (swap axis = test axis):

  * FAKE-RUNNER unit tests — a deterministic stand-in for ``subprocess.run`` returns
    canned git output, so every parse/classify/fail-closed/secret-safe branch is
    pinned with no live repo (millisecond, hermetic).
  * REAL tmp-git integration tests — an actual repo with a merge, a squash-merge, a
    revert, and a bugfix-whose-blame-overlaps commit, proving the porcelain parse +
    the blame round-trip against real git (the §8 mandate), and that the produced
    CommitFacts plug into the BUILT pure OutcomeJoiner unchanged.
"""
from __future__ import annotations

import logging
import random
import re
import shutil
import subprocess
from typing import Optional

import pytest

from hive.adapters.git_source import GitCliSource, _default_runner
from hive.domain.join import OutcomeJoiner
from hive.domain.models import RecallWindow, WindowTrace
from hive.domain.ports import OutcomeSource

# ── ASCII record/field separators the adapter formats git output with ────────────
RS = "\x1e"
US = "\x1f"


# ── a fake `runner(args, *, cwd, timeout) -> CompletedProcess-like` ──────────────
class _Run:
    """Minimal CompletedProcess stand-in (returncode/stdout/stderr only)."""
    def __init__(self, stdout: str = "", returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


class FakeRunner:
    """Routes a git invocation to a canned reply by its FIRST git subcommand
    (log/show/blame/remote). ``log`` is a single reply; ``show``/``blame`` are keyed
    by the SHA argument so per-commit phases resolve deterministically. A subcommand
    set to the sentinel ``BOOM`` raises (simulates a porcelain/timeout failure)."""
    BOOM = object()

    def __init__(self, *, log="", shows=None, blames=None, remote="",
                 raise_on=()) -> None:
        self.log = log
        self.shows = dict(shows or {})       # sha -> diff text
        self.blames = dict(blames or {})     # (sha, path) -> porcelain text
        self.remote = remote
        self.raise_on = set(raise_on)        # subcommands that raise TimeoutExpired
        self.calls: list[list[str]] = []

    def __call__(self, args, *, cwd, timeout):
        self.calls.append(list(args))
        # skip leading `-c key=val` global options to find the subcommand (git accepts
        # `git -c core.quotePath=false show ...`)
        i = 0
        while i < len(args) and args[i] == "-c":
            i += 2
        rest = args[i:]
        sub = rest[0] if rest else ""
        if sub in self.raise_on:
            raise subprocess.TimeoutExpired(cmd=["git", *args], timeout=timeout)
        if sub == "log":
            return _Run(stdout=self.log)
        if sub == "remote":
            return _Run(stdout=self.remote)
        if sub == "show":
            sha = rest[1]
            if sha not in self.shows:
                return _Run(returncode=128, stderr="bad object")
            return _Run(stdout=self.shows[sha])
        if sub == "blame":
            sha = rest[1]
            # path is the token after the standalone '--'
            path = rest[rest.index("--") + 1] if "--" in rest else ""
            return _Run(stdout=self.blames.get((sha, path), ""))
        return _Run(returncode=1, stderr="unrouted")


def _log_record(sha, parents, ts, subject, trailers="", body="") -> str:
    """One \x1e-delimited, \x1f-fielded git-log record as the adapter formats it:
    sha US parents US ts US subject US trailers US body."""
    return RS + US.join([sha, parents, str(ts), subject, trailers, body])


def _src(runner, **kw) -> GitCliSource:
    kw.setdefault("repo_remote", "git@github.com:acme/widget.git")
    return GitCliSource("/repo", runner=runner, **kw)


# ── conformance ──────────────────────────────────────────────────────────────────
def test_conforms_to_outcome_source_port():
    """The REAL adapter satisfies the port (not just the Fake) — runtime_checkable
    isinstance, the swap-seam contract."""
    assert isinstance(_src(FakeRunner()), OutcomeSource)


# ── classification ───────────────────────────────────────────────────────────────
def test_poll_classifies_plain_commit():
    log = _log_record("aaa1", "p0", 1000, "add feature")
    show = "+++ b/feature.py\n@@ -0,0 +1,2 @@\n+a\n+b\n"
    poll = _src(FakeRunner(log=log, shows={"aaa1": show})).poll()
    assert poll.ok and len(poll.commits) == 1
    c = poll.commits[0]
    assert c.sha == "aaa1" and c.kind == "commit" and c.ts == 1000
    assert c.files_touched == ("feature.py",)
    assert c.repo_remote == "git@github.com:acme/widget.git"


def test_poll_parses_introduced_lines_from_hunks():
    """★ introduced_lines = the POST-image added range `+c,d` → {c..c+d-1}; the
    pre-image side `-a,b` is NOT introduced (it is the blame target). This is the
    pinned RULE-2 target: parsing the pre-image side here makes the set wrong."""
    log = _log_record("bbb2", "p0", 2000, "edit")
    show = "+++ b/mod.py\n@@ -10,1 +5,3 @@\n-old\n+x\n+y\n+z\n"
    c = _src(FakeRunner(log=log, shows={"bbb2": show})).poll().commits[0]
    assert set(c.introduced_lines) == {5, 6, 7}          # post-image +5,3
    assert 10 not in c.introduced_lines                  # pre-image -10,1 excluded


def test_poll_classifies_bugfix_by_pattern():
    log = _log_record("ccc3", "p0", 3000, "fix: null deref")
    show = "+++ b/a.py\n@@ -3,1 +3,1 @@\n-bad\n+good\n"
    c = _src(FakeRunner(log=log, shows={"ccc3": show},
                        blames={("ccc3^", "a.py"): "deadbeef 3 3 1\n"})).poll().commits[0]
    assert c.kind == "bugfix"


def test_poll_classifies_revert_and_extracts_target():
    body = "This reverts commit aaa1deadbeef.\n"
    log = _log_record("ddd4", "p0", 4000, 'Revert "add feature"', body=body)
    show = "+++ b/feature.py\n@@ -1,2 +0,0 @@\n-a\n-b\n"
    c = _src(FakeRunner(log=log, shows={"ddd4": show})).poll().commits[0]
    assert c.kind == "revert"
    assert c.reverts == "aaa1deadbeef"


def test_poll_classifies_merge_by_parents():
    log = _log_record("eee5", "p0 p1", 5000, "Merge branch 'topic'")
    show = "+++ b/x.py\n@@ -0,0 +1,1 @@\n+merged\n"
    c = _src(FakeRunner(log=log, shows={"eee5": show})).poll().commits[0]
    assert c.kind == "merge"


def test_revert_precedence_over_bugfix_pattern():
    """A subject matching BOTH 'fix:' and a revert body classifies as revert (the
    explicit reverts-target is the higher-precision clawback path)."""
    body = "This reverts commit feed0000.\n"
    log = _log_record("f6", "p0", 6000, "fix: revert the bad change", body=body)
    show = "+++ b/a.py\n@@ -1,1 +0,0 @@\n-bad\n"
    c = _src(FakeRunner(log=log, shows={"f6": show})).poll().commits[0]
    assert c.kind == "revert" and c.reverts == "feed0000"


# ── trailers ─────────────────────────────────────────────────────────────────────
def test_poll_parses_hive_trace_trailer():
    log = _log_record("g7", "p0", 7000, "work", trailers="T-alpha T-beta")
    show = "+++ b/a.py\n@@ -0,0 +1,1 @@\n+x\n"
    c = _src(FakeRunner(log=log, shows={"g7": show})).poll().commits[0]
    assert c.trailer_traces == ("T-alpha", "T-beta")


def test_poll_no_trailer_is_empty_tuple():
    log = _log_record("h8", "p0", 8000, "work", trailers="")
    show = "+++ b/a.py\n@@ -0,0 +1,1 @@\n+x\n"
    c = _src(FakeRunner(log=log, shows={"h8": show})).poll().commits[0]
    assert c.trailer_traces == ()


# ── blame / touched_blame ────────────────────────────────────────────────────────
def test_poll_bugfix_attaches_touched_blame():
    """A bugfix's pre-image touched lines are blamed at the PARENT; touched_blame =
    the ORIGINAL line numbers — the same coord space as a credited commit's
    introduced_lines, so the joiner's overlap test is meaningful."""
    log = _log_record("i9", "p0", 9000, "fix: correct calc")
    # pre-image -42,2 (lines 42,43 in the parent) are the blame targets
    show = ("--- a/calc.py\n+++ b/calc.py\n"
            "@@ -42,2 +42,2 @@\n-wrong1\n-wrong2\n+right1\n+right2\n")
    # blame of parent lines 42-43 → they were originally introduced at lines 7,8
    # (porcelain headers: <sha≥7hex> <origline> <finalline> [count])
    blame = "abc1230 7 42 1\n\tcode7\nabc1230 8 43 1\n\tcode8\n"
    c = _src(FakeRunner(log=log, shows={"i9": show},
                        blames={("i9^", "calc.py"): blame})).poll().commits[0]
    assert c.kind == "bugfix"
    assert set(c.touched_blame) == {7, 8}                 # original introduced lines


def test_pure_addition_bugfix_has_empty_touched_blame():
    """A bugfix that only ADDS lines (no pre-image) overlaps nothing — touched_blame
    empty, so it cannot clawback a prior commit (correct: it touched no old line)."""
    log = _log_record("j10", "p0", 10000, "fix: add guard")
    show = "+++ b/a.py\n@@ -0,0 +5,1 @@\n+guard\n"
    c = _src(FakeRunner(log=log, shows={"j10": show})).poll().commits[0]
    assert c.kind == "bugfix"
    assert c.touched_blame == frozenset()


# ── audit regressions: diff-content vs file-header, C-quoted paths, cursor hold ──
def test_removed_dashdash_content_not_mistaken_for_file_header():
    """[audit P18-001] A removed line whose CONTENT starts with '-- ' renders as
    '--- <content>' in a -u0 diff; an added line starting with '++ ' renders as
    '+++ <content>'. These are HUNK CONTENT (they appear AFTER the first @@), NOT
    new file headers — the parser must anchor headers on `diff --git` / pre-@@
    position, or old_path/new_path get corrupted and touched_blame is lost."""
    log = _log_record("sq1", "p0", 1, "fix: correct sql")
    # real `git show --unified=0` shape: header block, then hunks whose content lines
    # happen to begin with '-- ' (a SQL comment) and '++ '.
    show = (
        "diff --git a/q.sql b/q.sql\n"
        "index 111..222 100644\n"
        "--- a/q.sql\n"
        "+++ b/q.sql\n"
        "@@ -2,1 +2,1 @@\n"
        "--- a SQL comment\n"            # CONTENT (removed line '-' + '-- a SQL comment')
        "+++ a SQL comment edited\n"     # CONTENT (added line '+' + '++ a SQL comment...')
        "@@ -4,1 +4,1 @@\n"
        "-line4 buggy\n"
        "+line4 fixed\n"
    )
    blame = "abc1230 4 4 1\n\tline4 buggy\nabc1230 2 2 1\n\tcomment\n"
    runner = FakeRunner(log=log, shows={"sq1": show},
                        blames={("sq1^", "q.sql"): blame})
    c = _src(runner).poll().commits[0]
    assert c.kind == "bugfix"
    assert c.files_touched == ("q.sql",)                 # NOT polluted by 'a SQL comment'
    # the real removed lines are parent lines 2 and 4 → blamed; the '--- a SQL comment'
    # content line must NOT have overwritten old_path with garbage (which would drop blame)
    assert set(c.touched_blame) == {2, 4}


def test_c_quoted_unicode_path_decoded():
    """[audit P1.8-DIFF-01] git C-quotes a non-ASCII path as `"b/caf\\303\\251.py"`
    (default core.quotePath). _diff_path must strip the QUOTES first, then the b/
    prefix, then C-unescape → 'café.py' (not 'b/caf\\303\\251.py'), so files_touched
    and the blame path arg are correct."""
    log = _log_record("uni1", "p0", 1, "fix: unicode file")
    show = (
        'diff --git "a/caf\\303\\251.py" "b/caf\\303\\251.py"\n'
        '--- "a/caf\\303\\251.py"\n'
        '+++ "b/caf\\303\\251.py"\n'
        "@@ -2,1 +2,1 @@\n-bad\n+good\n"
    )
    blame = "abc1230 2 2 1\n\tcode\n"
    runner = FakeRunner(log=log, shows={"uni1": show},
                        blames={("uni1^", "café.py"): blame})
    c = _src(runner).poll().commits[0]
    assert c.files_touched == ("café.py",)               # decoded, prefix stripped
    assert set(c.touched_blame) == {2}                   # blame ran on the decoded path


def test_show_disables_quotepath():
    """The `git show` invocation passes `-c core.quotePath=false` so the common
    unicode-filename case arrives literal (the realistic path; the C-quote handler is
    the belt for the residual tab/quote/newline names)."""
    log = _log_record("qp1", "p0", 1, "edit")
    runner = FakeRunner(log=log, shows={"qp1": "+++ b/a.py\n@@ -0,0 +1,1 @@\n+x\n"})
    runner.shows["qp1"] = "--- a/a.py\n+++ b/a.py\n@@ -0,0 +1,1 @@\n+x\n"
    _src(runner).poll()
    show_args = next(a for a in runner.calls if "show" in a)
    assert "core.quotePath=false" in " ".join(show_args)


def test_cursor_held_when_a_commit_is_dropped():
    """[audit P18-CURSOR-01] A per-commit show failure drops the commit; the cursor
    must NOT advance past it (a transient failure would otherwise lose the commit
    forever via the next cursor..HEAD range). On ANY drop the cursor holds so the
    whole range is retried next tick (the producer's PK-upsert makes re-emit safe)."""
    log = _log_record("D2new", "p", 2, "newest ok") + _log_record("D1old", "p", 1, "older fails-show")
    # D1old has no canned show → rc 128 → dropped; D2new builds fine
    runner = FakeRunner(log=log, shows={"D2new": "+++ b/a.py\n@@ -0,0 +1,1 @@\n+x\n"})
    src = _src(runner, since_sha="seed")
    poll = src.poll()
    assert poll.ok is True
    assert [c.sha for c in poll.commits] == ["D2new"]    # only the buildable one
    assert src.cursor == "seed"                          # HELD — D1old will be retried


def test_cursor_advances_when_all_commits_build():
    """The complement: with zero drops the cursor advances to the newest sha."""
    log = _log_record("A2", "p", 2, "two") + _log_record("A1", "p", 1, "one")
    runner = FakeRunner(log=log,
                        shows={"A2": "+++ b/a.py\n@@ -0,0 +1,1 @@\n+x\n",
                               "A1": "+++ b/b.py\n@@ -0,0 +1,1 @@\n+y\n"})
    src = _src(runner, since_sha="seed")
    src.poll()
    assert src.cursor == "A2"


# ── fail-closed (poll never raises) ──────────────────────────────────────────────
def test_poll_never_raises_on_log_failure():
    runner = FakeRunner(raise_on={"log"})                 # git log raises TimeoutExpired
    poll = _src(runner).poll()
    assert poll.ok is False and poll.error
    assert poll.commits == ()


def test_poll_never_raises_on_missing_repo():
    class _Missing:
        def __call__(self, args, *, cwd, timeout):
            raise FileNotFoundError("no such repo")
    poll = _src(_Missing()).poll()
    assert poll.ok is False and poll.error


def test_log_nonzero_returncode_is_not_ok():
    runner = FakeRunner(log="")
    # force a non-zero rc out of the log subcommand
    def rc_runner(args, *, cwd, timeout):
        if args[0] == "log":
            return _Run(returncode=128, stderr="fatal: not a git repository")
        return _Run()
    poll = _src(rc_runner).poll()
    assert poll.ok is False and poll.error


def test_per_commit_show_failure_skips_commit_not_poll():
    """One commit's `git show` failing drops THAT commit but the poll stays ok and
    the other commits survive (per-commit boundary, not whole-poll)."""
    log = _log_record("k1", "p", 1, "good") + _log_record("k2", "p", 2, "bad-show")
    runner = FakeRunner(log=log, shows={"k1": "+++ b/a.py\n@@ -0,0 +1,1 @@\n+x\n"})
    # k2 has no canned show → returncode 128 → skipped
    poll = _src(runner).poll()
    assert poll.ok is True
    assert [c.sha for c in poll.commits] == ["k1"]


def test_blame_failure_yields_empty_touched_blame_not_crash():
    log = _log_record("l1", "p", 1, "fix: thing")
    show = "--- a/a.py\n+++ b/a.py\n@@ -3,1 +3,1 @@\n-bad\n+good\n"  # pre-image ⇒ blame attempted
    runner = FakeRunner(log=log, shows={"l1": show}, raise_on={"blame"})
    c = _src(runner).poll().commits[0]
    assert c.kind == "bugfix" and c.touched_blame == frozenset()


# ── cursor ───────────────────────────────────────────────────────────────────────
def test_cursor_advances_to_newest_sha():
    """git log is newest-first; cursor becomes the newest (first) sha."""
    log = _log_record("new9", "p", 9, "newest") + _log_record("old1", "p", 1, "older")
    src = _src(FakeRunner(log=log,
                          shows={"new9": "+++ b/a.py\n@@ -0,0 +1,1 @@\n+n\n",
                                 "old1": "+++ b/a.py\n@@ -0,0 +1,1 @@\n+o\n"}))
    assert src.cursor is None
    src.poll()
    assert src.cursor == "new9"


def test_cursor_not_advanced_on_failure():
    src = _src(FakeRunner(raise_on={"log"}), since_sha="seed0")
    assert src.cursor == "seed0"
    src.poll()
    assert src.cursor == "seed0"                          # unchanged on failure


def test_second_poll_uses_cursor_range():
    """After a successful poll the next `git log` is scoped to cursor..HEAD (only new
    commits), not a full re-scan."""
    log1 = _log_record("c1", "p", 1, "one")
    runner = FakeRunner(log=log1, shows={"c1": "+++ b/a.py\n@@ -0,0 +1,1 @@\n+x\n"})
    src = _src(runner)
    src.poll()
    runner.log = ""                                       # no new commits second time
    runner.calls.clear()
    src.poll()
    log_args = next(a for a in runner.calls if a[0] == "log")
    assert any("c1..HEAD" == tok for tok in log_args)


def test_empty_log_returns_ok_empty():
    poll = _src(FakeRunner(log="")).poll()
    assert poll.ok is True and poll.commits == ()


# ── secret-safety ────────────────────────────────────────────────────────────────
def test_no_commit_body_or_subject_in_logs(caplog):
    """§6/§9: WARN/INFO log lines carry only sha/repo/counts — NEVER a commit body or
    subject. A token hidden in the message must not surface in any log record."""
    token = "pypi-AgEISECRETvalue12345"
    log = _log_record("m1", "p", 1, f"fix: {token}", body=f"leak {token} here")
    show = "+++ b/a.py\n@@ -3,1 +3,1 @@\n-x\n+y\n"
    runner = FakeRunner(log=log, shows={"m1": show}, raise_on={"blame"})  # blame WARNs
    with caplog.at_level(logging.DEBUG, logger="hive.source"):
        _src(runner).poll()
    blob = " ".join(r.getMessage() for r in caplog.records)
    assert token not in blob


def test_repo_remote_preattached_to_each_commit():
    log = _log_record("n1", "p", 1, "a") + _log_record("n2", "p", 2, "b")
    runner = FakeRunner(log=log,
                        shows={"n1": "+++ b/a.py\n@@ -0,0 +1,1 @@\n+x\n",
                               "n2": "+++ b/b.py\n@@ -0,0 +1,1 @@\n+y\n"})
    poll = _src(runner, repo_remote="REMOTE").poll()
    assert all(c.repo_remote == "REMOTE" for c in poll.commits)
    assert poll.repo_remote == "REMOTE"


# ════════════════════════════════════════════════════════════════════════════════
#  REAL tmp-git integration (the §8 mandate). Skipped only if git is unavailable.
# ════════════════════════════════════════════════════════════════════════════════
_GIT = shutil.which("git")
pytestmark_real = pytest.mark.skipif(_GIT is None, reason="git not installed")


def _git(repo, *args, env=None):
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True, env=env)


def _commit(repo, msg, env):
    _git(repo, "add", "-A")
    subprocess.run(["git", "-C", str(repo), "commit", "-m", msg],
                   check=True, capture_output=True, text=True, env=env)
    out = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                         check=True, capture_output=True, text=True, env=env)
    return out.stdout.strip()


@pytest.fixture
def git_repo(tmp_path):
    if _GIT is None:
        pytest.skip("git not installed")
    repo = tmp_path / "r"
    repo.mkdir()
    env = {
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
        "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
        "HOME": str(tmp_path), "PATH": __import__("os").environ.get("PATH", ""),
    }
    _git(repo, "init", "-q", "-b", "main", env=env)
    return repo, env


@pytestmark_real
def test_real_repo_plain_commit_introduced_lines(git_repo):
    repo, env = git_repo
    (repo / "calc.py").write_text("def f():\n    return 1\n")
    sha = _commit(repo, "add calc", env)
    poll = GitCliSource(str(repo), repo_remote="R").poll()
    assert poll.ok
    c = next(c for c in poll.commits if c.sha == sha)
    assert c.kind == "commit"
    assert c.files_touched == ("calc.py",)
    assert set(c.introduced_lines) == {1, 2}             # two added lines


@pytestmark_real
def test_real_repo_bugfix_blame_overlaps_original(git_repo):
    """The authoritative blame round-trip: an original commit introduces lines; a
    later 'fix:' commit edits one of them; GitCliSource's touched_blame (from real
    `git blame`) overlaps the ORIGINAL commit's introduced_lines — so the BUILT
    OutcomeJoiner claws it back. Proves the impure blame seam end-to-end."""
    repo, env = git_repo
    (repo / "calc.py").write_text("a = 1\nb = 2\nc = 3\n")
    orig = _commit(repo, "feat: add calc", env)
    poll0 = GitCliSource(str(repo), repo_remote="R").poll()
    orig_fact = next(c for c in poll0.commits if c.sha == orig)
    assert set(orig_fact.introduced_lines) == {1, 2, 3}

    # edit line 2 only, with a bugfix subject
    (repo / "calc.py").write_text("a = 1\nb = 22\nc = 3\n")
    fix = _commit(repo, "fix: wrong b", env)
    fix_fact = next(c for c in GitCliSource(str(repo), repo_remote="R").poll().commits
                    if c.sha == fix)
    assert fix_fact.kind == "bugfix"
    assert 2 in fix_fact.touched_blame                   # blames back to original line 2
    # …and it overlaps the original commit's introduced_lines (the clawback trigger)
    assert fix_fact.touched_blame & orig_fact.introduced_lines == {2}

    # plug BOTH facts into the BUILT pure joiner → a clawback emit fires on `orig`
    joiner = OutcomeJoiner(assoc_window_s=1800, settle_days=7, provisional_reward=0.2,
                           clawback_reward=-1.0, require_stamp=False, assoc_epsilon=0.1,
                           rng=random.Random(0))
    live = joiner.associate([orig_fact], RecallWindow(items=(WindowTrace("T", orig_fact.ts),)))
    claws = joiner.clawback(live, [fix_fact], now=fix_fact.ts + 1)
    assert [e.reward_sign for e in claws] == [-1]
    assert claws[0].task_ref == orig


@pytestmark_real
def test_real_repo_coincidental_same_file_no_overlap_no_clawback(git_repo):
    """GUARD a (false-positive blame): a later bugfix edits a DIFFERENT line of the
    same file → touched_blame does NOT overlap the original introduced_lines → the
    joiner does NOT clawback. Same-file-alone never claws back (Decision B)."""
    repo, env = git_repo
    (repo / "m.py").write_text("L1\nL2\nL3\nL4\n")
    orig = _commit(repo, "feat: seed", env)
    orig_fact = next(c for c in GitCliSource(str(repo), repo_remote="R").poll().commits
                     if c.sha == orig)
    # append a brand-new line and "fix" it — touches a line the original never introduced
    (repo / "m.py").write_text("L1\nL2\nL3\nL4\nL5\n")
    _commit(repo, "feat: grow", env)                     # introduces line 5
    (repo / "m.py").write_text("L1\nL2\nL3\nL4\nL5fixed\n")
    fix = _commit(repo, "fix: tweak L5", env)
    fix_fact = next(c for c in GitCliSource(str(repo), repo_remote="R").poll().commits
                    if c.sha == fix)
    assert fix_fact.kind == "bugfix"
    assert not (fix_fact.touched_blame & orig_fact.introduced_lines)
    joiner = OutcomeJoiner(assoc_window_s=1800, settle_days=7, provisional_reward=0.2,
                           clawback_reward=-1.0, require_stamp=False, assoc_epsilon=0.1,
                           rng=random.Random(0))
    live = joiner.associate([orig_fact], RecallWindow(items=(WindowTrace("T", orig_fact.ts),)))
    assert joiner.clawback(live, [fix_fact], now=fix_fact.ts + 1) == []


@pytestmark_real
def test_real_repo_revert_extracts_target(git_repo):
    repo, env = git_repo
    (repo / "z.py").write_text("x = 1\n")
    orig = _commit(repo, "feat: z", env)
    subprocess.run(["git", "-C", str(repo), "revert", "--no-edit", orig],
                   check=True, capture_output=True, text=True, env=env)
    poll = GitCliSource(str(repo), repo_remote="R").poll()
    rev = next(c for c in poll.commits if c.kind == "revert")
    assert rev.reverts == orig


@pytestmark_real
def test_real_repo_hive_trace_trailer(git_repo):
    repo, env = git_repo
    (repo / "a.py").write_text("v = 1\n")
    _git(repo, "add", "-A")
    subprocess.run(["git", "-C", str(repo), "commit",
                    "-m", "work\n\nHive-Trace: T-1 T-2"],
                   check=True, capture_output=True, text=True, env=env)
    c = GitCliSource(str(repo), repo_remote="R").poll().commits[0]
    assert c.trailer_traces == ("T-1", "T-2")


@pytestmark_real
def test_real_repo_squash_merge_resolves_single_commit(git_repo):
    """Squash-survival (§8 / §6.1.6 guard b): a 2-commit topic branch squash-merged
    to main appears as ONE first-parent commit whose introduced_lines are the COMBINED
    content — the join key + blame target survive the squash (no dropped trailer)."""
    repo, env = git_repo
    (repo / "f.py").write_text("base\n")
    _commit(repo, "base", env)
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "topic"],
                   check=True, capture_output=True, text=True, env=env)
    (repo / "f.py").write_text("base\nx\n")
    _commit(repo, "topic1", env)
    (repo / "f.py").write_text("base\nx\ny\n")
    _commit(repo, "topic2", env)
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "main"],
                   check=True, capture_output=True, text=True, env=env)
    subprocess.run(["git", "-C", str(repo), "merge", "--squash", "topic"],
                   check=True, capture_output=True, text=True, env=env)
    squash = _commit(repo, "squash topic", env)

    src = GitCliSource(str(repo), repo_remote="R")
    poll = src.poll()
    # the topic1/topic2 commits are NOT on the first-parent line; only base + squash are
    shas = [c.sha for c in poll.commits]
    assert squash in shas
    sq = next(c for c in poll.commits if c.sha == squash)
    assert sq.kind == "commit"                           # squash is a normal single-parent commit
    assert set(sq.introduced_lines) == {2, 3}            # combined x,y in one fact
    assert src.cursor == squash                          # newest first-parent sha is the watermark


@pytestmark_real
def test_real_repo_no_ff_merge_classified_and_first_parent_diff(git_repo):
    """A --no-ff merge is kind='merge' (2 parents) and its first-parent diff is parsed
    (introduced_lines non-empty) — merges are credited like any associated commit."""
    repo, env = git_repo
    (repo / "f.py").write_text("base\n")
    _commit(repo, "base", env)
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "side"],
                   check=True, capture_output=True, text=True, env=env)
    (repo / "g.py").write_text("a\nb\nc\n")
    _commit(repo, "side feature", env)
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "main"],
                   check=True, capture_output=True, text=True, env=env)
    subprocess.run(["git", "-C", str(repo), "merge", "--no-ff", "side",
                    "-m", "Merge branch side"],
                   check=True, capture_output=True, text=True, env=env)
    merge_sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                               check=True, capture_output=True, text=True,
                               env=env).stdout.strip()
    mc = next(c for c in GitCliSource(str(repo), repo_remote="R").poll().commits
              if c.sha == merge_sha)
    assert mc.kind == "merge"
    assert set(mc.introduced_lines) == {1, 2, 3}         # g.py via the first-parent diff


@pytestmark_real
def test_real_repo_sql_comment_content_not_corrupting_blame(git_repo):
    """[audit P18-001, real git] A file with a leading-'--' SQL comment: a bugfix that
    edits a DATA line (not the comment) must still blame back correctly. Real `git show
    --unified=0` renders the unrelated removed comment line as '--- ...' content; the
    parser must not let it corrupt old_path and drop the real blame target."""
    repo, env = git_repo
    (repo / "q.sql").write_text("-- header comment\nSELECT 1;\nSELECT 2;\nSELECT 3;\n")
    orig = _commit(repo, "feat: add query", env)
    orig_fact = next(c for c in GitCliSource(str(repo), repo_remote="R").poll().commits
                     if c.sha == orig)
    assert orig_fact.files_touched == ("q.sql",)
    assert set(orig_fact.introduced_lines) == {1, 2, 3, 4}

    # edit BOTH the comment (line 1) and a data line (line 3) in one bugfix
    (repo / "q.sql").write_text("-- header comment v2\nSELECT 1;\nSELECT 22;\nSELECT 3;\n")
    fix = _commit(repo, "fix: wrong value", env)
    fix_fact = next(c for c in GitCliSource(str(repo), repo_remote="R").poll().commits
                    if c.sha == fix)
    assert fix_fact.kind == "bugfix"
    assert fix_fact.files_touched == ("q.sql",)          # not polluted by comment content
    # both edited lines (1 and 3) blame back to the original; the overlap with the
    # original commit's introduced_lines is non-empty ⇒ the joiner claws back
    assert {1, 3} <= set(fix_fact.touched_blame)
    assert fix_fact.touched_blame & orig_fact.introduced_lines


@pytestmark_real
def test_default_runner_targets_repo_and_passes_timeout(git_repo):
    """The real `_default_runner` invokes `git -C <repo>` and honors a timeout
    kwarg (smoke: a trivial `remote` call returns a CompletedProcess)."""
    repo, env = git_repo
    cp = _default_runner(["rev-parse", "--is-inside-work-tree"], cwd=str(repo), timeout=10)
    assert cp.returncode == 0 and cp.stdout.strip() == "true"
