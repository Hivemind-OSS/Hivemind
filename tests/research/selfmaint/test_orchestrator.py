"""S4 — the gated-approval reviewer + the §10 O7 firewall.

The orchestrator reads the real worklist (``hive_health(include_conflicts,
include_suspect_consensus)``), asks a ``claude -p`` JUDGMENT which surfaced candidates to retire,
and issues the ``approved_by`` retirements for ONLY the ones the judgment chose — declining the
rest (a declined candidate is a measured judgment). The load-bearing property (§6): the harness
applies NO mechanical auto-prune; the retirement set is gated by the LLM decision, never by the
mere presence of a candidate. A pinned mutation that auto-approves every candidate reds the
firewall test. The AGI arm only flips the ``approved_by`` stamp to the byte-distinguishable
``AGI_OVERRIDE`` (AC3).
"""
from __future__ import annotations

import json

from hive.app.config import ConflictConfig
from hive.research.bench.llm import FakeLLM
from hive.research.selfmaint.daemon import McpHttpClient
from hive.research.selfmaint.orchestrator import OrchestratorObs, run_orchestrator_review
from tests.fakes._fakes import FakeClusterProvider

from ._selfmaint_helpers import live_daemon

_WORKLIST = {
    "conflicts": [{"a_id": 1, "b_id": 2, "loser_hint": 1, "relation": "redundant",
                   "note": "near-duplicate; 1 is the retire candidate"}],
    "suspect_consensus": [{"episode_id": 3, "note": "thin effective-independence demand"}],
}


class _FakeClient:
    """Records the privileged calls and returns a canned worklist — lets the orchestrator's
    JUDGMENT-gated behaviour be pinned without engineering real near-dups."""
    def __init__(self, worklist: dict) -> None:
        self._worklist = worklist
        self.prunes: list = []
        self.supersedes: list = []

    def health(self, **kw) -> dict:
        return {"ok": True, **self._worklist}

    def prune(self, episode_id, *, approved_by) -> dict:
        self.prunes.append((episode_id, approved_by))
        return {"status": "pruned", "episode_id": episode_id, "approved_by": approved_by}

    def supersede(self, loser, winner, *, approved_by) -> dict:
        self.supersedes.append((loser, winner, approved_by))
        return {"status": "superseded", "loser": loser, "winner": winner}


def _llm_deciding(decisions: list) -> FakeLLM:
    return FakeLLM(lambda prompt, system: json.dumps({"decisions": decisions}))


# ── the §10 O7 firewall: retirement is gated by the JUDGMENT, never by candidate presence ──
def test_orchestrator_retires_only_what_the_judgment_chose():
    client = _FakeClient(_WORKLIST)
    llm = _llm_deciding([
        {"episode_id": 1, "verb": "prune", "reason": "incorrect"},
        {"episode_id": 3, "verb": "keep", "reason": "neutral — leave it"},
    ])
    obs = run_orchestrator_review(client=client, llm=llm, agi_mode=False, approver="orch")
    assert client.prunes == [(1, "orch")]                  # ONLY the judged-prune candidate
    assert [r.episode_id for r in obs.retired] == [1]
    assert 3 in [d.episode_id for d in obs.declined]       # the kept candidate is a measured decline


def test_declined_candidate_is_never_retired():
    # the mechanical-auto-prune mutation (retire every surfaced candidate) reds THIS assertion.
    client = _FakeClient(_WORKLIST)
    llm = _llm_deciding([{"episode_id": 1, "verb": "keep", "reason": "actually fine"},
                         {"episode_id": 3, "verb": "keep", "reason": "neutral"}])
    obs = run_orchestrator_review(client=client, llm=llm, approver="orch")
    assert client.prunes == [] and client.supersedes == []
    assert [r.episode_id for r in obs.retired] == []
    assert sorted(d.episode_id for d in obs.declined) == [1, 3]


