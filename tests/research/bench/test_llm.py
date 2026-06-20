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


# ── usage capture (ABSTAIN-TOKEN C1: real agent spend, not a proxy) ─────────────
def _env_usage(result: str, *, input_tokens: int = 0, output_tokens: int = 0,
               cache_creation: int = 0, cache_read: int = 0, cost: float = 0.0,
               is_error: bool = False, subtype: str = "success") -> str:
    """An envelope carrying a `usage` block + top-level `total_cost_usd` (the real shape)."""
    return json.dumps({
        "type": "result", "subtype": subtype, "is_error": is_error, "result": result,
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens,
                  "cache_creation_input_tokens": cache_creation,
                  "cache_read_input_tokens": cache_read},
        "total_cost_usd": cost, "session_id": "x"})


def test_usage_accumulates_across_real_calls():
    r = _Runner(RunResult(0, _env_usage("a", input_tokens=10, output_tokens=3, cost=0.001), ""),
                RunResult(0, _env_usage("b", input_tokens=20, output_tokens=5, cost=0.002), ""))
    llm = ClaudeSubscriptionLLM(runner=r)
    llm.complete("one"); llm.complete("two")
    u = llm.usage_totals()
    assert u["input_tokens"] == 30 and u["output_tokens"] == 8
    assert u["total_cost_usd"] == pytest.approx(0.003)
    assert u["n_live_calls"] == 2


def test_cache_hit_adds_no_usage_and_no_live_call():
    r = _Runner(RunResult(0, _env_usage("cached", input_tokens=10, cost=0.5), ""))
    llm = ClaudeSubscriptionLLM(runner=r)
    llm.complete("same"); llm.complete("same")            # second served from cache
    assert len(r.calls) == 1
    u = llm.usage_totals()
    assert u["input_tokens"] == 10 and u["total_cost_usd"] == pytest.approx(0.5)
    assert u["n_live_calls"] == 1                          # a cache hit spends nothing


def test_missing_usage_envelope_yields_zeros_no_raise():
    # the legacy envelope (no `usage` block) must accumulate zeros, NEVER crash (BUG-002 class)
    r = _Runner(RunResult(0, _envelope("ok"), ""))
    llm = ClaudeSubscriptionLLM(runner=r)
    assert llm.complete("q") == "ok"
    u = llm.usage_totals()
    assert u["input_tokens"] == 0 and u["total_cost_usd"] == 0.0 and u["n_live_calls"] == 1


def test_error_envelope_with_usage_still_raises_and_harvests_nothing():
    # usage is parsed ONLY after the error gate — an error envelope never yields a usage tuple
    r = _Runner(RunResult(0, _env_usage("nope", input_tokens=999, cost=9.0,
                                        is_error=True, subtype="error_during_execution"), ""))
    llm = ClaudeSubscriptionLLM(runner=r, max_retries=0)
    with pytest.raises(RuntimeError, match="error_during_execution|is_error"):
        llm.complete("q")
    assert llm.usage_totals()["input_tokens"] == 0        # nothing harvested from a failure
    assert llm.usage_totals()["total_cost_usd"] == 0.0


def test_replay_log_reproduces_usage_total_without_recalling(tmp_path):
    log = tmp_path / "calls.jsonl"
    r1 = _Runner(RunResult(0, _env_usage("first", input_tokens=42, output_tokens=7, cost=0.01), ""))
    ClaudeSubscriptionLLM(runner=r1, log_path=log).complete("q")
    # a fresh instance replays the log — runner never called, cold-cost total reproduced
    r2 = _Runner(RunResult(0, _env_usage("SHOULD-NOT", input_tokens=999), ""))
    llm2 = ClaudeSubscriptionLLM(runner=r2, log_path=log)
    assert llm2.complete("q") == "first" and len(r2.calls) == 0
    u = llm2.usage_totals()
    assert u["input_tokens"] == 42 and u["output_tokens"] == 7
    assert u["total_cost_usd"] == pytest.approx(0.01)
    assert u["n_live_calls"] == 0                          # replay spends nothing


