"""P1.5 — query→family resolution [A2]. `_resolve_query_family` is the SOLE owner
of the LIVE query's family_scope. It uses the SAME three-axis grammar the producer
derives at link time (git-remote | language | coarse-workflow) so query-side and
credit-side families are byte-comparable — a memory proven on `repoX|python|bugfix`
is never boosted for a `repoY|...` query because the keys differ.

(The pipeline-level pins — `recall()` calls `utility_map(family_scope=fam)` with
exactly this string, and a confident posterior under one family does not reorder a
different-family query — live in test_recall_pipeline.py where the pipeline exists.)
"""
from __future__ import annotations

from hive.domain.models import AgentContext
from hive.domain.recall import _resolve_query_family


def test_query_family_pins_known_context():
    fam = _resolve_query_family(AgentContext("github.com/acme/web", "python", "bugfix"))
    assert fam == "github.com/acme/web|python|bugfix"


def test_query_family_empty_axes_collapse_to_unscoped():
    # empty axes collapse to "*"/"general" — the cross-repo aggregate slice nothing
    # credits ⇒ utility_map returns {} ⇒ surfacer identity (safe degradation, A2).
    assert _resolve_query_family(AgentContext("", "", "")) == "*|*|general"
    assert _resolve_query_family(AgentContext()) == "*|*|general"
    assert _resolve_query_family(AgentContext("r", "", "general")) == "r|*|general"


def test_query_family_distinct_repos_are_distinct_keys():
    a = _resolve_query_family(AgentContext("github.com/acme/web", "python", "bugfix"))
    b = _resolve_query_family(AgentContext("github.com/acme/api", "go", "general"))
    assert a != b  # cross-family isolation is keyed on the exact string
