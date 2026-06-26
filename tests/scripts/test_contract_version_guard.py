"""Contract-version pre-commit guard: pure-helper units + hermetic temp-repo integration.

The integration scenarios build a throwaway git repo with a STUB ``hive.app.onboard_ref`` whose
``bundle_digest()`` depends on ``CONTRACT_VERSION`` (mirroring the real version-embedded-in-block
property), so a version bump is observable as a golden change — exercising the guard end to end,
including its fresh-interpreter digest subprocess, without touching the real package."""
from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import contract_version_guard as guard
from scripts.contract_version_guard import (
    decide, format_version, parse_version, set_golden_line, set_version_line,
)

GUARD = Path(guard.__file__).resolve()


# ── pure helpers ──────────────────────────────────────────────────────────────────────────────

def test_parse_version_reads_the_padded_integer():
    assert parse_version('CONTRACT_VERSION: str = "v.01"\n') == 1
    assert parse_version('x = 1\nCONTRACT_VERSION: str = "v.100"\ny = 2\n') == 100


def test_parse_version_raises_when_absent():
    with pytest.raises(ValueError):
        parse_version('NO_VERSION_HERE = 1\n')


@pytest.mark.parametrize("n,expected", [
    (1, "v.01"), (2, "v.02"), (9, "v.09"), (10, "v.10"),
    (99, "v.99"), (100, "v.100"), (101, "v.101"),
])
def test_format_version_pads_to_two_and_extends_past_99(n, expected):
    assert format_version(n) == expected


@pytest.mark.parametrize("current,head,expected", [
    (1, 1, (2, True)),    # not ahead -> bump to head+1
    (5, 1, (5, False)),   # already hand-bumped ahead -> keep
    (1, 5, (6, True)),    # behind HEAD -> jump to head+1 (strictly greater than initial)
    (2, 1, (2, False)),   # exactly one ahead -> keep
])
def test_decide_keeps_when_ahead_else_steps_to_head_plus_one(current, head, expected):
    assert decide(current, head) == expected


def test_set_version_line_rewrites_exactly_the_one_line():
    text = 'a\nCONTRACT_VERSION: str = "v.09"\nb\n'
    out = set_version_line(text, "v.10")
    assert 'CONTRACT_VERSION: str = "v.10"' in out
    assert out == 'a\nCONTRACT_VERSION: str = "v.10"\nb\n'   # nothing else moved
    assert parse_version(out) == 10


def test_set_version_line_raises_when_no_line_matches():
    with pytest.raises(ValueError):
        set_version_line("nothing here\n", "v.02")


def test_set_golden_line_rewrites_the_64_hex_literal():
    new = "a" * 64
    text = f'_GOLDEN_BUNDLE_SHA256 = "{"e" * 64}"\n'
    assert set_golden_line(text, new) == f'_GOLDEN_BUNDLE_SHA256 = "{new}"\n'


def test_set_golden_line_raises_when_absent():
    with pytest.raises(ValueError):
        set_golden_line("no golden\n", "a" * 64)


# ── hermetic integration ────────────────────────────────────────────────────────────────────

def _stub_digest(version: str) -> str:
    """The stub onboard_ref's bundle_digest() output for a given CONTRACT_VERSION."""
    return hashlib.sha256(("block-" + version).encode("utf-8")).hexdigest()


