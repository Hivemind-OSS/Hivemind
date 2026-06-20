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

# The four token counters accumulate as ints; cost accumulates as a float. Kept as a single
# tuple so the accumulate/normalize helpers and the zero shape cannot drift apart.
_TOKEN_FIELDS = ("input_tokens", "output_tokens",
                 "cache_creation_input_tokens", "cache_read_input_tokens")


def zero_usage() -> dict:
    """A costless usage snapshot — the fallback for a double lacking ``usage_totals()``.
    A FRESH dict each call (never a shared mutable constant). ``n_live_calls`` is included
    so the shape matches ``usage_totals()`` exactly."""
    return {f: 0 for f in _TOKEN_FIELDS} | {"total_cost_usd": 0.0, "n_live_calls": 0}


def zero_served_usage() -> dict:
    """A costless SERVED-usage snapshot (advances on every served result, real or replay) — the
    fallback for a double lacking ``served_usage_totals()``. ``n_served`` counts served results,
    NOT live calls (a replay/cache hit serves but spends nothing)."""
    return {f: 0 for f in _TOKEN_FIELDS} | {"total_cost_usd": 0.0, "n_served": 0}


def _zero_accum() -> dict:
    """The 5-field accumulator shape (4 token counters + cost) — no ``n_live_calls`` (that is
    counted separately so a replayed entry can add usage WITHOUT counting as a spent call)."""
    return {f: 0 for f in _TOKEN_FIELDS} | {"total_cost_usd": 0.0}


def _normalize_usage(d: Optional[dict]) -> dict:
    """Coerce a flat usage dict to the 5-field shape with numeric zero defaults — fail-OPEN
    on a missing/old-CLI field (a missing cost figure must never abort a run; only a missing
    *answer* does — that stays fail-CLOSED in ``_invoke``). The BUG-002 lesson, on the cost axis."""
    d = d or {}
    return {f: int(d.get(f, 0) or 0) for f in _TOKEN_FIELDS} | {
        "total_cost_usd": float(d.get("total_cost_usd", 0.0) or 0.0)}


def _parse_usage(env: dict) -> dict:
    """Harvest the 5-field usage from a SUCCESS envelope: token counters from ``env['usage']``,
    cost from the top-level ``env['total_cost_usd']``. Called ONLY after the error gate."""
    merged = dict(env.get("usage") or {})
    merged["total_cost_usd"] = env.get("total_cost_usd", 0.0)
    return _normalize_usage(merged)


