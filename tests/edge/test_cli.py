"""Engine-verb unit tests (plan 8a) over real tmp trees.

The agent verbs (mint / verify) are FAIL-OPEN: any fault yields
the empty/again output and ALWAYS exit 0. The operator verbs (graph) keep
exit-coded semantics. These tests are the contract; the code is written to
satisfy them.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import hive.combdrift as combdrift
import pytest

from hive.edge import cli
from hive.edge.cli import _is_code_shaped_path, _split_anchor, main

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")


def _run(argv, capsys):
    """Invoke the CLI, returning (exit_code, parsed_stdout_json_or_raw, stderr)."""
    code = main(argv)
    cap = capsys.readouterr()
    try:
        parsed = json.loads(cap.out)
    except json.JSONDecodeError:
        parsed = cap.out
    return code, parsed, cap.err


# ── _split_anchor: ONE tokenizer for mint AND verify ────────────────────────────


def test_split_anchor_single_colon():
    assert _split_anchor("a/b.py:sym") == ("a/b.py", "sym")


def test_split_anchor_double_colon_wins():
    assert _split_anchor("a/b.ts::C.m") == ("a/b.ts", "C.m")


def test_split_anchor_no_colon_is_existence_only():
    assert _split_anchor("a/b.py") == ("a/b.py", None)


def test_split_anchor_line_number_is_existence_only():
    # A purely numeric "symbol" is a line number, not a callable -> existence-only.
    assert _split_anchor("a/b.py:42") == ("a/b.py", None)


def test_split_anchor_empty_symbol_is_existence_only():
    assert _split_anchor("a/b.py:") == ("a/b.py", None)


# ── mint (agent verb, always exit 0) ────────────────────────────────────────────


def test_mint_current_callable(py_repo, capsys):
    # py_repo is a single-source-root tree (all parsed source under pkg/), so the union
    # carries BOTH fingerprints — the path-dialect bridge resolves the anchor for matrix too.
    code, out, _ = _run(
        ["mint", "--repo", str(py_repo), "--anchor", "pkg/f.py:foo"], capsys
    )
    assert code == 0
    # // marker: dropping the combdrift/fp key (bare token) is mutation 1.
    assert set(out.keys()) == {"combdrift/fp", "matrix/subgraph_fp"}
    assert out["combdrift/fp"].startswith("combdrift-fp/")


def test_mint_no_symbol_is_empty(py_repo, capsys):
    code, out, _ = _run(
        ["mint", "--repo", str(py_repo), "--anchor", "pkg/f.py"], capsys
    )
    assert code == 0 and out == {}


def test_mint_missing_file_is_empty(py_repo, capsys):
    code, out, _ = _run(
        ["mint", "--repo", str(py_repo), "--anchor", "pkg/nope.py:foo"], capsys
    )
    assert code == 0 and out == {}


def test_mint_missing_symbol_is_empty(py_repo, capsys):
    code, out, _ = _run(
        ["mint", "--repo", str(py_repo), "--anchor", "pkg/f.py:ghost"], capsys
    )
    assert code == 0 and out == {}


def test_mint_prose_anchor_is_empty(py_repo, capsys):
    code, out, _ = _run(
        ["mint", "--repo", str(py_repo), "--anchor", "the auth refresh flow"], capsys
    )
    assert code == 0 and out == {}


def test_mint_failopen_on_exception(py_repo, capsys, monkeypatch):
    # A broken engine must degrade to {} + exit 0, and be operator-visible (one line).
    def boom(*a, **k):
        raise RuntimeError("engine broke")

    monkeypatch.setattr(combdrift, "fingerprint_anchor", boom)
    code, out, err = _run(
        ["mint", "--repo", str(py_repo), "--anchor", "pkg/f.py:foo"], capsys
    )
    assert code == 0 and out == {}  # // marker: nonzero-on-failure is mutation 4.
    assert err.strip()  # broken install is visible, not silent


# ── mint --branch-scope (the opt-in, set-valued branch relevance tag) ───────────


@pytest.fixture
def git_branch_repo(git_repo):
    """Builder: git_branch_repo(branch) -> a committed git repo checked out on `branch`,
    carrying one resolvable callable pkg/f.py:foo(a, b)."""
    import subprocess

    def _make(branch: str = "feat-x") -> Path:
        repo = git_repo("branchrepo")
        (repo / "pkg").mkdir()
        (repo / "pkg" / "f.py").write_text(
            "def foo(a, b):\n    return a + b\n", encoding="utf-8"
        )
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "add f"], check=True)
        subprocess.run(["git", "-C", str(repo), "checkout", "-qb", branch], check=True)
        return repo

    return _make


@requires_git
def test_mint_branch_scope_current_branch_tags_the_map(git_branch_repo, capsys):
    # Bare --branch-scope resolves the CURRENT branch and merges the versioned token.
    repo = git_branch_repo("feat-x")
    code, out, _ = _run(
        ["mint", "--repo", str(repo), "--anchor", "pkg/f.py:foo", "--branch-scope"],
        capsys,
    )
    assert code == 0
    assert out["git/branches"] == "git-branches/1:feat-x"
    assert out["combdrift/fp"].startswith("combdrift-fp/")  # the fp union still rides


@requires_git
def test_mint_branch_scope_explicit_names_sorted_deduped(git_branch_repo, capsys):
    # Explicit names: stripped, deduped, SORTED into one space-joined set body.
    repo = git_branch_repo("feat-x")
    code, out, _ = _run(
        [
            "mint",
            "--repo",
            str(repo),
            "--anchor",
            "pkg/f.py:foo",
            "--branch-scope",
            "release-1.x",
            "master",
            " master ",
        ],
        capsys,
    )
    assert code == 0
    assert out["git/branches"] == "git-branches/1:master release-1.x"


@requires_git
def test_mint_without_branch_scope_is_byte_identical(git_branch_repo, capsys):
    # No opt-in ⇒ the FULL printed map is byte-identical to the pre-tag build: the two
    # fingerprint keys and NOTHING else (never a git/branches key).
    repo = git_branch_repo("feat-x")
    code, out, _ = _run(
        ["mint", "--repo", str(repo), "--anchor", "pkg/f.py:foo"], capsys
    )
    assert code == 0
    assert set(out.keys()) == {"combdrift/fp", "matrix/subgraph_fp"}


@requires_git
def test_mint_branch_scope_detached_head_omits_key_exit_zero(git_branch_repo, capsys):
    # Detached HEAD resolves no branch name: fail-open — the key is OMITTED, mint
    # still exits 0 and prints the ordinary fp union.
    import subprocess

    repo = git_branch_repo("feat-x")
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "--detach"], check=True)
    code, out, _ = _run(
        ["mint", "--repo", str(repo), "--anchor", "pkg/f.py:foo", "--branch-scope"],
        capsys,
    )
    assert code == 0
    assert "git/branches" not in out
    assert set(out.keys()) == {"combdrift/fp", "matrix/subgraph_fp"}


def test_mint_branch_scope_outside_repo_omits_key(py_repo, capsys):
    # py_repo is a plain (non-git) tree: --repo pins it, but there is no branch to
    # resolve — the key is omitted and mint stays exit 0 (fail-open).
    code, out, _ = _run(
        ["mint", "--repo", str(py_repo), "--anchor", "pkg/f.py:foo", "--branch-scope"],
        capsys,
    )
    assert code == 0
    assert "git/branches" not in out


# ── the branch token helpers (unit layer) ───────────────────────────────────────


def test_branches_token_strips_dedupes_sorts():
    assert cli._branches_token(["b", "a", " b ", "a"]) == "git-branches/1:a b"


def test_branches_token_empty_is_none():
    assert cli._branches_token([]) is None
    assert cli._branches_token(["  ", ""]) is None


def test_branch_scope_round_trips_the_token():
    token = cli._branches_token(["feat-x", "master"])
    assert cli._branch_scope(token) == frozenset({"feat-x", "master"})


@pytest.mark.parametrize(
    "token",
    [
        "garbage",  # no recognized prefix
        "git-branches/1:",  # empty body
        "git-branches/999:feat-x",  # future version (reader is current-only)
        "git-branches/:feat-x",  # empty version segment
        "combdrift-fp/1:whatever",  # a foreign key's token
    ],
)
def test_branch_scope_unreadable_tokens_are_none(token):
    # marker: parsing (comparing) across versions here is a mutation — an unreadable
    # token is INCOMPARABLE and must route to None (silence), never a false scope.
    assert cli._branch_scope(token) is None


def test_branch_scope_reader_ahead_of_stored_token_is_none(monkeypatch):
    # BUG-037 idiom: a stored v1 token under a BUMPED reader is incomparable -> None.
    token = cli._branches_token(["feat-x"])
    monkeypatch.setattr(cli, "BRANCHES_VERSION", "2")
    assert cli._branch_scope(token) is None


@requires_git
def test_current_branch_named_detached_and_fault(git_repo, tmp_path):
    import subprocess

    repo = git_repo("cbrepo")
    subprocess.run(["git", "-C", str(repo), "checkout", "-qb", "feat-y"], check=True)
    assert cli._current_branch(str(repo)) == "feat-y"
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "--detach"], check=True)
    assert cli._current_branch(str(repo)) is None  # literal "HEAD" ⇒ detached ⇒ None
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    assert cli._current_branch(str(plain)) is None  # git fault ⇒ None
    assert cli._current_branch(None) is None


# ── verify (agent verb, always exit 0) ──────────────────────────────────────────


def test_verify_live_is_current(py_repo, capsys):
    fp = combdrift.fingerprint_anchor(str(py_repo), combdrift.Anchor("pkg/f.py", "foo"))
    code, out, _ = _run(
        ["verify", "--repo", str(py_repo), "--anchor", "pkg/f.py:foo", "--fp", fp],
        capsys,
    )
    assert code == 0
    assert out["verdict"] == "current" and out["reason"] == "ok"
    assert "remediation" not in out and "delta" not in out


def test_verify_removed_symbol_is_stale(py_repo, capsys):
    (py_repo / "pkg" / "f.py").write_text(
        "def other():\n    return 1\n", encoding="utf-8"
    )
    code, out, _ = _run(
        ["verify", "--repo", str(py_repo), "--anchor", "pkg/f.py:foo"], capsys
    )
    assert code == 0
    # // marker: reporting stale-on-unverifiable is mutation 2 (this must stay stale).
    assert out["verdict"] == "stale" and out["reason"] == "symbol_missing"
    assert "remediation" in out
    assert "delta" not in out  # symbol_missing carries no token diff


def test_verify_unsupported_ext_is_unverifiable(py_repo, capsys):
    (py_repo / "notes.md").write_text("# prose, not code\n", encoding="utf-8")
    code, out, _ = _run(
        ["verify", "--repo", str(py_repo), "--anchor", "notes.md:foo"], capsys
    )
    assert code == 0
    # // marker: mutation 2 widens suppression so this reads stale -> this test reds.
    assert out["verdict"] == "unverifiable" and out["reason"] == "unsupported_language"
    assert "remediation" not in out and "delta" not in out


def test_verify_path_outside_repo_is_unverifiable(py_repo, capsys):
    code, out, _ = _run(
        ["verify", "--repo", str(py_repo), "--anchor", "../../../etc/passwd:x"], capsys
    )
    assert code == 0
    assert out["verdict"] == "unverifiable" and out["reason"] == "path_outside_repo"
    assert "remediation" not in out


def test_verify_missing_code_file_stays_stale(py_repo, capsys):
    # GUARD: a real code path (.py) whose FILE is absent is genuinely stale — the code moved
    # past the memory. The prose reclassification below must NOT suppress this.
    code, out, _ = _run(
        ["verify", "--repo", str(py_repo), "--anchor", "pkg/gone.py:foo"], capsys
    )
    assert code == 0
    assert out["verdict"] == "stale" and out["reason"] == "file_missing"
    assert "remediation" in out


def test_verify_prose_anchor_is_unverifiable(py_repo, capsys):
    # A prose anchor ("the auth refresh flow") is not a code path (no extension); a missing such
    # path was never code, so it is unverifiable — NOT stale. Otherwise the post-recall hook would
    # relay a false retire-this-memory alarm on every prose-anchored recall hit.
    code, out, _ = _run(
        ["verify", "--repo", str(py_repo), "--anchor", "the auth refresh flow"], capsys
    )
    assert code == 0
    # // marker: dropping the file_missing->unverifiable reclassification is a mutation.
    assert out["verdict"] == "unverifiable" and out["reason"] == "not_a_code_anchor"
    assert "remediation" not in out and "delta" not in out


def test_verify_prose_anchor_with_colon_is_unverifiable(py_repo, capsys):
    # A colon does not make it code: "auth: refresh the token" splits to path "auth" (no
    # extension) -> unverifiable, not stale.
    code, out, _ = _run(
        ["verify", "--repo", str(py_repo), "--anchor", "auth: refresh the token"],
        capsys,
    )
    assert code == 0
    assert out["verdict"] == "unverifiable" and out["reason"] == "not_a_code_anchor"


@pytest.mark.parametrize(
    "anchor",
    [
        "the v2.0 rollout:refresh",  # a dot mid-prose -> splitext ext ".0 rollout"
        "the rollout v2.0:sym",  # a trailing version -> splitext ext ".0"
    ],
)
def test_verify_dotted_prose_anchor_is_unverifiable(py_repo, capsys, anchor):
    # A prose anchor that merely CONTAINS a dot is still prose, not a code path: a bare
    # "has any extension" check reads ".0 rollout"/".0" as a file extension and routes the
    # missing path to a false stale. The tightened classifier keeps it unverifiable, so the
    # post-recall hook raises no retire-this-memory alarm on a dotted prose anchor.
    code, out, _ = _run(["verify", "--repo", str(py_repo), "--anchor", anchor], capsys)
    assert code == 0
    assert out["verdict"] == "unverifiable" and out["reason"] == "not_a_code_anchor"
    assert "remediation" not in out and "delta" not in out


def test_is_code_shaped_path_rejects_prose_keeps_code():
    # The classifier that gates BOTH the verify reclassification and the mint prose gate:
    # prose (dotted, non-dotted, or ending in a filename-looking word) is NOT code-shaped,
    # while every real repo-relative code path is. Loosening it back to "has any extension"
    # reds this and the false-stale reclassification returns (the Law-7 marker's mutation).
    for prose in [
        "the v2.0 rollout",
        "the rollout v2.0",
        "the auth refresh flow",
        "update the readme.md",  # a trailing filename-word does not make prose code
        "auth",
    ]:
        assert not _is_code_shaped_path(prose), prose
    for code_path in ["mod.py", "pkg/sub/f.py", "src/main.rs", "a.py", "pkg/f.py"]:
        assert _is_code_shaped_path(code_path), code_path


def test_verify_breaking_drift_is_stale_with_delta(py_repo, capsys):
    fp = combdrift.fingerprint_anchor(
        str(py_repo), combdrift.Anchor("pkg/f.py", "foo")
    )  # foo(a, b)
    (py_repo / "pkg" / "f.py").write_text(
        "def foo(a):\n    return a\n", encoding="utf-8"
    )  # -> foo(a)
    code, out, _ = _run(
        ["verify", "--repo", str(py_repo), "--anchor", "pkg/f.py:foo", "--fp", fp],
        capsys,
    )
    assert code == 0
    # // marker: ignoring --fp (existence-only) is mutation 3 -> this reads current and reds.
    assert out["verdict"] == "stale" and out["reason"] == "signature_changed"
    assert out["delta"]["old_token"] == fp
    assert out["delta"]["new_token"].startswith("combdrift-fp/")
    assert out["delta"]["breaking"] is True


def test_verify_additive_drift_is_current_no_delta(py_repo, capsys):
    (py_repo / "pkg" / "f.py").write_text(
        "def foo(a):\n    return a\n", encoding="utf-8"
    )
    fp = combdrift.fingerprint_anchor(
        str(py_repo), combdrift.Anchor("pkg/f.py", "foo")
    )  # foo(a)
    (py_repo / "pkg" / "f.py").write_text(
        "def foo(a, b=1):\n    return a\n", encoding="utf-8"
    )
    code, out, _ = _run(
        ["verify", "--repo", str(py_repo), "--anchor", "pkg/f.py:foo", "--fp", fp],
        capsys,
    )
    assert code == 0
    # additive drift breaks no prior call -> current; current carries NO delta and NO remediation.
    assert out["verdict"] == "current"
    assert "delta" not in out and "remediation" not in out


def test_verify_stale_remediation_full_vocabulary(py_repo, capsys):
    (py_repo / "pkg" / "f.py").write_text(
        "def other():\n    return 1\n", encoding="utf-8"
    )
    _, out, _ = _run(
        ["verify", "--repo", str(py_repo), "--anchor", "pkg/f.py:foo"], capsys
    )
    rem = out["remediation"]
    # // marker: dropping the remediation block from a stale verdict is mutation 11.
    for token in (
        "hive_outcome",
        "hive_supersede",
        "hive_write(replaces=",
        "hive_prune",
        "BAD_VS_STALE",
        "never retire on your own judgment",
    ):
        assert token in rem, token
    # hive_flag is a memory-vs-memory tool (two episode ids) — NOT part of a single-anchor menu.
    assert "hive_flag" not in rem


def test_verify_failopen_on_exception(py_repo, capsys, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("verify broke")

    monkeypatch.setattr(combdrift, "verify_record", boom)
    code, out, err = _run(
        ["verify", "--repo", str(py_repo), "--anchor", "pkg/f.py:foo"], capsys
    )
    assert code == 0  # // marker: nonzero-on-failure is mutation 4.
    assert out["verdict"] == "unverifiable"
    assert err.strip()


# ── verify --branches (membership-based branch-scope verdict routing) ───────────

_MOD_PY = "def helper(x):\n    return x * 2\n\n\ndef fn(v):\n    return helper(v)\n"
_MOD_PY_DIVERGED = (
    "def helper(x):\n    return x * 2\n\n\ndef fn():\n    return helper(1)\n"
)
_MAIN_PY = "from pkg.mod import fn\n\n\ndef entry():\n    return fn(1)\n"


@pytest.fixture
def two_branch_repo(git_repo):
    """A two-branch tree: `feat-x` holds pkg/mod.py:fn(v) (the mint line);
    `development` rewrites fn's signature (the diverged line). Two top-level code
    entries (main.py + pkg/) keep the graph dialect offset at ""."""
    import subprocess

    repo = git_repo("twobranch")
    (repo / "pkg").mkdir()
    (repo / "pkg" / "mod.py").write_text(_MOD_PY, encoding="utf-8")
    (repo / "main.py").write_text(_MAIN_PY, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base tree"], check=True)
    subprocess.run(["git", "-C", str(repo), "checkout", "-qb", "feat-x"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "checkout", "-qb", "development"], check=True
    )
    (repo / "pkg" / "mod.py").write_text(_MOD_PY_DIVERGED, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "diverge fn"], check=True)
    return repo


def _checkout(repo, ref: str, *, detach: bool = False) -> None:
    import subprocess

    argv = (
        ["git", "-C", str(repo), "checkout", "-q"]
        + (["--detach"] if detach else [])
        + [ref]
    )
    subprocess.run(argv, check=True)


def _mint_fp(repo) -> str:
    """The stored-at-capture combdrift token for pkg/mod.py:fn on branch feat-x."""
    _checkout(repo, "feat-x")
    return combdrift.fingerprint_anchor(str(repo), combdrift.Anchor("pkg/mod.py", "fn"))


@requires_git
def test_verify_branch_scope_member_diverged_stays_stale_byte_identical(
    two_branch_repo, capsys
):
    # Consumer ∈ tagged set: routing adds NOTHING — the stale verdict (remediation,
    # delta, everything) is byte-identical to the untagged verify of the same hit.
    fp = _mint_fp(two_branch_repo)
    _checkout(two_branch_repo, "development")
    _, untagged, _ = _run(
        [
            "verify",
            "--repo",
            str(two_branch_repo),
            "--anchor",
            "pkg/mod.py:fn",
            "--fp",
            fp,
        ],
        capsys,
    )
    code, tagged, _ = _run(
        [
            "verify",
            "--repo",
            str(two_branch_repo),
            "--anchor",
            "pkg/mod.py:fn",
            "--fp",
            fp,
            "--branches",
            "git-branches/1:development",
        ],
        capsys,
    )
    assert code == 0
    assert untagged["verdict"] == "stale" and "remediation" in untagged
    assert tagged == untagged  # the FULL dict, not one key


@requires_git
def test_verify_branch_scope_off_set_diverged_is_branch_scoped(two_branch_repo, capsys):
    # Consumer ∉ tagged set + would-be-stale ⇒ the advisory branch_scoped verdict:
    # NO remediation, NO radius, NO delta — the FULL output dict is pinned.
    fp = _mint_fp(two_branch_repo)
    _checkout(two_branch_repo, "development")
    code, out, _ = _run(
        [
            "verify",
            "--repo",
            str(two_branch_repo),
            "--anchor",
            "pkg/mod.py:fn",
            "--fp",
            fp,
            "--branches",
            "git-branches/1:feat-x",
        ],
        capsys,
    )
    assert code == 0
    assert out == {
        "verdict": "branch_scoped",
        "reason": "off_branch",
        "branch_scope": {"scoped_to": ["feat-x"], "consumer": "development"},
        "notice": cli.BRANCH_SCOPE_NOTICE.format(
            scoped="feat-x", consumer="development"
        ),
    }


@requires_git
def test_verify_branch_scope_off_set_current_suppresses_radius(two_branch_repo, capsys):
    # Consumer ∉ set + shape-match ⇒ current with the radius tier SUPPRESSED — even
    # though the stored neighborhood fp really moved (the untagged control reads
    # radius: changed on the very same inputs).
    _checkout(two_branch_repo, "development")
    sfp_dev = cli._subgraph_fp_core(str(two_branch_repo), "pkg/mod.py:fn")
    stored_sfp = sfp_dev["matrix/subgraph_fp"]
    fp = _mint_fp(two_branch_repo)  # checks out feat-x
    _, control, _ = _run(
        [
            "verify",
            "--repo",
            str(two_branch_repo),
            "--anchor",
            "pkg/mod.py:fn",
            "--fp",
            fp,
            "--subgraph-fp",
            stored_sfp,
        ],
        capsys,
    )
    assert control == {"verdict": "current", "reason": "ok", "radius": "changed"}
    code, tagged, _ = _run(
        [
            "verify",
            "--repo",
            str(two_branch_repo),
            "--anchor",
            "pkg/mod.py:fn",
            "--fp",
            fp,
            "--subgraph-fp",
            stored_sfp,
            "--branches",
            "git-branches/1:development",
        ],
        capsys,
    )
    assert code == 0
    assert tagged == {"verdict": "current", "reason": "ok"}  # FULL dict — no radius key


@requires_git
def test_verify_branch_scope_multi_branch_member_is_member(two_branch_repo, capsys):
    # A multi-name set: the consumer matching ANY member keeps today's semantics.
    fp = _mint_fp(two_branch_repo)
    _checkout(two_branch_repo, "development")
    _, untagged, _ = _run(
        [
            "verify",
            "--repo",
            str(two_branch_repo),
            "--anchor",
            "pkg/mod.py:fn",
            "--fp",
            fp,
        ],
        capsys,
    )
    _, tagged, _ = _run(
        [
            "verify",
            "--repo",
            str(two_branch_repo),
            "--anchor",
            "pkg/mod.py:fn",
            "--fp",
            fp,
            "--branches",
            "git-branches/1:development feat-x",
        ],
        capsys,
    )
    assert tagged == untagged and tagged["verdict"] == "stale"


@requires_git
def test_verify_branch_scope_detached_consumer_byte_identical(two_branch_repo, capsys):
    # A detached consumer resolves no branch name ⇒ membership is undecidable ⇒
    # today's semantics, byte-identical (fail-open — never a false off-branch).
    fp = _mint_fp(two_branch_repo)
    _checkout(two_branch_repo, "development", detach=True)
    _, untagged, _ = _run(
        [
            "verify",
            "--repo",
            str(two_branch_repo),
            "--anchor",
            "pkg/mod.py:fn",
            "--fp",
            fp,
        ],
        capsys,
    )
    _, tagged, _ = _run(
        [
            "verify",
            "--repo",
            str(two_branch_repo),
            "--anchor",
            "pkg/mod.py:fn",
            "--fp",
            fp,
            "--branches",
            "git-branches/1:feat-x",
        ],
        capsys,
    )
    assert tagged == untagged and tagged["verdict"] == "stale"


@requires_git
@pytest.mark.parametrize("token", ["git-branches/999:feat-x", "garbage"])
def test_verify_branch_scope_unreadable_token_byte_identical(
    two_branch_repo, capsys, token
):
    # A future-version or malformed stored token is INCOMPARABLE ⇒ ignored ⇒ the
    # verify output is byte-identical to the untagged run (silence, never a false
    # off-branch — the meta envelope law's failure direction).
    fp = _mint_fp(two_branch_repo)
    _checkout(two_branch_repo, "development")
    _, untagged, _ = _run(
        [
            "verify",
            "--repo",
            str(two_branch_repo),
            "--anchor",
            "pkg/mod.py:fn",
            "--fp",
            fp,
        ],
        capsys,
    )
    _, tagged, _ = _run(
        [
            "verify",
            "--repo",
            str(two_branch_repo),
            "--anchor",
            "pkg/mod.py:fn",
            "--fp",
            fp,
            "--branches",
            token,
        ],
        capsys,
    )
    assert tagged == untagged and tagged["verdict"] == "stale"


@requires_git
def test_verify_branch_scope_reader_ahead_upgrade_sim_byte_identical(
    two_branch_repo, capsys, monkeypatch
):
    # Upgrade simulation (the BUG-037 idiom): a stored v1 token under a BUMPED reader
    # is incomparable ⇒ byte-identity with the untagged run — never routed off-branch.
    fp = _mint_fp(two_branch_repo)
    _checkout(two_branch_repo, "development")
    _, untagged, _ = _run(
        [
            "verify",
            "--repo",
            str(two_branch_repo),
            "--anchor",
            "pkg/mod.py:fn",
            "--fp",
            fp,
        ],
        capsys,
    )
    monkeypatch.setattr(cli, "BRANCHES_VERSION", "2")
    _, tagged, _ = _run(
        [
            "verify",
            "--repo",
            str(two_branch_repo),
            "--anchor",
            "pkg/mod.py:fn",
            "--fp",
            fp,
            "--branches",
            "git-branches/1:feat-x",
        ],
        capsys,
    )
    assert tagged == untagged and tagged["verdict"] == "stale"


# ── graph verb group (operator, exit-coded; persistent per-checkout matrix cache) ─


@pytest.fixture
def calls_repo(tmp_path):
    """A plain tree where pkg/f.py:bar CALLS pkg/f.py:foo — a caller edge for radius."""
    root = tmp_path / "callsrepo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "f.py").write_text(
        "def foo(a, b):\n    return a + b\n\n\ndef bar():\n    return foo(1, 2)\n",
        encoding="utf-8",
    )
    return root


def test_graph_update_builds_and_persists_under_state_dir(py_repo, edge_home, capsys):
    code, out, _ = _run(["graph", "update", "--repo", str(py_repo)], capsys)
    assert code == 0
    assert out["nodes"] >= 1
    sha = out["graph_sha256"]
    assert len(sha) == 64 and int(sha, 16) >= 0  # a real hex graph identity
    cache = cli._matrix_out_dir(str(py_repo))
    assert cache.is_relative_to(edge_home / "state" / "matrix")
    assert (cache / "graph.json").is_file() and (cache / "manifest.json").is_file()
    # The cache must live OUTSIDE the repo: an untracked artifact dir would flip
    # `git status --porcelain` for any git operation over the checkout.
    assert not (py_repo / "matrix-out").exists()


def test_graph_update_is_incremental_on_second_run(py_repo, capsys, monkeypatch):
    import hive.matrix as matrix

    code1, out1, _ = _run(["graph", "update", "--repo", str(py_repo)], capsys)
    assert code1 == 0
    real = matrix.build_graph
    scratch_builds = []

    def spy(*args, **kwargs):
        scratch_builds.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(matrix, "build_graph", spy)
    code2, out2, _ = _run(["graph", "update", "--repo", str(py_repo)], capsys)
    assert code2 == 0
    assert scratch_builds == []  # incremental: no from-scratch rebuild
    assert (
        out2["graph_sha256"] == out1["graph_sha256"]
    )  # unchanged tree, identical identity


def test_graph_radius_prints_hits_json(calls_repo, capsys):
    # No prior `graph update` — the first graph query on a checkout self-builds the cache.
    code, out, _ = _run(
        ["graph", "radius", "--repo", str(calls_repo), "--anchor", "pkg/f.py:foo"],
        capsys,
    )
    assert code == 0
    assert out["seed"]  # the anchor's symbol resolved to a node
    assert out["unsound"] is True  # the engine's lower-bound contract rides through
    callers = out["callers"]
    assert callers and any("bar" in hit["node_id"] for hit in callers)
    assert all(
        {"node_id", "depth", "via_relation", "source_file"} <= hit.keys()
        for hit in callers
    )


def test_graph_help_lists_verbs_exit_0(capsys):
    code = main(["graph", "--help"])
    cap = capsys.readouterr()
    assert code == 0
    assert "update" in cap.out and "radius" in cap.out and "fp" in cap.out
    assert cap.err == ""
    assert main(["graph", "-h"]) == 0  # -h prints the same single-owner text
    assert "radius" in capsys.readouterr().out


def test_help_lists_graph_stub(capsys):
    # BUG-027: graph dispatches in main() before parsing; `hive-edge --help` must still list it.
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    assert "graph" in capsys.readouterr().out


def test_graph_empty_prints_usage_to_stderr_exit_two(capsys):
    code = main(["graph"])
    cap = capsys.readouterr()
    assert code == 2
    assert "update" in cap.err and "radius" in cap.err and "fp" in cap.err


def test_graph_unknown_subcommand_is_usage_error(capsys):
    assert main(["graph", "frobnicate"]) == 2


def test_graph_fp_mints_and_explains_member_tokens(tmp_path, capsys):
    # fp is live: it prints the {"matrix/subgraph_fp": ...} map, and --explain additionally
    # lists the member tokens behind the digest (the feedback surface a silent {} lacked).
    root = tmp_path / "fprepo"
    (root / "pkg").mkdir(parents=True)
    (root / "main.py").write_text(
        "from pkg.f import foo\n\n\ndef entry():\n    return foo(1, 2)\n",
        encoding="utf-8",
    )
    (root / "pkg" / "f.py").write_text(
        "def foo(a, b):\n    return a + b\n", encoding="utf-8"
    )
    code, out, _ = _run(
        ["graph", "fp", "--repo", str(root), "--anchor", "pkg/f.py:foo"], capsys
    )
    assert code == 0
    token = out["matrix/subgraph_fp"]
    assert token.startswith("matrix-subgraph-fp/1:")
    code2, out2, _ = _run(
        ["graph", "fp", "--repo", str(root), "--anchor", "pkg/f.py:foo", "--explain"],
        capsys,
    )
    assert code2 == 0
    assert out2["matrix/subgraph_fp"] == token
    members = out2["members"]
    assert any(m.startswith("pkg/f.py:foo:combdrift-fp/") for m in members)
    assert any(
        m.startswith("main.py:entry:") for m in members
    )  # the caller-cone rides along


def test_graph_fp_unresolvable_anchor_prints_empty_map(py_repo, capsys):
    # Unresolvable is NOT a fault: the operator gets a clean {} and exit 0 — never a bogus
    # token. A real fault (unloadable graph) stays exit 2 like every other graph verb.
    code, out, _ = _run(
        ["graph", "fp", "--repo", str(py_repo), "--anchor", "pkg/f.py:ghost"], capsys
    )
    assert code == 0 and out == {}


def test_matrix_out_dir_is_stable_and_repo_keyed(tmp_path, edge_home):
    a = tmp_path / "repo-a"
    b = tmp_path / "repo-b"
    a.mkdir()
    b.mkdir()
    dir_a = cli._matrix_out_dir(str(a))
    # Stable: the same checkout always maps to the same cache dir.
    assert dir_a == cli._matrix_out_dir(str(a))
    # Repo-keyed: a different checkout must NEVER share (or clobber) another repo's cache.
    assert dir_a != cli._matrix_out_dir(str(b))
    # Pinned under the state home — never inside the repo.
    assert dir_a.is_relative_to(edge_home / "state" / "matrix")
    # Keyed by the REAL path: a symlinked view of the same checkout shares its cache.
    link = tmp_path / "link-a"
    link.symlink_to(a)
    assert cli._matrix_out_dir(str(link)) == dir_a


def test_load_graph_returns_none_on_fault(py_repo, edge_home, capsys, monkeypatch):
    import hive.matrix as matrix

    def boom(*a, **k):
        raise RuntimeError("graph engine broke")

    monkeypatch.setattr(matrix, "update", boom)
    assert cli._load_graph(str(py_repo)) is None
    err = capsys.readouterr().err
    assert "graph" in err and "internal error" in err  # one operator-visible line
    assert "internal error" in (edge_home / "last-error.log").read_text(
        encoding="utf-8"
    )


def test_graph_update_fault_exits_two(py_repo, capsys, monkeypatch):
    # Operator regime: a fault is a NONZERO exit (unlike the fail-open agent verbs).
    import hive.matrix as matrix

    def boom(*a, **k):
        raise RuntimeError("graph engine broke")

    monkeypatch.setattr(matrix, "update", boom)
    code, out, err = _run(["graph", "update", "--repo", str(py_repo)], capsys)
    assert code == 2
    assert err.strip()


# ── the device state home ────────────────────────────────────────────────────────


def test_hive_edge_home_env_overrides_config_dir(tmp_path):
    """BUG-028: the device state home is overridable via HIVE_EDGE_HOME (import-time read —
    the tool is a short-lived process). cli.py is the ONE owner of the resolution since the
    lifecycle module (launch.py) left with the `upgrade` verb."""
    import os
    import subprocess
    import sys

    custom = tmp_path / "custom-home"
    env = {**os.environ, "HIVE_EDGE_HOME": str(custom)}
    code = "import hive.edge.cli as c; print(c.CONFIG_DIR)"
    res = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env
    )
    assert res.returncode == 0, res.stderr
    assert res.stdout.splitlines() == [str(custom)]


# ── --version ───────────────────────────────────────────────────────────────────


def test_version_prints_edge_and_engine_stamps(capsys):
    code = main(["--version"])
    out = capsys.readouterr().out
    assert code == 0
    assert "hive-edge " in out
    assert "combdrift" in out and "matrix" in out
