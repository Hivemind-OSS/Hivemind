"""B5a — the ONE LLM touchpoint for the whole harness. Every model call (subagent fact
extraction, the orchestrator gate, an optional QA judge) goes through ``ClaudeSubscriptionLLM``,
which shells the Claude **subscription** CLI — ``claude -p <prompt> --output-format json`` — and
NOTHING else. No raw Anthropic/OpenAI API, no SDK, no key: the only egress is the authenticated
``claude`` binary.

Deep module, narrow surface (``complete``), everything hidden behind it:
  * subprocess invocation via an INJECTABLE ``runner`` seam, so tests never touch the real CLI;
  * ``.result`` extraction with FAIL-CLOSED semantics — a non-zero exit, an ``is_error`` envelope,
    or an unparseable reply raises (never a silent empty string);
  * a content-hash CACHE so each unique (prompt, system, model) is called at most once per run;
  * a persisted JSONL replay-LOG so a re-run reproduces bit-for-bit with ZERO new calls, and a
    ``digest()`` over it stamps provenance into the report;
  * bounded retries for transient infra failures (a definitive model error is not retried);
  * ``preflight()`` — the binary is on PATH and authenticated, else fail fast (no silent default).

There is deliberately no ``max_tokens`` knob: ``claude -p`` exposes no such flag, and a parameter
that silently does nothing is a lying contract.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Protocol, runtime_checkable

_DEFAULT_TIMEOUT_S = 120


@dataclass(frozen=True)
class RunResult:
    """The outcome of one subprocess invocation — the seam between the LLM and the OS."""
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[list[str]], RunResult]


@runtime_checkable
class LLM(Protocol):
    """The minimal contract the gate / extractor / judge depend on — never the concrete class."""

    def complete(self, prompt: str, *, system: Optional[str] = None) -> str:
        ...


def _default_runner(argv: list[str]) -> RunResult:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=_DEFAULT_TIMEOUT_S)
        return RunResult(p.returncode, p.stdout, p.stderr)
    except subprocess.TimeoutExpired:
        return RunResult(124, "", f"timed out after {_DEFAULT_TIMEOUT_S}s")
    except FileNotFoundError as e:                      # binary vanished between preflight and call
        return RunResult(127, "", str(e))


class ClaudeSubscriptionLLM:
    """``text in → text out`` over ``claude -p``. The only LLM egress in the harness."""

    def __init__(self, *, runner: Optional[Runner] = None, log_path: Optional[Path | str] = None,
                 model: Optional[str] = None, bin: str = "claude", max_retries: int = 2) -> None:
        self._runner = runner or _default_runner
        self._model = model
        self._bin = bin
        self._max_retries = int(max_retries)
        self._log_path = Path(log_path) if log_path else None
        self._cache: dict[str, str] = {}
        if self._log_path and self._log_path.exists():
            self._load_log()

    # ── public surface ──────────────────────────────────────────────────────────
    def complete(self, prompt: str, *, system: Optional[str] = None) -> str:
        key = self._key(prompt, system)
        if key in self._cache:                          # replay / dedup — no call
            return self._cache[key]
        text = self._invoke(self._build_argv(prompt, system))
        self._cache[key] = text
        self._append_log(key, prompt, system, text)
        return text

    def preflight(self) -> None:
        """Fail fast unless the subscription CLI is present AND authenticated."""
        if shutil.which(self._bin) is None:
            raise FileNotFoundError(
                f"`{self._bin}` CLI not found on PATH — the bench routes every LLM call through "
                "the Claude subscription CLI (no raw API key is read)")
        try:
            self.complete("Reply with the single word: ready")
        except RuntimeError as e:
            raise RuntimeError(f"claude preflight failed (unauthenticated or CLI error): {e}") from e

    def digest(self) -> str:
        """A stable, content-sensitive hash of the call log — provenance for the report."""
        canon = json.dumps(sorted(self._cache.items()), ensure_ascii=False)
        return hashlib.sha256(canon.encode("utf-8")).hexdigest()

    # ── internals ────────────────────────────────────────────────────────────────
    def _key(self, prompt: str, system: Optional[str]) -> str:
        canon = json.dumps([prompt, system, self._model], ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(canon.encode("utf-8")).hexdigest()

    def _build_argv(self, prompt: str, system: Optional[str]) -> list[str]:
        argv = [self._bin, "-p", prompt, "--output-format", "json"]
        if system:
            argv += ["--append-system-prompt", system]
        if self._model:
            argv += ["--model", self._model]
        return argv

    def _invoke(self, argv: list[str]) -> str:
        last = "unknown error"
        for _ in range(self._max_retries + 1):
            res = self._runner(argv)
            if res.returncode != 0:                     # transient infra — retry
                last = res.stderr.strip() or res.stdout.strip() or f"exit {res.returncode}"
                continue
            try:
                env = json.loads(res.stdout)
            except (json.JSONDecodeError, ValueError):
                last = f"unparseable reply: {res.stdout[:120]!r}"
                continue
            if not isinstance(env, dict) or "result" not in env:
                last = f"reply missing 'result': {res.stdout[:120]!r}"
                continue
            if env.get("is_error") or env.get("subtype") != "success":
                raise RuntimeError(                      # definitive model error — do NOT retry
                    f"claude -p returned an error envelope (subtype={env.get('subtype')!r}, "
                    f"is_error={env.get('is_error')!r}): {env.get('result')!r}")
            return str(env["result"])
        raise RuntimeError(f"claude -p failed after {self._max_retries + 1} attempt(s): {last}")

    def _load_log(self) -> None:
        for line in self._log_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            e = json.loads(line)
            self._cache[e["key"]] = e["output"]

    def _append_log(self, key: str, prompt: str, system: Optional[str], output: str) -> None:
        if not self._log_path:
            return
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"key": key, "prompt": prompt, "system": system,
                 "model": self._model, "output": output}
        with self._log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


class FakeLLM:
    """The deterministic double for gate / extractor tests. ``responder(prompt, system) -> str``
    scripts the reply; every call is recorded in ``calls`` for assertions."""

    def __init__(self, responder: Optional[Callable[[str, Optional[str]], str]] = None) -> None:
        self._responder = responder or (lambda prompt, system: "")
        self.calls: list[tuple[str, Optional[str]]] = []

    def complete(self, prompt: str, *, system: Optional[str] = None) -> str:
        self.calls.append((prompt, system))
        return self._responder(prompt, system)