def _digest_for_input_tokens(input_tokens: int) -> str:
    r = _Runner(RunResult(0, _env_usage("out", input_tokens=input_tokens), ""))
    llm = ClaudeSubscriptionLLM(runner=r)
    llm.complete("q")
    return llm.digest()


def test_digest_covers_usage_so_a_tampered_cost_changes_provenance():
    # same prompt + same output, different usage ⇒ different provenance digest
    assert _digest_for_input_tokens(10) != _digest_for_input_tokens(20)
    assert _digest_for_input_tokens(10) == _digest_for_input_tokens(10)   # stable when equal


def test_fakellm_usage_totals_default_zero_and_scripted():
    assert FakeLLM().usage_totals() == {
        "input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0, "total_cost_usd": 0.0, "n_live_calls": 0}
    f = FakeLLM(usage={"input_tokens": 5, "output_tokens": 2, "total_cost_usd": 0.001})
    f.complete("a"); f.complete("b")
    u = f.usage_totals()
    assert u["input_tokens"] == 10 and u["output_tokens"] == 4
    assert u["total_cost_usd"] == pytest.approx(0.002) and u["n_live_calls"] == 2


def test_zero_usage_helper_is_a_fresh_full_shape():
    from hive.research.bench.llm import zero_usage
    z = zero_usage()
    assert z == {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0,
                 "cache_read_input_tokens": 0, "total_cost_usd": 0.0, "n_live_calls": 0}
    z["input_tokens"] = 99
    assert zero_usage()["input_tokens"] == 0              # fresh dict each call (no shared mutation)


# ── served usage (the MEASUREMENT axis: advances on EVERY served result) ────────
# usage_totals() is the cold-reproduction cost (cache/replay hit adds nothing); served_usage_totals()
# is what the agent loop measures — it must advance on a cache OR replay hit too, else a re-run from
# the log (or a within-arm duplicate) would report ZERO injected tokens for the served call.
def test_served_usage_counts_a_within_arm_cache_hit_unlike_cold_usage():
    r = _Runner(RunResult(0, _env_usage("x", input_tokens=10, cost=0.5), ""))
    llm = ClaudeSubscriptionLLM(runner=r)
    llm.complete("same"); llm.complete("same")           # 1 real call + 1 cache hit
    assert llm.usage_totals()["input_tokens"] == 10      # cold: the unique call only
    sv = llm.served_usage_totals()
    assert sv["input_tokens"] == 20 and sv["n_served"] == 2   # served: BOTH serves counted
    assert sv["total_cost_usd"] == pytest.approx(1.0)


def test_served_usage_reproduces_on_a_replay_run(tmp_path):
    log = tmp_path / "calls.jsonl"
    ClaudeSubscriptionLLM(runner=_Runner(RunResult(0, _env_usage("first", input_tokens=42), "")),
                          log_path=log).complete("q")
    # a fresh instance over the same log: served is ZERO until it serves, then reproduces the spend
    llm2 = ClaudeSubscriptionLLM(runner=_Runner(RunResult(0, _env_usage("X", input_tokens=999), "")),
                                 log_path=log)
    assert llm2.served_usage_totals()["input_tokens"] == 0      # nothing served yet
    assert llm2.complete("q") == "first"                         # replay hit (no runner call)
    assert llm2.served_usage_totals()["input_tokens"] == 42      # served spend reproduced on replay
    assert llm2.served_usage_totals()["n_served"] == 1
    assert llm2.usage_totals()["n_live_calls"] == 0              # ...but no live call was spent


def test_fakellm_served_usage_tracks_completes():
    f = FakeLLM(usage={"input_tokens": 3})
    f.complete("a"); f.complete("b")
    sv = f.served_usage_totals()
    assert sv["input_tokens"] == 6 and sv["n_served"] == 2
