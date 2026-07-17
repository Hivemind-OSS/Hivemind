"""Pre-commit guard that keeps the agent-contract version monotonic with the INSTALLED bundle.

The version gates what agents install and hold a version-stamped copy of — the rules block, the
claude hooks, and the auto-approve allowlist (``onboard_ref.bundle_digest``). When a commit
changes those bytes this guard bumps ``CONTRACT_VERSION`` past HEAD (so a connected agent whose
installed marker drifts re-onboards) and regenerates the ``_GOLDEN_BUNDLE_SHA256`` keystone golden
in the same step (Law 1/7) — a bundle edit can't land at an equal/stale version.

A staged edit that leaves the bundle byte-identical to HEAD is NOT a version event: serving-side
text re-fetched fresh at onboard — the install procedure, ``REMEDIATION_NOTICE``, the
mint/verify/census directives — can't drift on the agent side, so editing it leaves the version at
HEAD's. The trigger is the bundle digest, not the mere fact that ``onboard_ref.py`` was touched;
the compare reads HEAD's golden (== HEAD's ``bundle_digest``) so the version stamp normalizes away
when the tree is still at HEAD's version (see ``main``).

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
_GOLDEN_VALUE_RE = re.compile(r'_GOLDEN_BUNDLE_SHA256 = "([0-9a-f]{64})"')


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


def needs_bump(*, bundle_changed: bool, current: int, head: int) -> tuple[int, bool]:
    """Target version + whether to bump, GATED on a real bundle change. A staged edit that leaves
    the installed bundle byte-identical (a serving-side-text-only edit) is never a version event —
    keep the current version. Otherwise step per ``decide`` (a hand-bump ahead of HEAD is kept)."""
    if not bundle_changed:
        return current, False
    return decide(current, head)


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


def golden_at_head(root: Path) -> str | None:
    """The ``_GOLDEN_BUNDLE_SHA256`` literal committed at HEAD — equal to HEAD's ``bundle_digest()``
    since the keystone test is green there — or None when HEAD lacks the line. The guard compares
    the live digest against this to tell a bundle change from a serving-side-text edit."""
    out = _git(root, "show", f"HEAD:{GOLDEN_FILE}")
    if out.returncode != 0:
        return None
    m = _GOLDEN_VALUE_RE.search(out.stdout)
    return m.group(1) if m else None


def compute_digest(root: Path) -> str:
    """The live keystone bundle hash, computed in a FRESH interpreter so it reflects the just-written
    bytes on disk (an in-process import would read stale, already-cached module bytes). Runs under
    ``-B`` (no bytecode cache): the guard imports onboard_ref twice around a SAME-SIZE version bump
    (``v.NN`` -> ``v.NN+1``), and a written ``.pyc`` would be reloaded stale when the two writes land
    on one mtime tick, yielding the pre-bump digest."""
    existing = os.environ.get("PYTHONPATH", "")
    pythonpath = str(root) + (os.pathsep + existing if existing else "")
    env = {**os.environ, "PYTHONPATH": pythonpath}
    out = subprocess.run([sys.executable, "-B", "-c", _DIGEST_ONELINER],
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
        # Bump only on a real bundle change. HEAD's golden IS HEAD's bundle_digest; when the tree is
        # still at HEAD's version the stamp is identical on both sides, so an equal live digest means
        # a serving-side-text-only edit (procedure / directives / REMEDIATION_NOTICE) that must NOT
        # bump. A missing golden or an already-hand-bumped version falls through to ``decide``.
        head_golden = golden_at_head(root)
        live_digest = compute_digest(root)
        bundle_changed = (head_golden is None or current_v != head_v or live_digest != head_golden)
        new_v, bumped = needs_bump(bundle_changed=bundle_changed, current=current_v, head=head_v)
        if not bumped and not bundle_changed:
            return 0  # serving-side-text-only edit — bundle unchanged, version stays at HEAD's
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
              f"(bundle changed); keystone golden regenerated")
    elif golden_changed:
        print(f"[contract-version] already at {format_version(current_v)} (> HEAD "
              f"{format_version(head_v)}); keystone golden regenerated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
