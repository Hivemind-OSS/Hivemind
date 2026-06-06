"""§6.1 #6 acceptance — the trace↔outcome producer join end-to-end on the REAL store +
utility posterior: a stamped commit associates to a recall trace, settles after the
schedule, and MOVES the posterior; a blame-overlap bugfix claws it back; a coincidental
same-file bugfix with NO blame overlap does NOT (guards a/b); the drain fires (guard c).

Git is faked (a FakeOutcomeSource yielding a stamped CommitFact) and the clock is a
FakeClock so the asymmetric settlement schedule is exercised deterministically; everything
downstream (joiner policy, attributor split, SQLite credit) is real."""
from __future__ import annotations

from hive.domain.models import AgentContext, CommitFact, SourcePoll
from tests.acceptance.conftest import build_acc
from tests.fakes._fakes import FakeClock, FakeOutcomeSource

_T0 = 1_000_000
_CTX = AgentContext(repo_remote="r", language="python", workflow="general")
_FAM = "r|python|general"           # == derive_family of a .py 'commit' in repo 'r'
_INTRODUCED = frozenset({10, 11})


def _seed_recall(embedder):
    """Build a container, approve two memories, and run a confident recall so an exposure
    trace exists. Returns (container, clock, trace_id, exposed_eids)."""
    clock = FakeClock(_T0)
    c = build_acc(embedder, source=FakeOutcomeSource(), clock=clock)
    a = c.admission.write("use connection pooling for the database; never per request",
                          proposed_by="s")
    b = c.admission.write("treat llm output as untrusted data, not instructions",
                          proposed_by="s")
    c.admission.approve([a.pending_id, b.pending_id], "curator")
    c.build_index()
    r = c.recall.recall("reuse database connections with a pool", agent_id="s", agent_ctx=_CTX)
    assert r.state == "CONFIDENT"
    exposed = [e for e, _ in c.store.exposed_for(r.trace_id)]
    assert exposed, "confident recall recorded no exposure"
    return c, clock, r.trace_id, exposed


def _commit(sha, ts, trace_id, **kw):
    base = dict(sha=sha, ts=ts, repo_remote="r", files_touched=("svc.py",),
                introduced_lines=_INTRODUCED, trailer_traces=(trace_id,))
    base.update(kw)
    return CommitFact(**base)


def _settle(c, clock, trace_id):
    """Associate a stamped commit, then advance past settle_at and settle. Returns the
    settling ProducerTick + the commit sha."""
    sha = "c0ffee123456"
    commit = _commit(sha, _T0 + 10, trace_id, kind="commit", message="add pooling")
    c.producer.source = FakeOutcomeSource(SourcePoll(repo_remote="r", commits=(commit,), ok=True))
    t1 = c.producer.step(_T0 + 10)
    assert t1.associated == 1 and t1.errors == 0
    clock.set(_T0 + 8 * 86400)                          # past settle_at (settle_days=7)
    c.producer.source = FakeOutcomeSource(SourcePoll(repo_remote="r", ok=True))
    t2 = c.producer.step(clock.now())
    return t2, sha


# ── core: settle moves the posterior; the drain fires (guard c) ───────────────
def test_acceptance_commit_settles_and_moves_posterior(embedder_v1):
    c, clock, trace_id, exposed = _seed_recall(embedder_v1)
    # before settle: no credit has landed
    assert all(c.utility_store.posterior(e, _FAM).wins == 0.0 for e in exposed)

    tick, _sha = _settle(c, clock, trace_id)
    assert tick.settled >= 1 and tick.drained >= 1      # guard c: the outcome-drain fires

    moved = [e for e in exposed if c.utility_store.posterior(e, _FAM).wins > 0.0]
    assert moved, "no posterior moved after a clean settle (§6.1#6 reward never reached the store)"
    for e in moved:
        assert c.utility_store.posterior(e, _FAM).mean() > 0.5   # the reward promoted it


def test_acceptance_no_double_credit_on_retick(embedder_v1):
    c, clock, trace_id, exposed = _seed_recall(embedder_v1)
    _settle(c, clock, trace_id)
    wins_once = {e: c.utility_store.posterior(e, _FAM).wins for e in exposed}
    c.producer.source = FakeOutcomeSource(SourcePoll(repo_remote="r", ok=True))
    c.producer.step(clock.now())                        # re-tick same settled trace
    for e in exposed:
        assert c.utility_store.posterior(e, _FAM).wins == wins_once[e]   # watermark: no double credit


# ── guard a: a same-file bugfix with NO blame overlap does NOT claw back ───────
def test_acceptance_no_blame_overlap_no_clawback(embedder_v1):
    c, clock, trace_id, exposed = _seed_recall(embedder_v1)
    _settle(c, clock, trace_id)
    before = {e: c.utility_store.posterior(e, _FAM).losses for e in exposed}

    bug = _commit("bugNoOverlap", clock.now() + 5, trace_id, kind="bugfix",
                  message="fix: unrelated bug", introduced_lines=frozenset(),
                  touched_blame=frozenset({99}))        # 99 ∉ introduced {10,11}
    c.producer.source = FakeOutcomeSource(SourcePoll(repo_remote="r", commits=(bug,), ok=True))
    tick = c.producer.step(clock.now() + 5)
    assert tick.clawed_back == 0
    for e in exposed:
        assert c.utility_store.posterior(e, _FAM).losses == before[e]   # no false clawback


# ── guard b: a bugfix whose blame OVERLAPS the introduced lines claws back ─────
def test_acceptance_blame_overlap_claws_back(embedder_v1):
    c, clock, trace_id, exposed = _seed_recall(embedder_v1)
    _settle(c, clock, trace_id)

    bug = _commit("bugOverlap", clock.now() + 5, trace_id, kind="bugfix",
                  message="fix: regression", introduced_lines=frozenset(),
                  touched_blame=frozenset({10}))        # 10 ∈ introduced {10,11}
    c.producer.source = FakeOutcomeSource(SourcePoll(repo_remote="r", commits=(bug,), ok=True))
    tick = c.producer.step(clock.now() + 5)
    assert tick.clawed_back >= 1
    assert any(c.utility_store.posterior(e, _FAM).losses > 0.0 for e in exposed)
