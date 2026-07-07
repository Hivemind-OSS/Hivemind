"""Phase-1 fleet benchmark — orchestrator-in-loop LongMemEval head-to-head (Hivemind vs mem0).

DEV-TIME ONLY. Like the rest of ``hive.research`` this package sits ON TOP of the runtime
and may import it (``hive.app.container``, ``hive.app.mcp_server``, ``hive.client``); the
purity fence (``tests/test_purity.py::test_research_not_imported_by_runtime``) forbids the
reverse — no runtime module may import ``hive.research``. Nothing here ships in the image.

The harness drives the REAL in-process ``HiveMCPServer`` (real embedder, store, admission,
recall) through a backend-swap ``MemoryBackend`` Protocol so the SAME orchestrator/subagent
loop scores Hivemind and mem0 on equal footing. See ``docs/PLANS/FLEET-BENCHMARK-PLAN.md``.
"""
