"""The append-only goldens for the evidence ledger.

Golden #1 (source-level, the strongest pin): no SQL string anywhere in hive/**/*.py
UPDATEs or DELETEs evidence_events — the purity-gate idiom applied to the ledger.
Golden #2 (behavior): retirements + the change-outcome feed + a re-ingest leave every
previously-present evidence row byte-identical — history is only ever added to."""
from __future__ import annotations

import ast
import pathlib
import re

import numpy as np

import hive
from hive.adapters.sqlite_db import connect
from hive.adapters.store_sqlite import SqliteEpisodeStore
from hive.domain.evidence_kinds import EK_CHANGE_OUTCOME

ROOT = pathlib.Path(hive.__file__).resolve().parent

# UPDATE evidence_events … / DELETE FROM evidence_events … (any spacing/case)
_MUTATING_SQL = re.compile(r"\b(update\s+evidence_events|delete\s+from\s+evidence_events)\b",
                           re.IGNORECASE)


def _string_constants(path: pathlib.Path) -> list[str]:
    """Every string constant in the file, including f-string literal fragments —
    a mutating statement cannot hide in a dead branch or a formatted query."""
    tree = ast.parse(path.read_text(), filename=str(path))
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append(node.value)
    return out


def test_no_update_or_delete_targets_evidence_events_anywhere_in_hive():
    offenders: dict[str, list[str]] = {}
    for path in ROOT.rglob("*.py"):
        hits = [s for s in _string_constants(path) if _MUTATING_SQL.search(s)]
        if hits:
            offenders[str(path.relative_to(ROOT))] = hits
    assert not offenders, (
        f"evidence_events is APPEND-ONLY — mutating SQL found: {offenders}")


DIM = 4


def _approved(s: SqliteEpisodeStore, text: str, *, anchor: str = "") -> int:
    eid, _ = s.stage(text=text, weight=1.0, tags="", proposed_by="w", ts=10, anchor=anchor)
    assert s.complete(eid, np.eye(DIM, dtype=np.float32)[0], expected_version=0,
                      trust="established", approver="h", approved_ts=10, last_active_ts=10)
    return eid


def _all_evidence_rows(s: SqliteEpisodeStore) -> list[tuple]:
    return [tuple(r) for r in s.conn.execute(
        "SELECT id, episode_id, kind, actor, ts, payload FROM evidence_events ORDER BY id")]


def test_retirements_and_reingest_never_touch_prior_evidence_rows():
    # extends test_deprecate's retention pin: supersede + deprecate + append_evidence
    # + re-ingest only ever ADD rows; every prior row survives byte-identically.
    s = SqliteEpisodeStore(connect(":memory:"))
    old = _approved(s, "old fact", anchor="a.py::f")
    new = _approved(s, "corrected fact", anchor="a.py::f")
    bad = _approved(s, "bad fact", anchor="b.py::g")
    assert s.supersede(old, new, actor="h", ts=20)           # 'supersede' audit
    assert s.deprecate(bad, actor="h", ts=21)                # 'prune' audit
    before = _all_evidence_rows(s)
    assert len(before) == 2

    batch = [(new, EK_CHANGE_OUTCOME, "census", 30, '{"head_sha":"abc"}'),
             (old, EK_CHANGE_OUTCOME, "census", 30, '{"head_sha":"abc"}')]
    inserted, _ = s.append_evidence(batch)
    assert len(inserted) == 2
    after_append = _all_evidence_rows(s)
    assert after_append[:len(before)] == before              # prior rows byte-identical

    s.append_evidence(batch)                                 # idempotent re-ingest
    assert _all_evidence_rows(s) == after_append             # nothing changed at all
