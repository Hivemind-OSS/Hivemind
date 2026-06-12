"""Phase-1-inert acceptance + the chunk's deliberate mutation target — utility is
OBSERVED-NOT-APPLIED: posteriors accrue, but the surfacer (enabled=False) NEVER moves
recall order. Flipping ``surfacer.enabled`` to True in build_container makes
``test_acceptance_utility_observed_not_applied`` go RED (the confident-negative posterior
demotes the top hit ⇒ order changes)."""
from __future__ import annotations

from hive.adapters.sqlite_db import tx
from hive.domain.attribution import CreditDelta
from hive.domain.models import CONFIDENT, AgentContext
from tests.acceptance.conftest import HIT_QUERIES, build_acc, seed_corpus

# explicit 3-axis context so the posterior family matches the recall family exactly
_CTX = AgentContext(repo_remote="acme/app", language="python", workflow="general")
_FAM = "acme/app|python|general"


def _order(container, query):
    r = container.recall.recall(query, agent_id="acc", agent_ctx=_CTX)
    return r, [h.episode_id for h in r.hits]


def test_acceptance_utility_observed_not_applied(embedder_v1):
    c = build_acc(embedder_v1)
    seed_corpus(c)
    q = HIT_QUERIES[0][0]

    r0, base = _order(c, q)
    assert r0.state == CONFIDENT and len(base) >= 2     # need ≥2 hits for a re-order to be observable
    top = base[0]

    # accrue a CONFIDENT-NEGATIVE posterior for the current top hit in THIS family
    with tx(c.conn):
        c.utility_store.apply_credit([CreditDelta(
            episode_id=top, family_scope=_FAM, d_wins=0.0, d_losses=30.0, source_agent="x")])
    # it really is confident + negative (would demote, IF the surfacer were live)
    umap = c.utility_store.utility_map(_FAM, confident_only=True)
    assert top in umap and umap[top] < 0.5

    _, after = _order(c, q)
    assert after == base, ("utility moved recall order in Phase 1 — the surfacer must be "
                           "inert (observed-not-applied) until a Phase-2 KEEP")


def test_acceptance_posteriors_still_accrue_while_inert(embedder_v1):
    """The other half of observed-not-applied: the posterior DID move (it is being
    observed) even though ranking did not — so the keystone has a stream to read."""
    c = build_acc(embedder_v1)
    seed_corpus(c)
    _, base = _order(c, HIT_QUERIES[0][0])
    top = base[0]
    with tx(c.conn):
        c.utility_store.apply_credit([CreditDelta(
            episode_id=top, family_scope=_FAM, d_wins=5.0, d_losses=0.0, source_agent="x")])
    assert c.utility_store.posterior(top, _FAM).wins == 5.0
