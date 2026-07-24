"""BUG-036 part 2, end-to-end: a comment-only edit must NOT false-stale a memory anchored at a
symbol on a single-source-root corpus whose qualified import does not resolve.

The user-facing symptom of the incremental-closure name-drop: on a `src/pkg/**` layout the
inferred extraction root is `<repo>/src/pkg`, so `from pkg.a import f` never resolves and the
scratch build binds `b.py:g --calls--> a.py:f` purely by NAME. When a comment-only edit to b.py
triggers an INCREMENTAL graph refresh, the dropped edge would move the anchor's
dependency-neighborhood fingerprint and the recall seam would surface a FALSE "dependency
neighborhood changed — re-verify" radius advisory over an untouched neighborhood. The
name-closure re-seeds the callee file, so the fp holds and the recall stays quiet.

Post-U5 the client-side hooks are gone; the same regression is driven through the surviving CLI
seam the sync service actually shells — `mint` lands the persistent graph cache at capture, and
`verify --subgraph-fp <stored>` recomputes the anchor's neighborhood against an INCREMENTAL
`_load_graph` refresh (the exact entry point the deleted post-recall hook used) — asserting the
verdict stays current with radius `current` (no false re-verify advisory).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from hive.edge import cli
from hive.edge.cli import main

_ANCHOR = "src/pkg/b.py:g"


def _write_flat_pkg_repo(root: Path) -> Path:
    """ALL source under `src/pkg/` (pads included), so matrix infers `src/pkg` as the extraction
    root and `from pkg.a import f` does not resolve — b.py:g calls a.py:f by name only. The pads
    keep the corpus on the incremental merge path, so the refresh exercises the closure, not the
    full-rebuild escape."""
    pkg = root / "src" / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (pkg / "b.py").write_text(
        "from pkg.a import f\n\ndef g():\n    return f()\n", encoding="utf-8"
    )
    for i in (1, 2, 3, 4):
        (pkg / f"pad{i}.py").write_text(
            f"def pad{i}():\n    return {i}\n", encoding="utf-8"
        )
    return root


def _run(argv, capsys):
    """Invoke the in-repo CLI, returning (exit_code, parsed_stdout_json_or_raw)."""
    code = main(argv)
    cap = capsys.readouterr()
    try:
        return code, json.loads(cap.out)
    except json.JSONDecodeError:
        return code, cap.out


def test_comment_edit_does_not_false_stale_pkg_anchor(tmp_path, monkeypatch, capsys):
    # Isolate the persistent per-checkout graph cache under a tmp HIVE_EDGE_HOME (the matrix
    # artifact dir hangs off cli.CONFIG_DIR/state), so this test never touches the real state dir.
    monkeypatch.setattr(cli, "CONFIG_DIR", tmp_path / "edge-home")
    repo = _write_flat_pkg_repo(tmp_path / "repo")

    # Mint the anchor's fingerprint union at capture — the first graph build pays the full build
    # and lands the persistent cache/manifest the incremental refresh reads.
    code, minted = _run(["mint", "--repo", str(repo), "--anchor", _ANCHOR], capsys)
    assert code == 0
    assert minted.get("matrix/subgraph_fp"), (
        "fixture drift: anchor did not mint a subgraph fp"
    )

    # A comment-only edit to b.py: g still calls f and f is untouched, so the anchor's dependency
    # neighborhood is UNCHANGED — a from-scratch graph would mint the identical fp.
    b = repo / "src" / "pkg" / "b.py"
    b.write_text(
        "from pkg.a import f\n\n# note\ndef g():\n    return f()\n", encoding="utf-8"
    )
    st = b.stat()
    os.utime(b, (st.st_atime, st.st_mtime + 10))

    # The recall seam recomputes the anchor fp through the real _load_graph (an INCREMENTAL
    # matrix.update) and must find NO neighborhood drift.
    argv = [
        "verify",
        "--repo",
        str(repo),
        "--anchor",
        _ANCHOR,
        "--subgraph-fp",
        minted["matrix/subgraph_fp"],
    ]
    if (fp := minted.get("combdrift/fp")) is not None:
        argv += ["--fp", fp]
    code, out = _run(argv, capsys)
    assert code == 0
    # RED pre-fix: the dropped b_g->a_f moves the recomputed fp -> radius "changed", a false
    # re-verify advisory over an untouched neighborhood. Post-fix the closure re-seeds the callee.
    assert out["verdict"] == "current"
    assert out.get("radius") == "current"
