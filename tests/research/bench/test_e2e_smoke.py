"""B7: end-to-end OFFLINE smoke — the whole harness in one shot (main → HivemindBackend over the
REAL in-process server → evaluate → score → paired-CI compare → provenance report) on the tiny
fixture with ZERO API and ZERO model download. FakeProvider embedder (fast, deterministic),
VerbatimLLM extraction, Hivemind under both gates (Oracle ceiling vs AllowAll floor). It proves the
pipeline wires together and emits a valid, provenance-stamped report; retrieval QUALITY is the real
run's job (real bge + Claude extraction), not this smoke's."""
from __future__ import annotations

import json
from pathlib import Path

from hive.app.config import AutonomyConfig
from hive.research.bench.hivemind_backend import HivemindBackend
from hive.research.bench.run import VerbatimLLM, main
from tests.mcp._helpers import build_real_server

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
    assert rep["provenance"]["embedder_model"] == "BAAI/bge-small-en-v1.5"
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
