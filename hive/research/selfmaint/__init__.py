"""FLEET-SELFMAINT — single-system, real-MCP, longitudinal fleet self-maintenance harness.

Dev-time only (the purity fence forbids any runtime import of ``hive.research``). A
BLACK-BOX MCP/HTTP client of the real Hivemind daemon: real agents emit their own
``hive_*`` tool calls against ONE warm ``hive up`` daemon, an orchestrator approves the
privileged retirement verbs, and the measured capability is Δ(maintenance-ON − OFF) under
the shipped paired-bootstrap-CI ship gate.

Sibling to ``hive/research/bench/`` (the comparative retrieval bench) but a different shape:
real HTTP transport + autonomous ``claude -p`` agents + a daemon lifecycle controller, vs the
in-process adapter + injected memory blocks of the comparative harness. The comparative bench
is untouched; this package reuses only ``metrics_ir`` (significance) and the
``poison_substrate`` PATTERNS (scenario construction).
"""