def _add_usage_into(total: dict, delta: dict) -> None:
    """Accumulate ``delta`` (a 5-field usage dict) into ``total`` in place. // O(1)."""
    for f in _TOKEN_FIELDS:
        total[f] += int(delta.get(f, 0) or 0)
    total["total_cost_usd"] += float(delta.get("total_cost_usd", 0.0) or 0.0)


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
        # usage provenance: per-key usage (for digest + replay reproduction), a running cold-cost
        # total (real calls AND replayed entries), and a count of calls actually SPENT this
        # invocation (real calls only — a replay/cache hit costs nothing, so it never bumps).
        self._usage_by_key: dict[str, dict] = {}
        self._usage_total: dict = _zero_accum()
        self._n_live_calls: int = 0
        # the SERVED total (advances on EVERY served result — real call, cache hit, OR replay hit)
        # is the measurement axis the agent loop differences; unlike _usage_total it is NOT
        # pre-loaded from the log, so a re-run reproduces the per-call spend as it is served.
        self._served_usage: dict = _zero_accum()
        self._served_calls: int = 0
        if self._log_path and self._log_path.exists():
            self._load_log()

    # ── public surface ──────────────────────────────────────────────────────────
    def complete(self, prompt: str, *, system: Optional[str] = None) -> str:
        key = self._key(prompt, system)
        if key in self._cache:                          # replay / dedup — no live call
            _add_usage_into(self._served_usage, self._usage_by_key.get(key, {}))
            self._served_calls += 1                     # ...but the result WAS served (it has a cost)
            return self._cache[key]
        text, usage = self._invoke(self._build_argv(prompt, system))
        self._cache[key] = text
        self._usage_by_key[key] = usage
        _add_usage_into(self._usage_total, usage)
        _add_usage_into(self._served_usage, usage)
        self._n_live_calls += 1                         # a real call was spent
        self._served_calls += 1
        self._append_log(key, prompt, system, text, usage)
        return text

    def usage_totals(self) -> dict:
        """The accumulated COLD-reproduction cost (a fully-replayed run reproduces it) plus
        ``n_live_calls`` = calls actually spent THIS invocation. The two are never conflated:
        a replayed log entry adds usage but NOT a live call."""
        return {**self._usage_total, "n_live_calls": self._n_live_calls}

    def served_usage_totals(self) -> dict:
        """The usage of every SERVED result this invocation (real call, cache hit, OR replay hit) —
        the agent-loop measurement axis. Reproducible across a cold run and a log replay: the spend
        is attributed when the result is served, never pre-loaded, so a `--llm-log` re-run yields the
        SAME per-task tokens instead of zero. ``n_served`` counts served results, not live calls."""
        return {**self._served_usage, "n_served": self._served_calls}

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
        """A stable, content-sensitive hash over ``(key, output, usage)`` per entry — so a
        tampered or absent usage value CHANGES the provenance digest (cost is a reproducibility
        guarantee, not decoration). Entries sorted by key for determinism."""
        entries = [[k, self._cache[k], self._usage_by_key.get(k, {})] for k in sorted(self._cache)]
        canon = json.dumps(entries, ensure_ascii=False, sort_keys=True)
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

    def _invoke(self, argv: list[str]) -> tuple[str, dict]:
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
            # usage is harvested ONLY past the error gate — an error envelope never yields a
            # usage tuple (moving this above the raise would let a failure's tokens be counted).
            return str(env["result"]), _parse_usage(env)
        raise RuntimeError(f"claude -p failed after {self._max_retries + 1} attempt(s): {last}")

    def _load_log(self) -> None:
        for line in self._log_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            e = json.loads(line)
            self._cache[e["key"]] = e["output"]
            # a replayed entry reproduces the cold cost (accumulate usage) but spends NO live
            # call (no n_live_calls bump) — older logs lacking "usage" normalize to zeros.
            usage = _normalize_usage(e.get("usage"))
            self._usage_by_key[e["key"]] = usage
            _add_usage_into(self._usage_total, usage)

    def _append_log(self, key: str, prompt: str, system: Optional[str], output: str,
                    usage: dict) -> None:
        if not self._log_path:
            return
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"key": key, "prompt": prompt, "system": system,
                 "model": self._model, "output": output, "usage": usage}
        with self._log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


class FakeLLM:
    """The deterministic double for gate / extractor / agent-loop tests. ``responder(prompt,
    system) -> str`` scripts the reply; every call is recorded in ``calls`` for assertions.
    Optional scripted ``usage`` (a per-``complete`` usage dict, default zeros) accumulates so the
    token-attribution tests have a non-trivial spend to assert against."""

    def __init__(self, responder: Optional[Callable[[str, Optional[str]], str]] = None, *,
                 usage: Optional[dict] = None) -> None:
        self._responder = responder or (lambda prompt, system: "")
        self.calls: list[tuple[str, Optional[str]]] = []
        self._usage_per_call = _normalize_usage(usage)   # None ⇒ zeros
        self._usage_total: dict = _zero_accum()
        self._n_live_calls: int = 0

    def complete(self, prompt: str, *, system: Optional[str] = None) -> str:
        self.calls.append((prompt, system))
        _add_usage_into(self._usage_total, self._usage_per_call)
        self._n_live_calls += 1
        return self._responder(prompt, system)

    def usage_totals(self) -> dict:
        return {**self._usage_total, "n_live_calls": self._n_live_calls}

    def served_usage_totals(self) -> dict:
        # FakeLLM has no cache, so every complete is a served real call.
        return {**self._usage_total, "n_served": self._n_live_calls}
