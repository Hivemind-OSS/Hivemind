"""B7: end-to-end OFFLINE smoke — the whole harness in one shot (main → HivemindBackend over the
REAL in-process server → evaluate → score → paired-CI compare → provenance report) on the tiny
fixture with ZERO API and ZERO model download. FakeProvider embedder (fast, deterministic),
VerbatimLLM extraction, Hivemind under both gates (Oracle ceiling vs AllowAll floor). It proves the
pipeline wires together and emits a valid, provenance-stamped report; retrieval QUALITY is the real
run's job (real Qwen3 + Claude extraction), not this smoke's."""
from __future__ import annotations

import json
from pathlib import Path

from hive.app.config import AutonomyConfig
from hive.research.bench.hivemind_backend import HivemindBackend
from hive.research.bench.mem0_backend import Mem0Backend
from hive.research.bench.run import VerbatimLLM, main
from hive.research.bench.token_run import ARM_MEM0, ARM_OFF, ARM_ON
from hive.research.bench.token_run import main as token_main
from tests.mcp._helpers import build_real_server
from tests.research.bench.test_mem0_backend import FakeMem0Client

_FIX = Path(__file__).parent / "fixtures" / "longmemeval_tiny.json"


def _smoke_server():
    # demand_m huge ⇒ the orchestrator commit is the SOLE path to a servable memory
    server, _clock = build_real_server(autonomy=AutonomyConfig(demand_m=10**9))
    return server


def test_e2e_offline_hivemind_oracle_vs_allowall(tmp_path):
    out = tmp_path / "smoke_report.json"
    rc = main(["--backend", "hivemind", "--gate", "oracle",
               "--baseline-backend", "hivemind", "--baseline-gate", "allowall",
               "--extractor", "verbatim", "--dataset", str(_FIX), "--out", str(out)],
              backend_factory=lambda name: HivemindBackend(_smoke_server),
              llm_factory=lambda extractor: VerbatimLLM())
    assert rc == 0 and out.exists()
    rep = json.loads(out.read_text())

    # provenance complete (build_report would have refused otherwise) + correctly stamped
    assert rep["provenance"]["n_cases"] == 3
    assert rep["provenance"]["embedder_model"] == "Qwen/Qwen3-Embedding-0.6B"
    assert rep["provenance"]["extractor"] == "verbatim" and rep["provenance"]["dataset_hash"]
    assert rep["provenance"]["llm_digest"] == "verbatim"

    # two arms, each with well-formed retrieval scores over the 2 answerable questions
    assert len(rep["arms"]) == 2
    for arm in rep["arms"].values():
        r = arm["retrieval"]
        assert r["n"] == 2 and 0.0 <= r["hit_at_k"]["5"] <= 1.0 and 0.0 <= r["coverage"] <= 1.0
        assert arm["abstention_auroc"] is None or 0.0 <= arm["abstention_auroc"] <= 1.0

    # the three comparison metrics, each with a paired CI
    assert {c["metric"] for c in rep["comparisons"]} == {"hit_at_5", "hit_at_10", "mrr"}
    for c in rep["comparisons"]:
        assert c["lo"] <= c["hi"] and isinstance(c["ships"], bool)


# ── ABSTAIN-TOKEN C5: the three-arm agent-loop token benchmark, end-to-end offline ──
def _hive_server(h: float):
    """A real in-process server at gate threshold ``h`` with the orchestrator as the SOLE
    promotion authority (demand_m huge), so only preloaded commits are servable."""
    def factory():
        server, _clock = build_real_server(h=h, autonomy=AutonomyConfig(demand_m=10**9))
        return server
    return factory


def _token_backend(arm: str):
    if arm == ARM_MEM0:
        return Mem0Backend(FakeMem0Client())
    if arm == ARM_OFF:
        return HivemindBackend(_hive_server(1.0))      # max legal threshold ⇒ no suppression
    return HivemindBackend(_hive_server(0.5))          # production gate


def test_e2e_offline_three_arm_token_report(tmp_path):
    out = tmp_path / "token_report.json"
    rc = token_main(["--dataset", str(_FIX), "--out", str(out)],
                    backend_factory=_token_backend,
                    llm_factory=lambda arm: VerbatimLLM())
    assert rc == 0 and out.exists()
    rep = json.loads(out.read_text())

    # three arms + three paired deltas; complete provenance (build_token_report would have refused)
    assert set(rep["arms"]) == {ARM_MEM0, ARM_OFF, ARM_ON}
    assert set(rep["deltas"]) == {"C-B", "B-A", "C-A"}
    prov = rep["provenance"]
    assert prov["h_frac_on"] == 0.5 and prov["h_frac_off"] == 1.0
    assert prov["embedder_model"] == "Qwen/Qwen3-Embedding-0.6B"
    assert prov["dataset_hash"] and prov["extractor"] == "substrate-raw-turns"
    # the tiny fixture carries exactly one question of each regime
    assert set(prov["regime_mix"]) == {"single-answer", "broad-relevant", "no-relevant"}

    # zero-cost plumbing: VerbatimLLM ⇒ no input tokens, no spend anywhere
    for arm in rep["arms"].values():
        assert arm["input_tokens_total"] == 0
        assert arm["usage_total"]["total_cost_usd"] == 0.0
    assert all(t == 0 for t in prov["token_totals"].values())

    # the abstain gate IS active on the real recall path: at the same store/embedder/top_n, the
    # production gate (C) can only serve a SUBSET of what the threshold-off gate (B) serves.
    assert rep["arms"][ARM_ON]["served_text_count"] <= rep["arms"][ARM_OFF]["served_text_count"]

    # every delta carries BOTH Pareto axes with a CI (tokens + success), each a valid interval
    for d in rep["deltas"].values():
        assert d["tokens"]["lo"] <= d["tokens"]["hi"]
        assert d["success"]["lo"] <= d["success"]["hi"]
        assert isinstance(d["tokens"]["saves"], bool) and isinstance(d["success"]["regresses"], bool)