def _write(repo: Path, rel: str, content: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _make_repo(tmp_path: Path) -> Path:
    """A baseline repo committed at CONTRACT_VERSION v.01 with a consistent keystone golden."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(repo, "hive/__init__.py", "")
    _write(repo, "hive/app/__init__.py", "")
    _write(repo, "hive/app/onboard_ref.py",
           'import hashlib\n'
           'CONTRACT_VERSION: str = "v.01"\n'
           'def bundle_digest() -> str:\n'
           '    return hashlib.sha256(("block-" + CONTRACT_VERSION).encode("utf-8")).hexdigest()\n')
    _write(repo, "hive/domain/__init__.py", "")
    _write(repo, "hive/domain/kinds.py", "TAXONOMY = 'v1'\n")
    _write(repo, "tests/app/test_onboard_ref.py",
           f'_GOLDEN_BUNDLE_SHA256 = "{_stub_digest("v.01")}"\n')
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "baseline"], cwd=repo, check=True)
    return repo


def _run_guard(repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(GUARD)], cwd=repo, capture_output=True, text=True)


def _version_on_disk(repo: Path) -> str:
    text = (repo / "hive/app/onboard_ref.py").read_text(encoding="utf-8")
    return format_version(parse_version(text))


def _golden_on_disk(repo: Path) -> str:
    text = (repo / "tests/app/test_onboard_ref.py").read_text(encoding="utf-8")
    return text.split('"')[1]


def _staged(repo: Path) -> set[str]:
    out = subprocess.run(["git", "diff", "--cached", "--name-only"],
                         cwd=repo, capture_output=True, text=True)
    return {line for line in out.stdout.splitlines() if line}


def test_bumps_and_regenerates_golden_when_contract_staged_without_a_bump(tmp_path):
    repo = _make_repo(tmp_path)
    # an edit to the contract file, staged, with the version left at v.01
    _write(repo, "hive/app/onboard_ref.py",
           (repo / "hive/app/onboard_ref.py").read_text(encoding="utf-8") + "# tweak\n")
    subprocess.run(["git", "add", "hive/app/onboard_ref.py"], cwd=repo, check=True)

    res = _run_guard(repo)

    assert res.returncode == 0
    assert "bumped from v.01 to v.02" in res.stdout
    assert _version_on_disk(repo) == "v.02"
    assert _golden_on_disk(repo) == _stub_digest("v.02")            # regenerated for the new version
    assert {"hive/app/onboard_ref.py", "tests/app/test_onboard_ref.py"} <= _staged(repo)


def test_a_kinds_only_change_still_bumps_the_version(tmp_path):
    repo = _make_repo(tmp_path)
    _write(repo, "hive/domain/kinds.py", "TAXONOMY = 'v2'\n")
    subprocess.run(["git", "add", "hive/domain/kinds.py"], cwd=repo, check=True)

    res = _run_guard(repo)

    assert res.returncode == 0
    assert _version_on_disk(repo) == "v.02"                          # bumped though dev never staged it
    assert _golden_on_disk(repo) == _stub_digest("v.02")
    assert {"hive/app/onboard_ref.py", "tests/app/test_onboard_ref.py"} <= _staged(repo)


def test_does_not_double_bump_when_already_ahead_but_regenerates_stale_golden(tmp_path):
    repo = _make_repo(tmp_path)
    # developer hand-bumped to v.02 but left the golden stale at v.01's value
    _write(repo, "hive/app/onboard_ref.py",
           'import hashlib\n'
           'CONTRACT_VERSION: str = "v.02"\n'
           'def bundle_digest() -> str:\n'
           '    return hashlib.sha256(("block-" + CONTRACT_VERSION).encode("utf-8")).hexdigest()\n')
    subprocess.run(["git", "add", "hive/app/onboard_ref.py"], cwd=repo, check=True)

    res = _run_guard(repo)

    assert res.returncode == 0
    assert _version_on_disk(repo) == "v.02"                          # NOT bumped to v.03
    assert _golden_on_disk(repo) == _stub_digest("v.02")            # stale golden healed
    assert "already at v.02" in res.stdout


def test_no_op_when_no_watched_file_is_staged(tmp_path):
    repo = _make_repo(tmp_path)
    _write(repo, "README.md", "hello\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)

    res = _run_guard(repo)

    assert res.returncode == 0
    assert res.stdout.strip() == ""                                 # silent
    assert _version_on_disk(repo) == "v.01"                          # untouched
    assert "tests/app/test_onboard_ref.py" not in _staged(repo)
