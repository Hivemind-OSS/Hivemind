"""B5a: the single LLM seam. ClaudeSubscriptionLLM shells `claude -p ... --output-format json`
through an INJECTABLE runner (so no test ever calls the real CLI), parses `.result`, dedups via a
content-hash cache, persists a JSONL replay-log (re-runs replay → bit-for-bit, zero new calls),
and fails CLOSED on a bad exit / error envelope / unparseable reply. No raw API: the only egress is
the `claude` subscription binary. FakeLLM is the deterministic double the gate/extractor tests use."""
from __future__ import annotations

import json

import pytest

from hive.research.bench.llm import LLM, ClaudeSubscriptionLLM, FakeLLM, RunResult


def _envelope(result: str, *, is_error: bool = False, subtype: str = "success") -> str:
    return json.dumps({"type": "result", "subtype": subtype, "is_error": is_error,
                       "result": result, "session_id": "x"})


class _Runner:
    """A scripted runner: records every argv, returns queued RunResults (last repeats)."""
    def __init__(self, *results: RunResult) -> None:
        self.calls: list[list[str]] = []
        self._results = list(results) or [RunResult(0, _envelope("ok"), "")]

    def __call__(self, argv: list[str]) -> RunResult:
        self.calls.append(list(argv))
        return self._results[min(len(self.calls) - 1, len(self._results) - 1)]


# ── Protocol conformance ───────────────────────────────────────────────────────
def test_both_satisfy_the_llm_protocol():
    assert isinstance(FakeLLM(), LLM)
    assert isinstance(ClaudeSubscriptionLLM(runner=_Runner()), LLM)


# ── argv construction (the subprocess contract) ────────────────────────────────
def test_complete_builds_headless_json_argv():
    r = _Runner(RunResult(0, _envelope("hi"), ""))
    out = ClaudeSubscriptionLLM(runner=r).complete("a question")
    assert out == "hi"
    argv = r.calls[0]
    assert argv[:2] == ["claude", "-p"] and "a question" in argv
    assert argv[argv.index("--output-format") + 1] == "json"
    assert "--append-system-prompt" not in argv          # none supplied


def test_system_and_model_flags_are_passed_when_set():
    r = _Runner(RunResult(0, _envelope("hi"), ""))
    ClaudeSubscriptionLLM(runner=r, model="claude-opus-4-8").complete("q", system="be terse")
    argv = r.calls[0]
    assert argv[argv.index("--append-system-prompt") + 1] == "be terse"
    assert argv[argv.index("--model") + 1] == "claude-opus-4-8"


# ── fail-closed parsing ────────────────────────────────────────────────────────
def test_nonzero_exit_raises_with_stderr():
    r = _Runner(RunResult(1, "", "boom: not logged in"))
    with pytest.raises(RuntimeError, match="boom: not logged in"):
        ClaudeSubscriptionLLM(runner=r, max_retries=0).complete("q")


def test_error_envelope_raises():
    r = _Runner(RunResult(0, _envelope("nope", is_error=True, subtype="error_during_execution"), ""))
    with pytest.raises(RuntimeError, match="error_during_execution|is_error"):
        ClaudeSubscriptionLLM(runner=r, max_retries=0).complete("q")


def test_unparseable_stdout_raises():
    r = _Runner(RunResult(0, "this is not json", ""))
    with pytest.raises(RuntimeError):
        ClaudeSubscriptionLLM(runner=r, max_retries=0).complete("q")


# ── content-hash cache (each unique prompt called once) ────────────────────────
def test_identical_prompt_calls_runner_once():
    r = _Runner(RunResult(0, _envelope("cached"), ""))
    llm = ClaudeSubscriptionLLM(runner=r)
    assert llm.complete("same") == "cached" and llm.complete("same") == "cached"
    assert len(r.calls) == 1                             # second served from cache


def test_distinct_prompts_each_call_runner():
    r = _Runner(RunResult(0, _envelope("a"), ""), RunResult(0, _envelope("b"), ""))
    llm = ClaudeSubscriptionLLM(runner=r)
    assert llm.complete("one") == "a" and llm.complete("two") == "b"
    assert len(r.calls) == 2


def test_system_prompt_is_part_of_the_cache_key():
    r = _Runner(RunResult(0, _envelope("x"), ""), RunResult(0, _envelope("y"), ""))
    llm = ClaudeSubscriptionLLM(runner=r)
    llm.complete("q"); llm.complete("q", system="different")
    assert len(r.calls) == 2                             # system varies the key ⇒ not a cache hit


# ── persisted replay-log (determinism + provenance) ────────────────────────────
def test_log_replays_across_instances_without_recalling(tmp_path):
    log = tmp_path / "calls.jsonl"
    r1 = _Runner(RunResult(0, _envelope("first-run"), ""))
    ClaudeSubscriptionLLM(runner=r1, log_path=log).complete("q")
    assert len(r1.calls) == 1 and log.exists()
    # a fresh instance pointed at the same log REPLAYS — the runner is never invoked
    r2 = _Runner(RunResult(0, _envelope("SHOULD-NOT-BE-CALLED"), ""))
    llm2 = ClaudeSubscriptionLLM(runner=r2, log_path=log)
    assert llm2.complete("q") == "first-run" and len(r2.calls) == 0


def test_digest_is_stable_and_content_sensitive(tmp_path):
    r = _Runner(RunResult(0, _envelope("a"), ""), RunResult(0, _envelope("b"), ""))
    llm = ClaudeSubscriptionLLM(runner=r, log_path=tmp_path / "l.jsonl")
    llm.complete("one")
    d1 = llm.digest()
    assert llm.digest() == d1                            # stable when nothing changed
    llm.complete("two")
    assert llm.digest() != d1                            # a new call changes the provenance digest


# ── bounded retries (transient infra) ──────────────────────────────────────────
def test_retries_a_transient_failure_then_succeeds():
    r = _Runner(RunResult(1, "", "transient"), RunResult(0, _envelope("recovered"), ""))
    assert ClaudeSubscriptionLLM(runner=r, max_retries=1).complete("q") == "recovered"
    assert len(r.calls) == 2


def test_gives_up_after_max_retries():
    r = _Runner(RunResult(1, "", "always down"))
    with pytest.raises(RuntimeError, match="always down"):
        ClaudeSubscriptionLLM(runner=r, max_retries=2).complete("q")
    assert len(r.calls) == 3                             # initial + 2 retries


# ── preflight (present + authenticated; no silent default) ─────────────────────
def test_preflight_raises_when_binary_absent(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: None)
    with pytest.raises(FileNotFoundError, match="claude"):
        ClaudeSubscriptionLLM(runner=_Runner()).preflight()


def test_preflight_passes_with_binary_and_good_probe(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/claude")
    ClaudeSubscriptionLLM(runner=_Runner(RunResult(0, _envelope("ready"), ""))).preflight()


def test_preflight_raises_on_unauthenticated_probe(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/claude")
    r = _Runner(RunResult(1, "", "Invalid API key / not logged in"))
    with pytest.raises(RuntimeError, match="not logged in|preflight"):
        ClaudeSubscriptionLLM(runner=r, max_retries=0).preflight()


# ── FakeLLM (the deterministic double) ─────────────────────────────────────────
def test_fakellm_scripts_responses_and_records_calls():
    llm = FakeLLM(lambda prompt, system: f"echo:{prompt}")
    assert llm.complete("hello") == "echo:hello"
    assert llm.calls == [("hello", None)]


def test_fakellm_default_is_empty_string():
    assert FakeLLM().complete("anything") == ""
