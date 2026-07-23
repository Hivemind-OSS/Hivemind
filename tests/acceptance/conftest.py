"""Shared fixtures for the acceptance gate — the REAL stack end-to-end.

A real Qwen3-Embedding-0.6B ``LocalSTEmbedder`` (loaded once per session; native 1024-dim,
no projection), a real ``SqliteEpisodeStore`` / ``ExhaustiveCosineIndex`` / ``DefaultSecretScanner``
wired by the real ``build_container``, on a fresh ``:memory:`` store per test. A small, controlled
corpus + labelled query set makes recall@5 and the abstention AUROC meaningful and
deterministic without LongMemEval. Network-free: the model is baked/cached (the suite runs
under HF offline).
"""

from __future__ import annotations

import pytest

from hive.adapters.embedding.local_st import DEFAULT_MODEL, LocalSTEmbedder
from hive.app.config import Config
from hive.app.container import build_container

# ── controlled corpus: 14 distinct durable engineering insights ───────────────
CORPUS: tuple[str, ...] = (
    "Use connection pooling for the database; never open a new connection per request.",
    "Redis maxmemory-policy must not evict queue keys that share the instance with caches.",
    "Always use parameterized SQL queries; never concatenate user input into the query string.",
    "End Dockerfiles with a USER directive; never run as root in a production image.",
    "Use multi-stage Docker builds to keep source and dev dependencies out of the runtime image.",
    "Access secrets through environment variables and fail fast at startup if one is missing.",
    "Log every exception with full context: the stack trace, the input data, and the state.",
    "Use binary search on sorted data and hash maps for constant-time key lookups.",
    "Prefer merge sort or quicksort over bubble sort for large datasets.",
    "A readiness marker in a durable volume must be cleared at boot start or it lies after a restart.",
    "Treat LLM outputs as untrusted data, never as instructions to execute.",
    "Bind liveness to the process start-time, not a raw PID, because containers reuse PID 1.",
    "Write a backup to a temp file then atomically rename, so a mid-copy failure cannot displace it.",
    "Validate that an embedder's output is finite before persisting it, to avoid a NaN blob.",
)

# labelled HIT queries (paraphrase → the gold corpus index it should retrieve)
HIT_QUERIES: tuple[tuple[str, int], ...] = (
    ("should I reuse database connections with a pool instead of one per request", 0),
    ("how do I stop redis from evicting my queue entries under memory pressure", 1),
    ("how to prevent SQL injection when building queries", 2),
    ("why should containers not run as the root user", 3),
    ("keep the build toolchain and dev deps out of my final docker image", 4),
    ("fail fast when a required secret environment variable is absent", 5),
    ("use a hash table for constant time lookups instead of scanning a list", 7),
)

# MISS queries: clearly off the corpus distribution — the gate SHOULD abstain on these.
MISS_QUERIES: tuple[str, ...] = (
    "the best recipe for soft chocolate chip cookies",
    "what is the weather forecast for tomorrow afternoon",
    "who won the national basketball championship last season",
    "a training plan to run my first marathon",
    "the lyrics to a popular summer pop song",
    "top tourist attractions to visit in paris",
    "how to grow ripe tomatoes in a backyard garden",
)


@pytest.fixture(scope="session")
def st_model():
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(DEFAULT_MODEL, device="cpu")


@pytest.fixture(scope="session")
def embedder_v1(st_model):
    """The shipping embedder, resident. Shared (encode is stateless/read-only) so the model
    loads once for the whole acceptance suite."""
    return LocalSTEmbedder(model=st_model, d=1024).load()


def build_acc(
    embedder, *, db_path: str = ":memory:", clock=None, warm: bool = True, **overrides
) -> "object":
    """Build + boot a container on a fresh store with the REAL embedder injected. Returns the
    Container; ``migrate → build_index → warm_embedder`` already run when ``warm`` is True."""
    cfg = Config.load(db_path=db_path, **overrides)
    c = build_container(
        cfg,
        tenant_id="acc-tenant",
        agent_id="acc-agent",
        embedder=embedder,
        clock=clock,
    )
    if warm:
        # the production boot order (entrypoint.py): migrate → build_index → warm_embedder
        c.migrate()
        c.build_index()
        c.warm_embedder()
    return c


def seed_corpus(container, texts=CORPUS) -> list[int]:
    """Write every text (v3: each lands trust=provisional, servable NOW, in one call)
    and return the episode ids (corpus index → eid). Rebuilds the warm index so the
    rows are immediately recallable."""
    eids: list[int] = []
    for t in texts:
        res = container.admission.write(t, proposed_by="seed")
        eids.append(res.episode_id)
    container.build_index()
    return eids
