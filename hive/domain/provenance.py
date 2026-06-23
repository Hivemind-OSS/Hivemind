"""The provenance taxonomy — the single source for a memory's ORIGIN label.

PURE DATA: this module imports nothing (the purity gate forbids sqlite3|torch|subprocess|
os|git|time anywhere in hive/domain/; pure constants are fine). Every surface that needs the
vocabulary PROJECTS from here — the store builds its CHECK clause, models.__post_init__
validates against ``PROVENANCE_NAMES``, the admission door stamps the transport-set value — so
the enforced and stored copies cannot drift (mirrors hive.domain.kinds).

Provenance is the ORIGIN of a memory's content, ORTHOGONAL to trust/vouch (trust is WHO
stands behind it; provenance is WHERE the content came from). Carried-not-interpreted: it
labels a memory, it is never a recall filter and never embedded.

The taxonomy:
- ``agent_reasoned`` — an agent reasoned the content (the autonomous hive_capture path). The
  fail-safe DEFAULT: a write-site that forgets to set provenance lands here, which never
  over-claims trust (an agent-reasoned label is the weakest origin claim).
- ``human`` — a human authored or vouched the text (the hive_write path, which carries
  ``approved_by``).
- ``artifact_ingested`` — ENUM-RESERVED for forward-compat (an agent ingested content from a
  tool/web/file artifact). Allowed by the DDL CHECK and Episode.__post_init__, but set by NO
  v1 transport path: the transport cannot know an agent ingested from an artifact without the
  caller ASSERTING it, and a caller-asserted provenance violates INV-2 (no caller-asserted
  provenance). It exists so the column need not migrate when a later thread produces it.
"""
from __future__ import annotations

# The fail-safe, under-claiming default: a memory whose origin nobody set is treated as
# agent-reasoned (the weakest origin claim), never over-claimed as human-authored.
DEFAULT_PROVENANCE = "agent_reasoned"

# The single membership set every enforcer reads (DDL CHECK, Episode.__post_init__).
PROVENANCE_NAMES = frozenset({"agent_reasoned", "artifact_ingested", "human"})
