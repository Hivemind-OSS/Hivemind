"""hive.domain.evidence_kinds — the evidence-EVENT kind registry (pure data).

Pins the EXACT stored vocabulary: ``evidence_events.kind`` has no DDL CHECK (unlike
``episodes.kind``), so these string values ARE the stored schema — existing ledgers keep
matching only while the constants stay byte-identical. Distinct from ``hive.domain.kinds``
(the served MEMORY taxonomy, contract-guard watched); this registry serves the ledger only.
"""
from __future__ import annotations

import ast
import inspect

from hive.domain import evidence_kinds as ek


def test_exact_values_pin_the_stored_vocabulary():
    # the golden: existing evidence_events rows keep matching these exact bytes
    assert ek.EK_PROMOTE == "promote"
    assert ek.EK_SUPERSEDE == "supersede"
    assert ek.EK_PRUNE == "prune"
    assert ek.EK_TTL_EXPIRED == "ttl_expired"
    assert ek.EK_OUTCOME_HELPED == "outcome_helped"
    assert ek.EK_OUTCOME_HURT == "outcome_hurt"
    assert ek.EK_CHANGE_OUTCOME == "change_outcome"


def test_membership_set_is_exactly_the_seven_constants():
    assert ek.EVIDENCE_KINDS == frozenset({
        "promote", "supersede", "prune", "ttl_expired",
        "outcome_helped", "outcome_hurt", "change_outcome"})


def test_pure_data_module_imports_nothing():
    # the provenance.py precedent: a pure-data sibling registry — only __future__ imports
    tree = ast.parse(inspect.getsource(ek))
    imports = [n for n in ast.walk(tree)
               if isinstance(n, (ast.Import, ast.ImportFrom))]
    assert all(isinstance(n, ast.ImportFrom) and n.module == "__future__"
               for n in imports), "evidence_kinds must stay pure data (no runtime imports)"
