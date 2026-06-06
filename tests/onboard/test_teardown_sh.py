"""P1.12 / M07 — teardown.sh: reversible mv-not-rm decommission of the OLD system.

Runs the REAL teardown.sh against a SANDBOX HOME (tmp_path) only. A ``systemctl`` PATH
stub shadows the real binary so the test can NEVER disable/stop the live `cortex-*`
units (systemctl --user targets the real user manager regardless of $HOME) — the
"no live teardown" hold is structurally honored. Skips if bash/jq are unavailable.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
TEARDOWN = REPO / "teardown.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("jq") is None,
    reason="teardown.sh requires bash + jq")

_UNITS = ["cortex-consolidate", "cortex-report", "cortex-backup",
          "cortex-maintain", "cortex-label"]


def _sandbox(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    (home / "cortex").mkdir(parents=True)
    (home / "cortex" / "shared.db").write_text("PRECIOUS-DATA")
    (home / ".claude").mkdir(parents=True)
    units_dir = home / ".config" / "systemd" / "user"
    units_dir.mkdir(parents=True)
    for u in _UNITS:
        (units_dir / f"{u}.timer").write_text("[Timer]\n")
        (units_dir / f"{u}.service").write_text("[Service]\n")
    settings = {"hooks": {"PostToolUse": [
        {"hooks": [{"command": "/home/u/.claude/hooks/cortex_write_commit.sh"}]},
        {"hooks": [{"command": "python ~/.claude/tools/groundcheck/hook.py"}]},
        {"hooks": [{"command": "git-ai-prepare-commit"}]},
        # a BENIGN third-party hook whose path merely CONTAINS "cortex" mid-component —
        # must survive (the broad-substring strip over-matched and removed it).
        {"hooks": [{"command": "python /home/u/projects/data-cortex/run.py"}]},
    ]}}
    (home / ".claude" / "settings.json").write_text(json.dumps(settings, indent=2))

    # systemctl stub: shadows the real binary so --user calls NEVER hit live units.
    binstub = tmp_path / "bin"
    binstub.mkdir()
    stub = binstub / "systemctl"
    stub.write_text("#!/usr/bin/env bash\nexit 0\n")
    stub.chmod(0o755)
    return home


def _run(home: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ,
               HOME=str(home),
               PATH=f"{home.parent / 'bin'}:{os.environ['PATH']}")
    return subprocess.run(["bash", str(TEARDOWN), *args],
                          env=env, capture_output=True, text=True)


def _hook_commands(home: Path) -> list[str]:
    settings = json.loads((home / ".claude" / "settings.json").read_text())
    return [h["command"]
            for grp in settings.get("hooks", {}).values()
            for entry in grp
            for h in entry.get("hooks", [])]


def test_teardown_archives_not_deletes(tmp_path):
    home = _sandbox(tmp_path)
    r = _run(home)
    assert r.returncode == 0, r.stderr
    assert not (home / "cortex").exists()                  # moved out of place
    archives = list(home.glob("cortex.archived-*"))
    assert len(archives) == 1
    assert (archives[0] / "shared.db").read_text() == "PRECIOUS-DATA"   # mv, not rm


def test_teardown_removes_all_units(tmp_path):
    home = _sandbox(tmp_path)
    _run(home)
    remaining = list((home / ".config" / "systemd" / "user").glob("cortex-*"))
    assert remaining == []                                 # all 10 unit files gone


def test_teardown_strips_only_cortex_hooks(tmp_path):
    home = _sandbox(tmp_path)
    _run(home)
    cmds = _hook_commands(home)
    assert not any("cortex_write_commit" in c for c in cmds)   # the real cortex hook stripped
    assert any("groundcheck" in c for c in cmds)               # groundcheck survives
    assert any("git-ai" in c for c in cmds)                    # git-ai survives


def test_teardown_does_not_over_match_cortex_substring(tmp_path):
    # the over-match regression: a benign hook whose path contains "cortex" mid-component
    # (data-cortex) must be PRESERVED — only cortex_*/cortex-* artifacts are stripped.
    home = _sandbox(tmp_path)
    _run(home)
    cmds = _hook_commands(home)
    assert any("data-cortex" in c for c in cmds)               # benign cortex-substring survives
    assert not any("cortex_write_commit" in c for c in cmds)   # but the real cortex hook is gone


def test_teardown_dry_run_no_mutation(tmp_path):
    home = _sandbox(tmp_path)
    _run(home, "--dry-run")
    assert (home / "cortex").exists()                      # nothing archived
    assert list((home / ".config" / "systemd" / "user").glob("cortex-*"))  # units intact
    assert any("cortex" in c for c in _hook_commands(home))                # hooks intact


def test_teardown_restore_round_trips(tmp_path):
    home = _sandbox(tmp_path)
    _run(home)
    assert not (home / "cortex").exists()
    r = _run(home, "--restore")
    assert r.returncode == 0, r.stderr
    assert (home / "cortex" / "shared.db").read_text() == "PRECIOUS-DATA"   # reversible


def test_teardown_idempotent_on_rerun(tmp_path):
    # second run (cortex already archived, units already absent) must exit clean
    home = _sandbox(tmp_path)
    assert _run(home).returncode == 0
    assert _run(home).returncode == 0                      # idempotent partial-state re-run
