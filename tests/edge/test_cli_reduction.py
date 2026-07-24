"""The CLI reduction contract: census/audit left in 0.7.0 (the census engines live server-side
now), the `upgrade` verb left in 0.9.0 (uv owns install/update — `uv tool install git+…` /
`uv tool upgrade hive-edge`; the CLI self-upgrade from PyPI is dead, our dists are never
published there), and the agent-side `hook` + `worktree-delta` verbs left post-U4/U5 (the
client-side hooks that drove them are deleted). The three surviving verbs (mint / verify /
graph) keep their exact pre-reduction behavior (pinned in depth by the untouched pre-existing
suites; this module pins the SURFACE: what the verb tree offers and what it refuses).
"""

from __future__ import annotations

import importlib
import json

import pytest

from hive.edge import cli
from hive.edge.cli import main

SURVIVING_VERBS = ("mint", "verify", "graph")
REMOVED_VERBS = ("census", "audit", "upgrade", "hook", "worktree-delta")


def _run(argv, capsys):
    code = main(argv)
    cap = capsys.readouterr()
    try:
        parsed = json.loads(cap.out)
    except json.JSONDecodeError:
        parsed = cap.out
    return code, parsed, cap.err


# ── the verb tree: survivors listed, the removed verbs absent everywhere ────────


def test_top_level_help_lists_survivors_and_no_removed_verbs(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for verb in SURVIVING_VERBS:
        assert verb in out, verb
    for verb in REMOVED_VERBS:
        assert verb not in out, verb


def test_census_verb_is_an_unknown_command(capsys):
    # No dispatch, no stub: `census` is an ordinary unknown command — argparse's
    # invalid-choice usage error (SystemExit 2), never a live verb.
    with pytest.raises(SystemExit) as exc:
        main(["census", "build", "--repo", "/x", "--base", "b", "--head", "h"])
    assert exc.value.code == 2
    assert "census" in capsys.readouterr().err


def test_census_help_is_gone_too(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["census", "--help"])
    assert exc.value.code == 2


def test_audit_verb_is_an_unknown_command(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["audit", "--records", "records.json", "--repo", "/x"])
    assert exc.value.code == 2
    assert "audit" in capsys.readouterr().err


def test_upgrade_verb_is_an_unknown_command(capsys):
    # uv owns install/update now (`uv tool upgrade hive-edge`); the CLI self-upgrade is
    # gone the same way census/audit went: an ordinary unknown command, never a live verb.
    with pytest.raises(SystemExit) as exc:
        main(["upgrade", "--version", "0.8.0"])
    assert exc.value.code == 2
    assert "upgrade" in capsys.readouterr().err


# ── the D4-removed agent-side verbs: hook + worktree-delta now exit 2 too ────────


def test_hook_verb_is_an_unknown_command(capsys):
    # D4: the client-side hooks that drove `hook` are deleted; the verb joins census/
    # audit/upgrade as an ordinary argparse invalid-choice error (SystemExit 2).
    with pytest.raises(SystemExit) as exc:
        main(["hook", "pre-capture"])
    assert exc.value.code == 2
    assert "hook" in capsys.readouterr().err


def test_worktree_delta_verb_is_an_unknown_command(capsys):
    # D4: worktree-delta (the session work-delta baseline/check) left with the hooks —
    # no dispatch, no stub: an unknown command, exit 2.
    with pytest.raises(SystemExit) as exc:
        main(["worktree-delta", "--repo", "/x", "--session-id", "S", "--check"])
    assert exc.value.code == 2
    assert "worktree-delta" in capsys.readouterr().err


def test_removed_symbols_are_actually_gone():
    # The deletions are real deletions, not just unreferenced dispatch entries.
    for name in (
        "_cmd_census",
        "_CENSUS_USAGE",
        "_cmd_audit",
        "_cmd_upgrade",
        "_cmd_hook",
        "_wd_state",
    ):
        assert not hasattr(cli, name), name
    # The lifecycle module died with the upgrade verb (cli.py owns CONFIG_DIR itself now),
    # and the Claude-Code hook adapters died with the `hook` verb (D4).
    for dead in ("hive.edge.launch", "hive.edge.hooks"):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(dead)


# ── --version: unchanged (edge header + the two surviving engine stamps) ─────────


def test_version_flag_unchanged_names_only_surviving_engines(capsys):
    code = main(["--version"])
    out = capsys.readouterr().out
    assert code == 0
    assert "hive-edge " in out
    assert "combdrift" in out and "matrix" in out
    assert "census" not in out and "verifier" not in out


# ── surviving agent verbs: the U3-shaped outputs still come back ─────────────────


def test_mint_and_verify_still_produce_u3_shaped_output(py_repo, capsys):
    code, minted, _ = _run(
        ["mint", "--repo", str(py_repo), "--anchor", "pkg/f.py:foo"], capsys
    )
    assert code == 0
    assert set(minted.keys()) == {"combdrift/fp", "matrix/subgraph_fp"}
    code, verdict, _ = _run(
        [
            "verify",
            "--repo",
            str(py_repo),
            "--anchor",
            "pkg/f.py:foo",
            "--fp",
            minted["combdrift/fp"],
        ],
        capsys,
    )
    assert code == 0
    assert verdict == {"verdict": "current", "reason": "ok"}


def test_graph_still_dispatches(py_repo, capsys):
    # graph survives the reduction: `graph fp` mints the neighborhood token. The
    # worktree-delta half of the old dispatch test is gone — that verb now exits 2.
    code, out, _ = _run(
        ["graph", "fp", "--repo", str(py_repo), "--anchor", "pkg/f.py:foo"], capsys
    )
    assert code == 0
    assert out["matrix/subgraph_fp"].startswith("matrix-subgraph-fp/1:")
