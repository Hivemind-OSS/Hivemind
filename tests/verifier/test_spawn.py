"""Contracts over the hermetic spawn door — real subprocesses, all bounded.

The whole package executes through run_hermetic, so this suite proves the
boundedness claims against live children: a timeout SIGKILLs the entire
process group (grandchildren included) and reaps it, the child environment is
an explicit minimal base that ambient variables cannot leak into, and a
spewing child is truncated at the cap without deadlocking. Every wait in
here is bounded — a hang IS the failure signal.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from hive.verifier.spawn import SpawnResult, ToolPresence, probe_tool, run_hermetic

_SHORT = 10.0  # generous ceiling for quick children; never reached when green


def _wait_until_dead(pid: int, deadline_s: float = 5.0) -> bool:
    """Bounded poll: True once the pid no longer exists."""
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


# --- happy path -----------------------------------------------------------------


def test_happy_path_captures_streams_and_exit(tmp_path: Path) -> None:
    res = run_hermetic(
        ("sh", "-c", "echo out; echo err 1>&2; exit 3"), cwd=tmp_path, timeout_s=_SHORT
    )
    assert res.exit_code == 3
    assert res.stdout == b"out\n"
    assert res.stderr == b"err\n"
    assert res.timed_out is False
    assert res.truncated is False
    assert 0.0 <= res.duration_s < _SHORT
    assert res.argv == ("sh", "-c", "echo out; echo err 1>&2; exit 3")


def test_empty_argv_is_a_programmer_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        run_hermetic((), cwd=tmp_path, timeout_s=_SHORT)
    with pytest.raises(ValueError):
        run_hermetic(("",), cwd=tmp_path, timeout_s=_SHORT)


def test_missing_binary_never_raises(tmp_path: Path) -> None:
    res = run_hermetic(("hive-no-such-tool-xyz",), cwd=tmp_path, timeout_s=_SHORT)
    assert res.exit_code == 127
    assert res.timed_out is False
    assert b"hive-no-such-tool-xyz" in res.stderr


# --- the group kill (the BUG-007 door) --------------------------------------------


def test_timeout_kills_the_whole_process_group(tmp_path: Path) -> None:
    pidfile = tmp_path / "grandchild.pid"
    started = time.monotonic()
    res = run_hermetic(
        ("sh", "-c", f"(sleep 30 & echo $! > {pidfile}); sleep 30"),
        cwd=tmp_path,
        timeout_s=0.5,
    )
    wall = time.monotonic() - started
    assert res.timed_out is True
    assert res.exit_code is None
    assert wall < 5.0, f"timeout return took {wall:.1f}s — the kill is not prompt"
    assert pidfile.exists(), "fixture bug: grandchild pid was never written"
    grandchild = int(pidfile.read_text().strip())
    assert _wait_until_dead(grandchild), (
        f"grandchild {grandchild} survived the group kill — children are leaking"
    )


# --- env minimalism ------------------------------------------------------------


def test_ambient_environment_does_not_leak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HIVE_CANARY", "1")
    res = run_hermetic(("env",), cwd=tmp_path, timeout_s=_SHORT)
    text = res.stdout.decode()
    assert res.exit_code == 0
    assert "HIVE_CANARY=" not in text, "ambient env leaked into the child"
    assert "PATH=" in text
    assert "LANG=C.UTF-8" in text
    assert "LC_ALL=C.UTF-8" in text


def test_extra_env_is_delivered(tmp_path: Path) -> None:
    res = run_hermetic(
        ("sh", "-c", 'printf %s "$HIVE_EXTRA"'),
        cwd=tmp_path,
        timeout_s=_SHORT,
        extra_env=(("HIVE_EXTRA", "forty-two"),),
    )
    assert res.exit_code == 0
    assert res.stdout == b"forty-two"


# --- output caps -----------------------------------------------------------------


def test_output_cap_truncates_and_child_still_completes(tmp_path: Path) -> None:
    res = run_hermetic(
        ("sh", "-c", "yes abcdefgh | head -c 200000"),
        cwd=tmp_path,
        timeout_s=_SHORT,
        max_output_bytes=10_000,
    )
    # exit 0 proves the child ran to completion: past the cap the stream is
    # drained and discarded, never left to block the child on a full pipe.
    assert res.exit_code == 0
    assert res.truncated is True
    assert len(res.stdout) == 10_000
    assert res.stdout.startswith(b"abcdefgh")


def test_small_output_is_not_truncated(tmp_path: Path) -> None:
    res = run_hermetic(
        ("sh", "-c", "printf hello"), cwd=tmp_path, timeout_s=_SHORT, max_output_bytes=10_000
    )
    assert res.truncated is False
    assert res.stdout == b"hello"


# --- carrier invariants ------------------------------------------------------------


def test_spawn_result_exit_code_none_iff_timed_out() -> None:
    with pytest.raises(ValueError):
        SpawnResult(
            argv=("x",), exit_code=None, stdout=b"", stderr=b"",
            timed_out=False, truncated=False, duration_s=0.1,
        )
    with pytest.raises(ValueError):
        SpawnResult(
            argv=("x",), exit_code=0, stdout=b"", stderr=b"",
            timed_out=True, truncated=False, duration_s=0.1,
        )


def test_tool_presence_unavailable_requires_reason() -> None:
    with pytest.raises(ValueError):
        ToolPresence(available=False, version="", reason=None)
    with pytest.raises(ValueError):
        ToolPresence(available=True, version="1.0", reason="should be None")


# --- probing ------------------------------------------------------------------------


def _write_tool(path: Path, body: str) -> str:
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)
    return str(path)


def test_probe_captures_version_first_line(tmp_path: Path) -> None:
    tool = _write_tool(tmp_path / "toolx", 'echo "toolx 9.9.9"; echo "extra line"')
    presence = probe_tool((tool,))
    assert presence.available is True
    assert presence.version == "toolx 9.9.9"
    assert presence.reason is None


def test_probe_missing_tool_is_unavailable_with_reason(tmp_path: Path) -> None:
    presence = probe_tool(("hive-no-such-tool-xyz", "--version"))
    assert presence.available is False
    assert presence.version == ""
    assert presence.reason and "hive-no-such-tool-xyz" in presence.reason


def test_probe_failing_tool_is_unavailable_with_reason(tmp_path: Path) -> None:
    tool = _write_tool(tmp_path / "broken", "echo nope 1>&2; exit 2")
    presence = probe_tool((tool, "--version"))
    assert presence.available is False
    assert presence.reason and "2" in presence.reason


def test_probe_rides_an_injected_spawn_seam(tmp_path: Path) -> None:
    # The orchestrator injects its spawn double here too: a probe answered by
    # the injected callable must never touch the real filesystem PATH.
    def fake_spawn(argv, *, cwd, timeout_s, extra_env=(), **kwargs):
        return SpawnResult(
            argv=tuple(argv), exit_code=0, stdout=b"fake 9.9\n", stderr=b"",
            timed_out=False, truncated=False, duration_s=0.0,
        )

    presence = probe_tool(("hive-no-such-tool-xyz", "--version"), spawn=fake_spawn)
    assert presence.available is True
    assert presence.version == "fake 9.9"


def test_probe_cache_hit_avoids_the_second_spawn(tmp_path: Path) -> None:
    counter = tmp_path / "spawn.count"
    tool = _write_tool(
        tmp_path / "counted", f'echo run >> "{counter}"; echo "counted 1.0"'
    )
    cache: dict[tuple[str, ...], ToolPresence] = {}
    first = probe_tool((tool,), cache=cache)
    second = probe_tool((tool,), cache=cache)
    assert first == second
    assert first.available is True
    assert counter.read_text().count("run") == 1, "cache miss: the probe spawned twice"
