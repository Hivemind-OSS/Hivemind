"""Single diff owner: git → frozen change carriers + two provisioned worktrees.

The census parses the unified diff exactly once, here. `open_change` is the
one entry point that performs that parse and provisions detached worktrees for
both sides; everything downstream consumes the frozen carriers it yields, and
no other module re-parses git output.

The parser is a pure state machine over `git diff --unified=0 --no-color
--find-renames` text. Classification is by parser STATE — inside a hunk the
line budget announced by the `@@` header decides what is content — never by
leading-prefix sniffing: a removed SQL comment `-- x` surfaces in the diff as
`--- x`, and removed content can byte-mimic a `--- a/path` file header, so a
prefix classifier would open a phantom file section mid-hunk.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

_SHA_RE = re.compile(r"[0-9a-f]{40}")
_HUNK_RE = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_BINARY_RE = re.compile(r"Binary files (.+) and (.+) differ")

# Extended header lines that carry nothing the carriers need.
_NOOP_HEADERS = (
    "index ",
    "old mode ",
    "new mode ",
    "similarity index ",
    "dissimilarity index ",
)


class DiffError(Exception):
    """A git invocation failed or its diff output did not parse; fail fast."""


@dataclass(frozen=True, slots=True)
class ChangedFile:
    path: str  # head-side path (base-side for deleted)
    status: Literal["added", "deleted", "modified", "renamed", "binary"]
    added_spans: tuple[tuple[int, int], ...]  # head-side (start, end) line spans
    removed_spans: tuple[tuple[int, int], ...]  # base-side spans
    old_path: str | None = None  # renamed only


@dataclass(frozen=True, slots=True)
class ChangeSet:
    base_sha: str
    head_sha: str
    files: tuple[ChangedFile, ...]

    def __post_init__(self) -> None:
        for name, sha in (("base_sha", self.base_sha), ("head_sha", self.head_sha)):
            if not _SHA_RE.fullmatch(sha):
                raise ValueError(
                    f"{name} must be a full 40-hex commit SHA, got {sha!r}"
                )


@dataclass(frozen=True, slots=True)
class Change:
    touched: ChangeSet
    base_tree: Path  # worktree checked out at base_sha
    head_tree: Path  # worktree checked out at head_sha


def _git(repo: Path, *args: str) -> str:
    # Call-time import: gitenv lives in the matrix package (the ONE denylist
    # owner), and hive.census loads engine packages only inside calls — the
    # census CLI pins MATRIX_OUT to a scratch dir before any engine import.
    from hive.matrix import gitenv

    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=gitenv.clean_git_env(),
    )
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip()
        raise DiffError(f"git {args[0]} failed: {detail}")
    return proc.stdout


def current_ref(repo: Path) -> str | None:
    """The measured checkout's current branch name (`git rev-parse --abbrev-ref HEAD`)
    for the receipt's provenance ref stamp. Fail-open: a git fault, empty output, or a
    detached HEAD (the literal ``"HEAD"``) yields None — the caller then omits the
    stamp and the receipt builds exactly as before."""
    try:
        name = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    except DiffError:
        return None
    return None if (not name or name == "HEAD") else name


def resolve_shas(repo: Path, base: str, head: str) -> tuple[str, str]:
    """Resolve two refs to full commit SHAs; DiffError on an unknown ref."""

    def resolve(ref: str) -> str:
        sha = _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").strip()
        if not _SHA_RE.fullmatch(sha):
            raise DiffError(f"git rev-parse returned a non-SHA for {ref!r}: {sha!r}")
        return sha

    return resolve(base), resolve(head)


def parse_unified_diff(text: str) -> tuple[ChangedFile, ...]:
    """Parse `git diff --unified=0 --no-color --find-renames` output.

    Pure state machine; any shape outside the known grammar raises DiffError
    rather than misparsing.
    """
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    files: list[ChangedFile] = []
    i = 0
    while i < len(lines):
        if not lines[i].startswith("diff --git "):
            raise DiffError(f"expected a 'diff --git' file header, got: {lines[i]!r}")
        i = _parse_file_section(lines, i, files)
    return tuple(files)


def _parse_file_section(lines: list[str], i: int, out: list[ChangedFile]) -> int:
    git_line = lines[i]
    i += 1
    old_hdr: str | None = None
    new_hdr: str | None = None
    rename_from: str | None = None
    rename_to: str | None = None
    binary_line: str | None = None
    new_file = deleted_file = False
    added_spans: list[tuple[int, int]] = []
    removed_spans: list[tuple[int, int]] = []
    in_hunks = False

    while i < len(lines):
        line = lines[i]
        if line.startswith("diff --git "):
            break
        if line.startswith("@@"):
            in_hunks = True
            i = _consume_hunk(lines, i, added_spans, removed_spans)
            continue
        if in_hunks:
            raise DiffError(f"unexpected line after hunk content: {line!r}")
        if line.startswith(_NOOP_HEADERS):
            pass
        elif line.startswith("new file mode "):
            new_file = True
        elif line.startswith("deleted file mode "):
            deleted_file = True
        elif line.startswith("rename from "):
            rename_from = line[len("rename from ") :]
        elif line.startswith("rename to "):
            rename_to = line[len("rename to ") :]
        elif line.startswith("--- "):
            old_hdr = _header_path(line[4:], "a/")
        elif line.startswith("+++ "):
            new_hdr = _header_path(line[4:], "b/")
        elif _BINARY_RE.fullmatch(line):
            binary_line = line
        else:
            raise DiffError(f"unrecognized diff header line: {line!r}")
        i += 1

    out.append(
        _finalize_file(
            git_line=git_line,
            old_hdr=old_hdr,
            new_hdr=new_hdr,
            rename_from=rename_from,
            rename_to=rename_to,
            binary_line=binary_line,
            new_file=new_file,
            deleted_file=deleted_file,
            added_spans=tuple(added_spans),
            removed_spans=tuple(removed_spans),
        )
    )
    return i


def _consume_hunk(
    lines: list[str],
    i: int,
    added_spans: list[tuple[int, int]],
    removed_spans: list[tuple[int, int]],
) -> int:
    header = _HUNK_RE.match(lines[i])
    if header is None:
        raise DiffError(f"malformed hunk header: {lines[i]!r}")
    old_start = int(header.group(1))
    old_count = int(header.group(2)) if header.group(2) is not None else 1
    new_start = int(header.group(3))
    new_count = int(header.group(4)) if header.group(4) is not None else 1
    if old_count:
        removed_spans.append((old_start, old_start + old_count - 1))
    if new_count:
        added_spans.append((new_start, new_start + new_count - 1))
    i += 1

    # Counted consumption: while the header's line budget remains, '-'/'+'
    # lines are CONTENT by state — including bytes that mimic file headers.
    need_removed, need_added = old_count, new_count
    while need_removed or need_added:
        if i >= len(lines):
            raise DiffError(
                "truncated hunk: diff ended before the announced line counts"
            )
        body = lines[i]
        if body.startswith("-") and need_removed:
            need_removed -= 1
        elif body.startswith("+") and need_added:
            need_added -= 1
        elif body.startswith("\\"):
            pass  # "\ No newline at end of file" is uncounted
        else:
            raise DiffError(f"unexpected line inside hunk: {body!r}")
        i += 1
    while i < len(lines) and lines[i].startswith("\\"):
        i += 1
    return i


def _header_path(raw: str, prefix: str) -> str | None:
    if raw == "/dev/null":
        return None
    if raw.startswith(prefix):
        return raw[len(prefix) :]
    raise DiffError(f"file header path lacks the {prefix!r} prefix: {raw!r}")


def _path_from_git_line(git_line: str) -> str:
    """Recover the path from `diff --git a/P b/P` when no other header names it.

    Only the symmetric (non-rename) form is decidable — try every ` b/` split
    and accept the one where both sides name the same path.
    """
    spec = git_line[len("diff --git ") :]
    at = spec.find(" b/")
    while at != -1:
        left, right = spec[:at], spec[at + 3 :]
        if left.startswith("a/") and left[2:] == right:
            return right
        at = spec.find(" b/", at + 1)
    raise DiffError(f"cannot determine path from {git_line!r}")


def _binary_path(binary_line: str) -> str:
    match = _BINARY_RE.fullmatch(binary_line)
    assert match is not None  # callers only pass a matched line
    for raw, prefix in ((match.group(2), "b/"), (match.group(1), "a/")):
        path = _header_path(raw, prefix)
        if path is not None:
            return path
    raise DiffError(f"binary change names no path: {binary_line!r}")


def _finalize_file(
    *,
    git_line: str,
    old_hdr: str | None,
    new_hdr: str | None,
    rename_from: str | None,
    rename_to: str | None,
    binary_line: str | None,
    new_file: bool,
    deleted_file: bool,
    added_spans: tuple[tuple[int, int], ...],
    removed_spans: tuple[tuple[int, int], ...],
) -> ChangedFile:
    old_path: str | None = None
    if binary_line is not None:
        # Binary trumps direction: no line spans exist for it either way.
        status: Literal["added", "deleted", "modified", "renamed", "binary"] = "binary"
        path = _binary_path(binary_line)
    elif rename_from is not None or rename_to is not None:
        if rename_from is None or rename_to is None:
            raise DiffError(f"incomplete rename headers under {git_line!r}")
        status = "renamed"
        path, old_path = rename_to, rename_from
    elif new_file:
        status = "added"
        path = new_hdr or _path_from_git_line(git_line)
    elif deleted_file:
        status = "deleted"
        path = old_hdr or _path_from_git_line(git_line)
    else:
        status = "modified"
        path = new_hdr or old_hdr or _path_from_git_line(git_line)
    return ChangedFile(
        path=path,
        status=status,
        added_spans=added_spans,
        removed_spans=removed_spans,
        old_path=old_path,
    )


@contextmanager
def open_change(repo: Path, base: str, head: str) -> Iterator[Change]:
    """Resolve, parse once, provision both worktrees, and always clean up.

    The only way to obtain a ChangeSet is the single parse inside this
    context manager; the worktrees are removed on every exit path, including
    when the body raises.
    """
    base_sha, head_sha = resolve_shas(repo, base, head)
    diff_text = _git(
        repo, "diff", "--unified=0", "--no-color", "--find-renames", base_sha, head_sha
    )
    touched = ChangeSet(
        base_sha=base_sha, head_sha=head_sha, files=parse_unified_diff(diff_text)
    )
    scratch = Path(tempfile.mkdtemp(prefix="hive-census-change-"))
    provisioned: list[Path] = []
    try:
        base_tree = scratch / "base"
        head_tree = scratch / "head"
        for tree, sha in ((base_tree, base_sha), (head_tree, head_sha)):
            _git(repo, "worktree", "add", "--detach", str(tree), sha)
            provisioned.append(tree)
        yield Change(touched=touched, base_tree=base_tree, head_tree=head_tree)
    finally:
        for tree in provisioned:
            try:
                _git(repo, "worktree", "remove", "--force", str(tree))
            except DiffError:
                pass  # prune + rmtree below still reap it
        try:
            _git(repo, "worktree", "prune")
        except DiffError:
            pass
        shutil.rmtree(scratch, ignore_errors=True)
