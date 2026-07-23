"""Hermetic subprocess discipline — the package's one spawning door.

Everything the verifier executes goes through ``run_hermetic``, so the
boundedness guarantees live in exactly one place. Safe direction: a child may
be slow, spewing, or forking, and the caller's run stays bounded anyway —

- every spawn gets its own session/process group, and a timeout SIGKILLs the
  whole group and reaps it, so no grandchild outlives the call;
- output is capped *during* the read (drained and discarded past the cap), so
  a spewing child can neither exhaust memory nor deadlock on a full pipe;
- the child environment is an explicit minimal base plus the recipe's static
  additions — never an ``os.environ`` copy — so ambient operator/agent
  configuration cannot leak into child tools.

``run_hermetic`` never raises on tool failure (a missing binary becomes the
shell's exit-127 shape); it raises only on programmer error (empty argv).
Spawns are serial in v1 — there is no concurrency to cap.
"""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import IO

_CHUNK_BYTES = 65_536
# Bounded belt-and-suspenders waits after SIGKILL; never reached in practice.
_REAP_TIMEOUT_S = 10.0
_JOIN_TIMEOUT_S = 10.0
_PROBE_TIMEOUT_S = 15.0


@dataclass(frozen=True, slots=True)
class SpawnResult:
    """One completed (or killed) spawn. ``exit_code is None`` iff timed out."""

    argv: tuple[str, ...]
    exit_code: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool
    truncated: bool
    duration_s: float

    def __post_init__(self) -> None:
        if (self.exit_code is None) != self.timed_out:
            raise ValueError("exit_code must be None exactly when timed_out")
        if self.duration_s < 0:
            raise ValueError("duration_s must be non-negative")


@dataclass(frozen=True, slots=True)
class ToolPresence:
    """Probe verdict: availability and the captured version, or the reason not."""

    available: bool
    version: str
    reason: str | None

    def __post_init__(self) -> None:
        if self.available and self.reason is not None:
            raise ValueError("an available tool carries no reason")
        if not self.available and not self.reason:
            raise ValueError("an unavailable tool must say why")


class _CappedReader(threading.Thread):
    """Drains one pipe on its own thread, keeping at most ``cap`` bytes.

    Past the cap it keeps reading and discards, so the child never blocks on
    a full pipe; the flag records that the kept stream is incomplete.
    """

    def __init__(self, stream: IO[bytes], cap: int) -> None:
        super().__init__(daemon=True)
        self._stream = stream
        self._cap = cap
        self._chunks: list[bytes] = []
        self._size = 0
        self.truncated = False

    def run(self) -> None:
        try:
            while True:
                chunk = self._stream.read(_CHUNK_BYTES)
                if not chunk:
                    break
                take = min(len(chunk), self._cap - self._size)
                if take > 0:
                    self._chunks.append(chunk[:take])
                    self._size += take
                if take < len(chunk):
                    self.truncated = True
        finally:
            try:
                self._stream.close()
            except OSError:
                pass

    @property
    def data(self) -> bytes:
        return b"".join(list(self._chunks))


def _minimal_env(extra_env: tuple[tuple[str, str], ...]) -> dict[str, str]:
    env = {
        key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR") if key in os.environ
    }
    env["LANG"] = "C.UTF-8"
    env["LC_ALL"] = "C.UTF-8"
    env.update(dict(extra_env))
    return env


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    try:
        # start_new_session makes the child the leader, so pid == pgid.
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def run_hermetic(
    argv: tuple[str, ...],
    *,
    cwd: Path,
    timeout_s: float,
    extra_env: tuple[tuple[str, str], ...] = (),
    max_output_bytes: int = 10_000_000,
) -> SpawnResult:
    """Run one tool to completion under the hermetic discipline."""
    if not argv or not argv[0]:
        raise ValueError("argv must name a tool to run")

    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            list(argv),
            cwd=str(cwd),
            env=_minimal_env(extra_env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        # Tool failure, not programmer error: report the shell's 127 shape.
        return SpawnResult(
            argv=tuple(argv),
            exit_code=127,
            stdout=b"",
            stderr=f"{argv[0]}: {exc}".encode(),
            timed_out=False,
            truncated=False,
            duration_s=time.monotonic() - started,
        )

    # Both streams are PIPE, so Popen guarantees the handles exist.
    assert proc.stdout is not None and proc.stderr is not None
    out_reader = _CappedReader(proc.stdout, max_output_bytes)
    err_reader = _CappedReader(proc.stderr, max_output_bytes)
    out_reader.start()
    err_reader.start()

    timed_out = False
    try:
        exit_code: int | None = proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        exit_code = None
        _kill_group(proc)
        try:
            proc.wait(timeout=_REAP_TIMEOUT_S)  # reap; SIGKILL is not refusable
        except subprocess.TimeoutExpired:
            pass  # unreapable child (kernel-stuck); return what we have

    # Group death closes the write ends, so EOF is prompt; bounded regardless.
    out_reader.join(timeout=_JOIN_TIMEOUT_S)
    err_reader.join(timeout=_JOIN_TIMEOUT_S)

    return SpawnResult(
        argv=tuple(argv),
        exit_code=exit_code,
        stdout=out_reader.data,
        stderr=err_reader.data,
        timed_out=timed_out,
        truncated=out_reader.truncated or err_reader.truncated,
        duration_s=time.monotonic() - started,
    )


def _first_line(raw: bytes) -> str:
    for line in raw.decode("utf-8", errors="replace").splitlines():
        if line.strip():
            return line.strip()
    return ""


def probe_tool(
    probe_argv: tuple[str, ...],
    *,
    cache: MutableMapping[tuple[str, ...], ToolPresence] | None = None,
    spawn: Callable[..., SpawnResult] = run_hermetic,
) -> ToolPresence:
    """One probe is both the availability gate and the version capture.

    Cached per run under the caller's mapping so N files never re-probe the
    same tool. Probes run from the temp dir: availability must not depend on
    the repo being verified. ``spawn`` is the same injection seam the
    orchestrator exposes — a caller running against a spawn double must have
    its probes answered by that double too, never by the real PATH.
    """
    key = tuple(probe_argv)
    if cache is not None:
        hit = cache.get(key)
        if hit is not None:
            return hit

    result = spawn(key, cwd=Path(tempfile.gettempdir()), timeout_s=_PROBE_TIMEOUT_S)
    if result.timed_out:
        presence = ToolPresence(
            available=False, version="", reason=f"probe timed out: {' '.join(key)}"
        )
    elif result.exit_code != 0:
        detail = _first_line(result.stderr) or _first_line(result.stdout)
        presence = ToolPresence(
            available=False,
            version="",
            reason=f"probe exit {result.exit_code}: {detail or ' '.join(key)}",
        )
    else:
        presence = ToolPresence(
            available=True,
            version=_first_line(result.stdout) or _first_line(result.stderr),
            reason=None,
        )

    if cache is not None:
        cache[key] = presence
    return presence
