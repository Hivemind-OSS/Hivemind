"""Contract tests for the single diff owner.

Two tiers: pure-parser fixtures (real `git diff --unified=0 --no-color
--find-renames` output, spans pinned exactly — including content that
byte-mimics file headers, which only parser-state classification survives) and
a real-git tier that pins `open_change`'s SHA resolution, worktree
orientation, and guaranteed cleanup on both the clean and the raise path.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hive.census import Change, ChangedFile, ChangeSet, open_change, parse_unified_diff
from hive.census.diff import DiffError, current_ref, resolve_shas

FIXTURES = Path(__file__).resolve().parent / "fixtures"

SHA_A = "a" * 40
SHA_B = "b" * 40


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def hermetic_git_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate every git subprocess from user/system config and identity."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "census-test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "census@test.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "census-test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "census@test.invalid")


class TestParseUnifiedDiff:
    def test_empty_diff_parses_to_no_files(self) -> None:
        assert parse_unified_diff("") == ()

    def test_modify(self) -> None:
        assert parse_unified_diff(fixture("modify.diff")) == (
            ChangedFile(
                path="notes.txt",
                status="modified",
                added_spans=((3, 3),),
                removed_spans=((3, 3),),
            ),
        )

    def test_add(self) -> None:
        assert parse_unified_diff(fixture("add.diff")) == (
            ChangedFile(
                path="greeting.txt",
                status="added",
                added_spans=((1, 3),),
                removed_spans=(),
            ),
        )

    def test_delete(self) -> None:
        assert parse_unified_diff(fixture("delete.diff")) == (
            ChangedFile(
                path="doomed.txt",
                status="deleted",
                added_spans=(),
                removed_spans=((1, 2),),
            ),
        )

    def test_rename_with_edit(self) -> None:
        assert parse_unified_diff(fixture("rename.diff")) == (
            ChangedFile(
                path="renamed.txt",
                status="renamed",
                added_spans=((4, 4),),
                removed_spans=((4, 4),),
                old_path="rename_me.txt",
            ),
        )

    def test_pure_rename_has_no_spans(self) -> None:
        assert parse_unified_diff(fixture("rename_pure.diff")) == (
            ChangedFile(
                path="new_name.txt",
                status="renamed",
                added_spans=(),
                removed_spans=(),
                old_path="old_name.txt",
            ),
        )

    def test_binary(self) -> None:
        assert parse_unified_diff(fixture("binary.diff")) == (
            ChangedFile(
                path="blob.bin",
                status="binary",
                added_spans=(),
                removed_spans=(),
            ),
        )

    def test_multi_hunk_span_math(self) -> None:
        assert parse_unified_diff(fixture("multihunk.diff")) == (
            ChangedFile(
                path="many.txt",
                status="modified",
                added_spans=((2, 2), (7, 7)),
                removed_spans=((2, 2), (5, 6), (9, 9)),
            ),
        )

    def test_mode_only_change_is_modified_with_no_spans(self) -> None:
        assert parse_unified_diff(fixture("mode_only.diff")) == (
            ChangedFile(
                path="script.sh",
                status="modified",
                added_spans=(),
                removed_spans=(),
            ),
        )

    def test_header_mimic_content_is_never_read_as_a_file_header(self) -> None:
        # The removed content includes the bytes "--- a/evil.sql" and
        # "---- header-lookalike", and the added content includes
        # "+++ b/fake.sql": a leading-prefix classifier would open a phantom
        # file section here; counted hunk state must consume them as content.
        assert parse_unified_diff(fixture("header_mimic.diff")) == (
            ChangedFile(
                path="evil.sql",
                status="modified",
                added_spans=((2, 3),),
                removed_spans=((2, 5),),
            ),
        )

    def test_garbage_text_fails_fast(self) -> None:
        with pytest.raises(DiffError):
            parse_unified_diff("this is not a diff\n")

    def test_truncated_hunk_fails_fast(self) -> None:
        truncated = (
            "diff --git a/notes.txt b/notes.txt\n"
            "index d9b2d36..8380a98 100644\n"
            "--- a/notes.txt\n"
            "+++ b/notes.txt\n"
            "@@ -3,2 +3 @@ bravo\n"
            "-charlie\n"
            "+CHARLIE\n"
        )
        with pytest.raises(DiffError):
            parse_unified_diff(truncated)


class TestChangeSetInvariants:
    def test_full_lowercase_hex_shas_accepted(self) -> None:
        cs = ChangeSet(base_sha=SHA_A, head_sha=SHA_B, files=())
        assert (cs.base_sha, cs.head_sha) == (SHA_A, SHA_B)

    def test_short_sha_refused(self) -> None:
        with pytest.raises(ValueError):
            ChangeSet(base_sha=SHA_A[:39], head_sha=SHA_B, files=())

    def test_non_hex_sha_refused(self) -> None:
        with pytest.raises(ValueError):
            ChangeSet(base_sha=SHA_A, head_sha="z" * 40, files=())


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, f"git {args} failed: {proc.stderr}"
    return proc.stdout


def _worktree_count(repo: Path) -> int:
    listing = _git(repo, "worktree", "list", "--porcelain")
    return sum(1 for line in listing.splitlines() if line.startswith("worktree "))


@pytest.fixture()
def two_commit_repo(tmp_path: Path) -> Path:
    """Base commit: marker.txt="base", doomed.txt. Head commit: marker.txt="head",
    new.txt added, doomed.txt deleted."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "marker.txt").write_text("base\n")
    (repo / "doomed.txt").write_text("bye\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    (repo / "marker.txt").write_text("head\n")
    (repo / "new.txt").write_text("hi\n")
    (repo / "doomed.txt").unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "head")
    return repo


