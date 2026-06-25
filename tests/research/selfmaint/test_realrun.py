"""P4 — the NO-API smoke for the real-run glue (no ``claude -p``, no real Qwen3, hermetic).

Validates ``realrun.make_realrun_seams`` WITHOUT spending tokens or loading the real model: the
``daemon_factory`` builds a real in-process loopback Hivemind daemon over ``build_real_server``'s
``FakeProvider`` (NOT Qwen3), driven by a scripted ``McpHttpClient`` sequence (write → recall →
outcome) instead of a real agent turn, and the ``outcome_fn`` runs real pytest against a real build
dir holding a hand-placed kvstore impl. It pins four glue contracts:

  1. the ``_Recorder`` reconciles SERVED episode_ids from a real recall response AND HURT
     episode_ids from a real outcome request (the diagnostic observation source);
  2. ``outcome_fn`` runs the task's gated acceptance test against the arm's build dir — passing
     impl ⇒ True, broken impl ⇒ False (the executed-test gold);
  3. the MANIFEST guard RAISES loudly when the frozen ``acceptance/`` suite is edited (the fleet
     must never touch the gold);
  4. ``_parse_hurt_evidence`` extracts the agent's ``HURT_EVIDENCE`` reply block into
     ``(episode_id, reason)`` pairs (the artifact-grounded reason a flagged memory is wrong),
     defensively — a missing/garbled block ⇒ ``()`` ⇒ the gate falls back to its default note.

Everything is under tmp dirs: the real Benchmark substrate + acceptance are copied into a tmp
``benchmark_dir`` so the per-arm build copies land in tmp, never beside the real repo.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from hive.research.selfmaint.daemon import McpHttpClient
from hive.research.selfmaint.run import Arm
from tests.fakes._fakes import FakeProvider
from tests.mcp._helpers import build_real_server

from ._selfmaint_helpers import loopback_server
from .realrun import _Recorder, _parse_hurt_evidence, make_realrun_seams, make_isolation_config_dir

_REAL_BENCHMARK = "/home/null/Desktop/work/Benchmark"

# a minimal in-memory KVStore that satisfies the FROZEN T1 acceptance gold (set/get/delete, the
# KeyError contract, ValueError on an empty key / max_keys). The path + max_keys kwargs are
# accepted; persistence is unneeded for T1 (every test uses one live instance).
_PASSING_KVSTORE = '''\
class KVStore:
    def __init__(self, path=None, *, max_keys=None):
        self._d = {}
        self._max = max_keys

    def set(self, key, value, ttl=None):
        if not key:
            raise ValueError("empty key")
        if (key not in self._d and self._max is not None
                and len(self._d) >= self._max):
            raise ValueError("max_keys exceeded")
        self._d[key] = value

    def get(self, key):
        if key not in self._d:
            raise KeyError(key)
        return self._d[key]

    def delete(self, key):
        if key not in self._d:
            raise KeyError(key)
        del self._d[key]

    def keys(self, prefix=""):
        return sorted(k for k in self._d if k.startswith(prefix))
'''

# breaks ONLY the missing-key contract (get returns None instead of raising) — the bad fact's
# false_value, the same mutation the substrate records — so T1's gate goes red.
_BROKEN_KVSTORE = _PASSING_KVSTORE.replace(
    "        if key not in self._d:\n            raise KeyError(key)\n        return self._d[key]",
    "        return self._d.get(key)")


@pytest.fixture()
def benchmark_dir(tmp_path) -> str:
    """A tmp copy of the real Benchmark repo (substrate + frozen acceptance + manifest), so the
    glue's per-arm build copies land under tmp and the real repo is never written."""
    dest = tmp_path / "Benchmark"
    shutil.copytree(_REAL_BENCHMARK, dest,
                    ignore=shutil.ignore_patterns(".git", "__pycache__"))
    return str(dest)


def _seams(benchmark_dir):
    return make_realrun_seams(benchmark_dir=benchmark_dir, embedder=FakeProvider(d=64),
                              isolation_config_dir="/unused/in/this/smoke")


