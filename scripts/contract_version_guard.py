"""Pre-commit guard that keeps the agent-contract version monotonic across commits.

When a commit touches the served-contract source (``hive/app/onboard_ref.py`` — the
``initialize`` instructions, the installable rules block, the claude hooks, the auto-approve
allowlist, and ``CONTRACT_VERSION`` — or ``hive/domain/kinds.py``, whose taxonomy renders into
the floor), this guard ensures ``CONTRACT_VERSION`` is greater than at HEAD. If it is not, it
bumps it by one and ``git add``s the change so the bump rides *in* the commit being created.

Because ``CONTRACT_VERSION`` is embedded in the rendered block, any bump changes the keystone
bundle bytes — so the guard also regenerates the ``_GOLDEN_BUNDLE_SHA256`` golden (from the one
owner, ``onboard_ref.bundle_digest()``) in the same step, keeping the Law-7 keystone test green.

The guard never blocks: it mutates mechanically and exits 0. Install it by pointing git at the
tracked hooks dir once::

    git config core.hooksPath .githooks

Version format is ``v.NN`` zero-padded to two digits, extending naturally past 99 (``v.100``).
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

# The served-contract source files. A staged change to any of these is the bump trigger; the
# version + golden both live in VERSION_FILE / GOLDEN_FILE (the only two the guard ever rewrites).
VERSION_FILE = "hive/app/onboard_ref.py"
GOLDEN_FILE = "tests/app/test_onboard_ref.py"
WATCHED: tuple[str, ...] = (VERSION_FILE, "hive/domain/kinds.py")

# The single owner of the keystone bundle hash — the guard regenerates the golden by importing it,
# so the hashed-byte composition is never reproduced (and never drifts) here.
_DIGEST_ONELINER = "from hive.app.onboard_ref import bundle_digest; print(bundle_digest())"

_VERSION_RE = re.compile(r'(CONTRACT_VERSION: str = ")v\.(\d+)(")')
_GOLDEN_RE = re.compile(r'(_GOLDEN_BUNDLE_SHA256 = ")[0-9a-f]{64}(")')


# ── pure helpers (no I/O — unit-testable without a repo) ──────────────────────────────────────

def parse_version(text: str) -> int:
    """The integer N of the ``CONTRACT_VERSION: str = "v.NN"`` line. Raises if the line is absent
    (the guard's structural assumption broke — fail loud, never guess a version)."""
    m = _VERSION_RE.search(text)
    if m is None:
        raise ValueError("CONTRACT_VERSION line not found")
    return int(m.group(2))


def format_version(n: int) -> str:
    """``v.NN`` zero-padded to a 2-digit floor, extending past 99 (``v.100``, ``v.101``)."""
    return f"v.{n:02d}"


def decide(current: int, head: int) -> tuple[int, bool]:
    """The target version and whether a bump is needed: if ``current`` is already ahead of HEAD
    (a hand-bump), keep it; otherwise step to ``head + 1`` so the result is strictly greater than
    the initial (HEAD) state."""
    if current > head:
        return current, False
    return head + 1, True


def set_version_line(text: str, new_version: str) -> str:
    """Rewrite exactly the ``CONTRACT_VERSION`` line to ``new_version``; raises unless exactly one
    line matched (the version is single-sourced — zero or many matches means the file changed shape)."""
    new_text, n = _VERSION_RE.subn(rf'\g<1>{new_version}\g<3>', text)
    if n != 1:
        raise ValueError(f"expected exactly one CONTRACT_VERSION line, found {n}")
    return new_text


def set_golden_line(text: str, new_hash: str) -> str:
    """Rewrite exactly the ``_GOLDEN_BUNDLE_SHA256`` literal to ``new_hash``; raises unless exactly
    one line matched."""
    new_text, n = _GOLDEN_RE.subn(rf'\g<1>{new_hash}\g<2>', text)
    if n != 1:
        raise ValueError(f"expected exactly one _GOLDEN_BUNDLE_SHA256 line, found {n}")
    return new_text


# ── git / IO shell (thin; everything that touches the outside world) ──────────────────────────

def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)


def repo_root() -> Path:
    out = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError("not inside a git work tree")
    return Path(out.stdout.strip())


def staged_paths(root: Path) -> set[str]:
    """Repo-root-relative paths staged for the commit being created."""
    out = _git(root, "diff", "--cached", "--name-only")
    return {line for line in out.stdout.splitlines() if line}


def head_version(root: Path) -> int | None:
    """``CONTRACT_VERSION`` as committed at HEAD, or None when HEAD has no such file (first commit
    / brand-new contract — nothing to enforce monotonicity against)."""
    out = _git(root, "show", f"HEAD:{VERSION_FILE}")
    if out.returncode != 0:
        return None
    return parse_version(out.stdout)


def compute_digest(root: Path) -> str:
    """The live keystone bundle hash, computed in a FRESH interpreter so it reflects the just-written
    bytes on disk (an in-process import would read stale, already-cached module bytes)."""
    existing = os.environ.get("PYTHONPATH", "")
    pythonpath = str(root) + (os.pathsep + existing if existing else "")
    env = {**os.environ, "PYTHONPATH": pythonpath}
    out = subprocess.run([sys.executable, "-c", _DIGEST_ONELINER],
                         cwd=root, capture_output=True, text=True, env=env)
    if out.returncode != 0:
        raise RuntimeError(f"could not compute bundle digest: {out.stderr.strip()}")
    return out.stdout.strip()


def main(argv: list[str] | None = None) -> int:
    try:
        root = repo_root()
        if not (staged_paths(root) & set(WATCHED)):
            return 0  # the commit does not touch the served contract — nothing to do
        head_v = head_version(root)
        if head_v is None:
            return 0  # no committed baseline yet; the keystone test owns the golden

        version_path = root / VERSION_FILE
        original = version_path.read_text(encoding="utf-8")
        current_v = parse_version(original)
        new_v, bumped = decide(current_v, head_v)
    except Exception as exc:  # never block a commit on a guard fault
        print(f"[contract-version] skipped (guard error: {exc})", file=sys.stderr)
        return 0

    if bumped:
        version_path.write_text(set_version_line(original, format_version(new_v)), encoding="utf-8")
    try:
        digest = compute_digest(root)
        golden_path = root / GOLDEN_FILE
        golden_text = golden_path.read_text(encoding="utf-8")
        new_golden = set_golden_line(golden_text, digest)
        golden_changed = new_golden != golden_text
        if golden_changed:
            golden_path.write_text(new_golden, encoding="utf-8")
    except Exception as exc:
        if bumped:  # leave the tree exactly as the developer had it
            version_path.write_text(original, encoding="utf-8")
        print(f"[contract-version] skipped (could not regenerate golden: {exc})", file=sys.stderr)
        return 0

    changed = ([VERSION_FILE] if bumped else []) + ([GOLDEN_FILE] if golden_changed else [])
    if changed:
        _git(root, "add", *changed)
    if bumped:
        print(f"[contract-version] bumped from {format_version(current_v)} to {format_version(new_v)} "
              f"(contract changed); keystone golden regenerated")
    elif golden_changed:
        print(f"[contract-version] already at {format_version(current_v)} (> HEAD "
              f"{format_version(head_v)}); keystone golden regenerated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