class TestResolveShas:
    def test_symbolic_refs_resolve_to_full_shas(self, two_commit_repo: Path) -> None:
        base, head = resolve_shas(two_commit_repo, "HEAD~1", "HEAD")
        assert base == _git(two_commit_repo, "rev-parse", "HEAD~1").strip()
        assert head == _git(two_commit_repo, "rev-parse", "HEAD").strip()
        assert base != head

    def test_unknown_ref_is_a_diff_error(self, two_commit_repo: Path) -> None:
        with pytest.raises(DiffError):
            resolve_shas(two_commit_repo, "no-such-ref", "HEAD")


class TestOpenChange:
    def test_yields_shas_statuses_and_oriented_worktrees(
        self, two_commit_repo: Path
    ) -> None:
        base_sha, head_sha = resolve_shas(two_commit_repo, "HEAD~1", "HEAD")
        with open_change(two_commit_repo, "HEAD~1", "HEAD") as change:
            assert isinstance(change, Change)
            assert change.touched.base_sha == base_sha
            assert change.touched.head_sha == head_sha
            statuses = {f.path: f.status for f in change.touched.files}
            assert statuses == {
                "marker.txt": "modified",
                "new.txt": "added",
                "doomed.txt": "deleted",
            }
            # Orientation: base worktree holds the base image, head the head.
            assert (change.base_tree / "marker.txt").read_text() == "base\n"
            assert (change.head_tree / "marker.txt").read_text() == "head\n"
            assert (change.base_tree / "doomed.txt").exists()
            assert not (change.head_tree / "doomed.txt").exists()
            assert (change.head_tree / "new.txt").exists()
            assert not (change.base_tree / "new.txt").exists()
            assert _worktree_count(two_commit_repo) == 3
            trees = (change.base_tree, change.head_tree)
        for tree in trees:
            assert not tree.exists()
        assert _worktree_count(two_commit_repo) == 1

    def test_parsed_spans_match_the_real_diff(self, two_commit_repo: Path) -> None:
        with open_change(two_commit_repo, "HEAD~1", "HEAD") as change:
            by_path = {f.path: f for f in change.touched.files}
            marker = by_path["marker.txt"]
            assert marker.removed_spans == ((1, 1),)
            assert marker.added_spans == ((1, 1),)

    def test_worktrees_cleaned_up_when_body_raises(
        self, two_commit_repo: Path
    ) -> None:
        trees: tuple[Path, ...] = ()
        with pytest.raises(RuntimeError, match="boom"):
            with open_change(two_commit_repo, "HEAD~1", "HEAD") as change:
                trees = (change.base_tree, change.head_tree)
                assert all(tree.exists() for tree in trees)
                raise RuntimeError("boom")
        for tree in trees:
            assert not tree.exists()
        assert _worktree_count(two_commit_repo) == 1

    def test_unknown_ref_fails_fast_with_no_worktree_left(
        self, two_commit_repo: Path
    ) -> None:
        with pytest.raises(DiffError):
            with open_change(two_commit_repo, "no-such-ref", "HEAD"):
                pytest.fail("body must never run on an unresolvable ref")
        assert _worktree_count(two_commit_repo) == 1


class TestCurrentRef:
    """current_ref — the measured checkout's branch name for the receipt ref stamp.
    Fail-open: a git fault, empty output, or a detached HEAD yields None (the caller
    then omits the stamp; the receipt builds unchanged)."""

    def test_named_branch_resolves(self, two_commit_repo: Path) -> None:
        _git(two_commit_repo, "checkout", "-qb", "feat-x")
        assert current_ref(two_commit_repo) == "feat-x"

    def test_detached_head_is_none(self, two_commit_repo: Path) -> None:
        _git(two_commit_repo, "checkout", "-q", "--detach")
        assert current_ref(two_commit_repo) is None

    def test_git_fault_is_none_never_a_raise(self, tmp_path: Path) -> None:
        plain = tmp_path / "not-a-repo"
        plain.mkdir()
        assert current_ref(plain) is None
