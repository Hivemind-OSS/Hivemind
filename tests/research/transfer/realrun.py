"""Real-run glue for the knowledge-transfer benchmark (Chunk 7, OUTSIDE the purity fence).

Wires the black-box ``hive.research.transfer`` runner to real ``claude -p`` agents talking to an
IN-PROCESS loopback Hivemind daemon (``build_real_server`` behind a real ``ThreadingHTTPServer``).
It lives under ``tests/`` because it imports ``build_real_server`` + the shipped ``_build_handler``,
which the purity fence forbids inside ``hive/research/transfer/``. The harness stays a black-box
client; this glue supplies its four injection seams.

``make_transfer_realrun_seams`` returns ``{transfer_window, daemon_factory, agent_turn, vouch_fn,
outcome_fn, state}``:

* ``daemon_factory(arm)`` — a fresh loopback daemon (autonomy on, low ``demand_m`` so the
  demand_promote arm fires within its fan-out), its ``handle`` wrapped by a recorder that OBSERVES
  every ``hive_capture`` text (Phase A) and ``hive_recall`` served text (Phase B). O7-safe: it
  observes, never steers.
* ``agent_turn(seat, task, client)`` — one real ``claude -p`` turn in the pair's upstream/downstream
  build dir under the isolated config dir. recall_off withholds ``hive_recall`` from ``--allowedTools``
  (the only cross-arm difference on the headline contrast); the oracle arm injects the fact into the
  prompt. Captured/served are reconciled from the recorder.
* ``vouch_fn(orchestrator_client, captured)`` — the orchestrator establishes each captured fact
  servable via ``hive_write(approved_by=...)`` (the reliable promotion path).
* ``outcome_fn(pair, *, arm, ...)`` — the executed HIDDEN gold: it REGENERATES the pair's hidden
  acceptance test fresh (the in-memory substrate is the frozen source — tamper-proof by
  construction, the fleet never sees it) and runs it against the reader's ``solution.py`` via
  ``PYTHONPATH``.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Optional

from hive.app.config import AutonomyConfig
from hive.app.http_server import _build_handler
from hive.research.bench.llm import RunResult
from hive.research.selfmaint.agent import BUILD_AGENT_TOOLS, run_agent_turn
from hive.research.selfmaint.daemon import McpHttpClient
from hive.research.transfer.run import DEFAULT_ARMS, ArmClients, TransferTurnObs, run_transfer
from hive.research.transfer.substrate import load_transfer_window
from tests.mcp._helpers import build_real_server
from tests.research.selfmaint.realrun import make_isolation_config_dir

_AGENT_TIMEOUT_S = 600
_PYTEST_TIMEOUT_S = 120
_RECALL_TOOL = "mcp__hive__hive_recall"


@dataclass(frozen=True)
class _RecEntry:
    """One observed ``tools/call``: a ``hive_capture`` text (Phase A) or the ``hive_recall`` served
    texts (Phase B). All default empty — an inert call under-claims rather than fabricating."""
    seat: str
    tool: str
    captured_texts: tuple = ()
    served_texts: tuple = ()


class _Recorder:
    """Wraps a server's ``handle`` to RECORD every ``tools/call`` without altering the reply. Defensive
    (BUG-002/003 class): a malformed envelope contributes nothing rather than raising. O7-safe."""

    def __init__(self, handle) -> None:
        self._handle = handle
        self.entries: list[_RecEntry] = []

    def handle(self, req, *, identity=None):
        resp = self._handle(req, identity=identity)
        try:
            if getattr(req, "method", None) == "tools/call":
                params = req.params if isinstance(req.params, dict) else {}
                tool = params.get("name")
                args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
                seat = str(getattr(identity, "agent_id", "local"))
                if tool == "hive_capture":
                    txt = args.get("text")
                    if isinstance(txt, str) and txt:
                        self.entries.append(_RecEntry(seat, "hive_capture", captured_texts=(txt,)))
                elif tool == "hive_recall":
                    self.entries.append(
                        _RecEntry(seat, "hive_recall", served_texts=self._served_texts(resp)))
        except Exception:                       # observation must never break a real turn
            pass
        return resp

    @staticmethod
    def _served_texts(resp) -> tuple:
        try:
            payload = json.loads(resp.result["content"][0]["text"])
        except (TypeError, KeyError, IndexError, ValueError):
            return ()
        out = []
        for hit in (payload.get("reference_context") or []):
            t = hit.get("text") if isinstance(hit, dict) else None
            if isinstance(t, str) and t:
                out.append(t)
        return tuple(out)


@dataclass
class _ArmState:
    work: str
    url: str
    recorder: _Recorder
    mcp_config_path: str
    httpd: ThreadingHTTPServer
    thread: threading.Thread


def _downstream_tools(can_recall: bool) -> tuple:
    """The reader's tool scope. recall_off withholds ``hive_recall`` (the headline lever) — it can
    still build, but it physically cannot recall the vouched fact."""
    if can_recall:
        return BUILD_AGENT_TOOLS
    return tuple(t for t in BUILD_AGENT_TOOLS if t != _RECALL_TOOL)


def _upstream_prompt(pair) -> str:
    return (
        f"You are a coding agent working in the CURRENT directory. {pair.upstream_prompt}\n\n"
        "Write your implementation to solution.py and make the visible test pass: run "
        "`python -m pytest test_upstream.py -q` with the Bash tool until it is green.\n"
        "Then record the durable fact you learned — the exact contract the test pinned — for your "
        "teammates by calling hive_capture with a single clear sentence. Capture the truth, never a "
        "guess.")


def _downstream_prompt(pair, *, can_recall: bool, oracle_value: Optional[str]) -> str:
    oracle_line = (f"A teammate has told you: {pair.fact.carrier_text}\n" if oracle_value is not None
                   else "")
    recall_line = (
        "FIRST call hive_recall with a query about the contract this task needs, to pull any note a "
        "teammate left; treat a recalled note as reference.\n" if can_recall else "")
    return (
        f"You are a coding agent working in the CURRENT directory. {pair.downstream_prompt}\n\n"
        f"{oracle_line}{recall_line}"
        "Write your implementation to solution.py and make the visible test pass: run "
        "`python -m pytest test_downstream.py -q` with the Bash tool. The visible test does not pin "
        "every detail of the contract, so implement it faithfully — there are additional hidden "
        "checks on the exact behavior.")


def make_transfer_realrun_seams(*, benchmark_dir: str, seed: int, embedder,
                                isolation_config_dir: str, model: Optional[str] = None,
                                demand_m: int = 2, work_base: Optional[str] = None) -> dict:
    """Build the real-run seams over the ``../Benchmark/transfer`` generator at ``seed``. ``embedder.d``
    sizes the daemon index; ``demand_m`` is the (low) promotion threshold for the demand arm."""
    window = load_transfer_window(_load_generated_window(benchmark_dir, seed))
    base = Path(work_base) if work_base else Path(benchmark_dir).parent
    state: dict[str, _ArmState] = {}
    url_to_arm: dict[str, str] = {}

    def daemon_factory(arm) -> ArmClients:
        work = base / f"transfer_{arm.name}"
        if work.exists():
            shutil.rmtree(work)
        work.mkdir(parents=True)
        server, _clock = build_real_server(
            d=int(embedder.d), embedder=embedder, tau_serve=0.30, k_min=1,
            autonomy=AutonomyConfig(demand_m=demand_m))
        recorder = _Recorder(server.handle)
        server.handle = recorder.handle
        httpd = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            _build_handler(server, lambda _tok: None, threading.Lock(), auth_required=False))
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{httpd.server_address[1]}/mcp"
        mcp_config_path = str(work / ".hive.mcp.json")
        Path(mcp_config_path).write_text(
            json.dumps({"mcpServers": {"hive": {"type": "http", "url": url}}}), encoding="utf-8")
        state[arm.name] = _ArmState(str(work), url, recorder, mcp_config_path, httpd, thread)
        url_to_arm[url] = arm.name

        def teardown() -> None:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

        return ArmClients(
            seat_client=lambda seat: McpHttpClient(url, agent_id=seat),
            orchestrator_client=McpHttpClient(url, agent_id="orchestrator"),
            teardown=teardown)

    def agent_turn(seat, task, client) -> TransferTurnObs:
        st = state[url_to_arm[client.url]]
        pair = task.pair
        if task.role == "upstream":
            bdir = Path(st.work) / pair.pair_id / "upstream"
            bdir.mkdir(parents=True, exist_ok=True)
            (bdir / "test_upstream.py").write_text(pair.upstream_test, encoding="utf-8")
            prompt, tools = _upstream_prompt(pair), BUILD_AGENT_TOOLS
        else:
            bdir = Path(st.work) / pair.pair_id / "downstream"
            bdir.mkdir(parents=True, exist_ok=True)
            (bdir / "test_downstream.py").write_text(pair.downstream_test, encoding="utf-8")
            prompt = _downstream_prompt(pair, can_recall=task.can_recall,
                                        oracle_value=task.oracle_value)
            tools = _downstream_tools(task.can_recall)

        mark = len(st.recorder.entries)

        def runner(argv: list[str]) -> RunResult:
            env = {**os.environ, "CLAUDE_CONFIG_DIR": isolation_config_dir}
            p = subprocess.run(argv, capture_output=True, text=True, timeout=_AGENT_TIMEOUT_S,
                               cwd=str(bdir), env=env)
            return RunResult(p.returncode, p.stdout, p.stderr)

        obs = run_agent_turn(seat, prompt, mcp_config_path=st.mcp_config_path, agent_id=seat,
                             allowed_tools=tools, model=model, runner=runner)
        captured, served = [], []
        for e in st.recorder.entries[mark:]:
            captured.extend(e.captured_texts)
            served.extend(e.served_texts)
        return TransferTurnObs(seat=seat, captured_texts=tuple(captured), served_texts=tuple(served),
                               result_text=obs.result_text)

    def vouch_fn(orch_client, captured) -> dict:
        out: dict = {}
        for pid, texts in captured.items():
            for t in texts:
                if isinstance(t, str) and t.strip():
                    orch_client.write(t, approved_by="orchestrator")
            if texts:
                out[pid] = texts[0]
        return out

    def outcome_fn(pair, *, arm, served_values=None, **_kw) -> bool:
        st = state[arm.name]
        ddir = Path(st.work) / pair.pair_id / "downstream"
        hdir = Path(st.work) / pair.pair_id / "_hidden"          # outside the agent's cwd
        if hdir.exists():
            shutil.rmtree(hdir)
        hdir.mkdir(parents=True)
        (hdir / "hidden_test.py").write_text(pair.hidden_test, encoding="utf-8")
        env = {**os.environ, "PYTHONPATH": str(ddir)}
        p = subprocess.run([sys.executable, "-m", "pytest", str(hdir / "hidden_test.py"), "-q"],
                           capture_output=True, text=True, timeout=_PYTEST_TIMEOUT_S, env=env,
                           cwd=str(hdir))
        return p.returncode == 0

    return {"transfer_window": window, "daemon_factory": daemon_factory, "agent_turn": agent_turn,
            "vouch_fn": vouch_fn, "outcome_fn": outcome_fn, "state": state}


def _load_generated_window(benchmark_dir: str, seed: int) -> dict:
    """Load the out-of-tree ``../Benchmark/transfer`` generator and call ``transfer_window(seed)``
    by PATH (its ``substrate`` module name collides with the package's)."""
    import importlib.util
    path = str(Path(benchmark_dir) / "substrate.py")
    spec = importlib.util.spec_from_file_location("_transfer_bench_substrate", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_transfer_bench_substrate"] = mod
    spec.loader.exec_module(mod)
    return mod.transfer_window(seed)


def run_real_transfer(*, benchmark_dir: str, out: str, seed: int = 0, arms=DEFAULT_ARMS,
                      model: Optional[str] = None, demand_m: int = 2,
                      isolation_config_dir: Optional[str] = None,
                      work_base: Optional[str] = None) -> dict:
    """Execute the REAL knowledge-transfer benchmark: load the shipping Qwen3 embedder, drive real
    ``claude -p`` build agents over an in-process loopback daemon, and write the provenance-stamped
    report to ``out``. Requires an authenticated ``claude`` (subscription) and the model weights.
    Spends subscription tokens. The embedder import is lazy (heavy/torch)."""
    from hive.adapters.embedding.factory import native_dim_for
    from hive.adapters.embedding.local_st import DEFAULT_MODEL, LocalSTEmbedder

    embedder = LocalSTEmbedder(d=native_dim_for(DEFAULT_MODEL), model_name=DEFAULT_MODEL).load()
    iso = isolation_config_dir or make_isolation_config_dir(
        str(Path(out).resolve().parent / ".transfer_cfg"))
    seams = make_transfer_realrun_seams(
        benchmark_dir=benchmark_dir, seed=seed, embedder=embedder, isolation_config_dir=iso,
        model=model, demand_m=demand_m, work_base=work_base)
    report = run_transfer(
        arms=arms, transfer_window=seams["transfer_window"], seed=seed,
        daemon_factory=seams["daemon_factory"], agent_turn=seams["agent_turn"],
        vouch_fn=seams["vouch_fn"], outcome_fn=seams["outcome_fn"],
        model=model or "unknown", demand_m=demand_m)
    Path(out).write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


__all__ = ["make_transfer_realrun_seams", "make_isolation_config_dir", "run_real_transfer"]


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Real knowledge-transfer benchmark run (BENCHMARK §7).")
    ap.add_argument("--benchmark-dir", required=True, help="path to ../Benchmark/transfer")
    ap.add_argument("--out", required=True, help="report JSON output path")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--model", default=None, help="claude model id (the agent tier)")
    ap.add_argument("--demand-m", type=int, default=2)
    _a = ap.parse_args()
    _rep = run_real_transfer(benchmark_dir=_a.benchmark_dir, out=_a.out, seed=_a.seed,
                             model=_a.model, demand_m=_a.demand_m)
    _d = _rep["deltas"]
    print(json.dumps({"headline_success_verdict": _d.get("success"),
                      "success_ci": _d.get("success_ci"),
                      "n_valid_pairs": _rep["necessity"].get("n_valid")}, indent=2))