def _place_kvstore(build_dir: str, source: str) -> None:
    pkg = Path(build_dir) / "kvstore"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text(source, encoding="utf-8")


# ── (1) the recorder reconciles served + hurt episode_ids from real MCP traffic ──────
def test_recorder_reconciles_served_and_hurt_from_real_traffic(benchmark_dir):
    seams = _seams(benchmark_dir)
    arm = Arm("maintenance_off", retire=False)
    clients = seams["daemon_factory"](arm)
    try:
        seat = clients.seat_client("seat-1")
        eid = seat.write("the parse function takes two positional arguments",
                         approved_by="orchestrator")["id"]
        rec = seat.recall("the parse function takes two positional arguments")
        assert any(h["episode_id"] == eid for h in rec["reference_context"])   # exact text ⇒ served
        seat.outcome(hurt=[eid])

        entries = seams["state"][arm.name].recorder.entries
        served = [e for e in entries if e.tool == "hive_recall"]
        hurt = [e for e in entries if e.tool == "hive_outcome"]
        assert served and eid in served[-1].served_eids        # served-id from the recall RESPONSE
        assert hurt and eid in hurt[-1].hurt_eids              # hurt-id from the outcome REQUEST
        assert served[-1].seat == "seat-1"                     # the calling identity is recorded
    finally:
        clients.teardown()


def test_recorder_observes_without_altering_the_reply(benchmark_dir):
    # the wrapper is O7-safe: it returns the server's reply verbatim (an abstain stays an abstain).
    seams = _seams(benchmark_dir)
    arm = Arm("maintenance_off", retire=False)
    clients = seams["daemon_factory"](arm)
    try:
        rec = clients.seat_client("seat-1").recall("nothing has been written to this store yet")
        assert rec["abstained"] is True and rec["reference_context"] == []
    finally:
        clients.teardown()


# ── (2) outcome_fn runs the gated acceptance test against the arm's build dir ─────────
def test_outcome_fn_passes_on_a_correct_impl_and_fails_on_a_broken_one(benchmark_dir):
    seams = _seams(benchmark_dir)
    arm = Arm("maintenance_off", retire=False)
    clients = seams["daemon_factory"](arm)
    try:
        build_dir = seams["state"][arm.name].build_dir
        t1 = next(t for t in seams["repo_window"].tasks if t.task_id == "T1")

        _place_kvstore(build_dir, _PASSING_KVSTORE)
        assert seams["outcome_fn"](t1, arm=arm) is True        # the frozen T1 gate passes

        _place_kvstore(build_dir, _BROKEN_KVSTORE)
        assert seams["outcome_fn"](t1, arm=arm) is False       # the missing-key contract is broken
    finally:
        clients.teardown()


# ── (3) the MANIFEST guard RAISES when the frozen acceptance suite is edited ──────────
def test_outcome_fn_raises_when_acceptance_gold_is_tampered(benchmark_dir):
    seams = _seams(benchmark_dir)
    arm = Arm("maintenance_off", retire=False)
    clients = seams["daemon_factory"](arm)
    try:
        build_dir = seams["state"][arm.name].build_dir
        _place_kvstore(build_dir, _PASSING_KVSTORE)
        t1 = next(t for t in seams["repo_window"].tasks if t.task_id == "T1")
        # edit a frozen acceptance file in the build copy → its hash drifts from the manifest.
        gold = Path(build_dir) / "acceptance" / "test_task1.py"
        gold.write_text(gold.read_text(encoding="utf-8") + "\n# fleet tampered with the gold\n",
                        encoding="utf-8")
        with pytest.raises(RuntimeError, match="TAMPERED|gold"):
            seams["outcome_fn"](t1, arm=arm)
    finally:
        clients.teardown()


# ── the isolation config dir is clean (auth artifact in, no CLAUDE.md, hook-less) ─────
def test_make_isolation_config_dir_is_clean(tmp_path):
    dest = make_isolation_config_dir(str(tmp_path / "cfgdir"))
    d = Path(dest)
    assert (d / ".credentials.json").exists()                  # subscription auth preserved
    assert (d / "settings.json").read_text().strip() == "{}"   # hook-less settings
    assert not (d / "CLAUDE.md").exists()                      # no ambient global instructions