def test_agent_surfaced_hurt_becomes_a_judged_candidate():
    # a standalone hurt-flagged bad fact appears on NO shipped worklist; the harness presents it as
    # a candidate the orchestrator JUDGES (O7-safe — the agent's hive_outcome surfaces, never acts).
    client = _FakeClient({"conflicts": [], "suspect_consensus": []})
    obs = run_orchestrator_review(
        client=client, llm=_llm_deciding([{"episode_id": 42, "verb": "prune", "reason": "wrong"}]),
        approver="orch", surfaced_hurts=(42,))
    assert client.prunes == [(42, "orch")]
    assert [r.episode_id for r in obs.retired] == [42]


def test_surfaced_hurt_is_still_only_retired_on_judgment():
    # the firewall holds for surfaced hurts too: a 'keep' judgment declines the hurt candidate.
    client = _FakeClient({"conflicts": [], "suspect_consensus": []})
    obs = run_orchestrator_review(
        client=client, llm=_llm_deciding([{"episode_id": 42, "verb": "keep", "reason": "fine"}]),
        surfaced_hurts=(42,))
    assert client.prunes == [] and [d.episode_id for d in obs.declined] == [42]


def test_no_candidates_skips_the_llm_entirely():
    client = _FakeClient({"conflicts": [], "suspect_consensus": []})
    llm = _llm_deciding([{"episode_id": 1, "verb": "prune"}])    # would prune if consulted
    obs = run_orchestrator_review(client=client, llm=llm)
    assert obs == OrchestratorObs(retired=(), declined=(), candidates=())
    assert llm.calls == []                                  # an empty worklist asks for no judgment


def test_unparseable_judgment_retires_nothing():
    # defensive parse (BUG-002/003 class): a non-JSON reply ⇒ no decisions ⇒ retire nothing.
    client = _FakeClient(_WORKLIST)
    llm = FakeLLM(lambda prompt, system: "I would keep everything; nothing to retire here.")
    obs = run_orchestrator_review(client=client, llm=llm)
    assert client.prunes == [] and [r.episode_id for r in obs.retired] == []


# ── the AGI arm only flips the approved_by stamp (AC3) ───────────────────────────────
def test_agi_mode_stamps_agi_override_actor():
    client = _FakeClient(_WORKLIST)
    decisions = [{"episode_id": 1, "verb": "prune", "reason": "wrong"}]
    run_orchestrator_review(client=client, llm=_llm_deciding(decisions), agi_mode=True)
    assert client.prunes == [(1, "AGI_OVERRIDE")]          # byte-distinguishable from the gated arm


def test_gated_mode_stamps_the_orchestrator_actor():
    client = _FakeClient(_WORKLIST)
    decisions = [{"episode_id": 1, "verb": "prune", "reason": "wrong"}]
    run_orchestrator_review(client=client, llm=_llm_deciding(decisions),
                            agi_mode=False, approver="orchestrator-7")
    assert client.prunes == [(1, "orchestrator-7")]


def test_supersede_decision_uses_the_winner_hint():
    client = _FakeClient(_WORKLIST)
    # decide to supersede candidate 1 (the conflict loser_hint); winner defaults to the b_id hint.
    run_orchestrator_review(client=client, llm=_llm_deciding(
        [{"episode_id": 1, "verb": "supersede", "reason": "b is better"}]), approver="orch")
    assert client.supersedes == [(1, 2, "orch")]           # loser=1, winner=2 (the worklist hint)


# ── integration: the real worklist → judgment → retirement loop ──────────────────────
def test_orchestrator_retires_a_real_conflict_loser_end_to_end():
    url, _server, _clock, stop = live_daemon(
        conflict=ConflictConfig(enabled=True), embedder=FakeClusterProvider(d=64))
    try:
        client = McpHttpClient(url, agent_id="orch")
        client.write("rule alpha cid=7", approved_by="human")
        client.write("rule beta cid=7", approved_by="human")     # near-dup ⇒ conflict surfaces
        loser = client.health(include_conflicts=True)["conflicts"][0]["loser_hint"]
        obs = run_orchestrator_review(
            client=client,
            llm=_llm_deciding([{"episode_id": loser, "verb": "prune", "reason": "duplicate"}]),
            approver="orch")
        assert loser in [r.episode_id for r in obs.retired]
        served = [h["episode_id"] for h in client.recall("rule cid=7")["reference_context"]]
        assert loser not in served                          # the retired loser dropped from serving
    finally:
        stop()
