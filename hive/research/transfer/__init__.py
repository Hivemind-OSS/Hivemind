"""Fleet knowledge-transfer benchmark (BENCHMARK §7) — recall-determined success.

A dev-time, single-purpose sibling of ``hive.research.selfmaint`` and ``hive.research.bench``. It
measures whether a fleet sharing the real Hivemind memory passes a downstream task that is solvable
ONLY by recalling a fact an upstream agent EARNED and captured during its own task — i.e. whether
recall of the correct, agent-earned memory is the determinant of success.

Like ``selfmaint`` it is a BLACK-BOX MCP/HTTP client of the real daemon: it imports no
``hive.domain`` decision function, exercises shipped behavior through the public tool surface only,
and is fenced out of the runtime import graph (``tests/test_purity.py``). The runtime never imports
``hive.research``.
"""