# ── (4) the HURT_EVIDENCE reply block parses to (episode_id, reason) pairs ────────────
def test_parse_hurt_evidence_extracts_id_and_reason_pairs():
    # the agent ends its reply with a HURT_EVIDENCE block; the parser yields each (eid, reason).
    # Mutation: drop the reason capture (return only the id) → this reds on the reason assertion.
    text = (
        "I built the task and it passes.\n\n"
        "HURT_EVIDENCE:\n"
        "- id=7: it claims get returns None but the verified acceptance test requires KeyError\n"
        "- id=12: says max_keys is ignored but the spec raises ValueError on overflow\n")
    assert _parse_hurt_evidence(text) == (
        (7, "it claims get returns None but the verified acceptance test requires KeyError"),
        (12, "says max_keys is ignored but the spec raises ValueError on overflow"))


def test_parse_hurt_evidence_no_block_yields_empty():
    # a reply with no HURT_EVIDENCE block ⇒ no evidence ⇒ the gate falls back to the default note.
    assert _parse_hurt_evidence("I built the task. All acceptance tests pass. Nothing misled me.") == ()


def test_parse_hurt_evidence_is_defensive_on_garbled_lines():
    # a missing/garbled line under the marker contributes nothing (no crash) — only well-formed
    # `id=<int>: <reason>` lines survive (BUG-002/003 defensive parse).
    text = (
        "HURT_EVIDENCE:\n"
        "- id=: no id here\n"               # no integer ⇒ skipped
        "- garbled line with no id marker\n"
        "- id=9: a real reason that survives\n")
    assert _parse_hurt_evidence(text) == ((9, "a real reason that survives"),)


# ── (5) the recorder observes the agent's own hive_prune, status-GATED (the autonomous arm) ──
def _recorder_daemon(*, agi_mode: bool):
    """A recorder-wrapped real Hivemind daemon behind real loopback HTTP at the given AGI_MODE — the
    same wrap ``realrun.daemon_factory`` applies. Returns ``(client, recorder, stop)``."""
    server, _clock = build_real_server(d=64, tau_serve=0.30, k_min=1, agi_mode=agi_mode)
    recorder = _Recorder(server.handle)
    server.handle = recorder.handle
    url, stop = loopback_server(server)
    return McpHttpClient(url, agent_id="seat-1"), recorder, stop


def test_recorder_observes_agent_prune_only_when_the_daemon_honors_it():
    # Under AGI_MODE the agent's own AGI_OVERRIDE prune is HONORED ⇒ the recorder records the
    # pruned episode_id (the autonomous arm's retirement). Under AGI_MODE OFF the same prune is
    # REFUSED fail-closed (status != "pruned") ⇒ the recorder records NO pruned eid.
    # Mutation: record the pruned eid regardless of reply status → the refused-prune assertion reds.

    # honored: AGI_MODE on
    client, rec, stop = _recorder_daemon(agi_mode=True)
    try:
        eid = client.write("the parse function takes two positional arguments",
                           approved_by="human")["id"]
        res = client.prune(eid, approved_by="AGI_OVERRIDE")
        assert res["status"] == "pruned"                       # the daemon honors AGI_OVERRIDE
        pruned = [e for entry in rec.entries for e in entry.pruned_eids]
        assert pruned == [eid]                                 # observed the agent's own retirement
    finally:
        stop()

    # refused: AGI_MODE off ⇒ fail-closed gate + status-gated observe ⇒ nothing recorded
    client, rec, stop = _recorder_daemon(agi_mode=False)
    try:
        eid = client.write("the parse function takes two positional arguments",
                           approved_by="human")["id"]
        res = client.prune(eid, approved_by="AGI_OVERRIDE")
        assert res["status"] != "pruned"                       # fail-closed: AGI_OVERRIDE rejected
        pruned = [e for entry in rec.entries for e in entry.pruned_eids]
        assert pruned == []                                    # a refused prune retires nothing
    finally:
        stop()
