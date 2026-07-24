"""Chunk 4: batch verification + summary aggregation, input order preserved."""

from __future__ import annotations

from pathlib import Path

from hive.combdrift.verdict import verify_records


def _mixed_batch(make_record):
    # Asymmetric counts (2 current, 1 stale, 1 unverifiable) in a non-sorted
    # order, so a swapped/mislabeled summary field is locally detectable.
    return [
        make_record("cur1", ("pkg/auth.py", "refresh")),  # current
        make_record("unv", ("../escape.py", None)),  # unverifiable
        make_record("stl", ("pkg/auth.py", "gone")),  # stale
        make_record("cur2", ("pkg/parser.py", "parse")),  # current
    ]


def test_verify_records_counts_and_length(tmp_repo: Path, make_record):
    records = _mixed_batch(make_record)
    verdicts, summary = verify_records(str(tmp_repo), records)
    assert len(verdicts) == 4
    # Order preserved exactly as input.
    assert [v.id for v in verdicts] == ["cur1", "unv", "stl", "cur2"]
    assert [v.verdict for v in verdicts] == [
        "current",
        "unverifiable",
        "stale",
        "current",
    ]
    assert (summary.current, summary.stale, summary.unverifiable) == (2, 1, 1)


def test_summary_sums_to_total(tmp_repo: Path, make_record):
    # A larger, lopsided batch: counts must still total the record count.
    records = [
        make_record("a", ("pkg/auth.py", "refresh")),  # current
        make_record("b", ("pkg/parser.py", "parse")),  # current
        make_record("c", ("pkg/missing.py", "x")),  # stale
        make_record("d"),  # unverifiable (no anchors)
        make_record("e", ("pkg/broken.py", "oops")),  # unverifiable (parse error)
    ]
    verdicts, summary = verify_records(str(tmp_repo), records)
    assert len(verdicts) == len(records)
    assert summary.current + summary.stale + summary.unverifiable == len(records)
    assert (summary.current, summary.stale, summary.unverifiable) == (2, 1, 2)


def test_verify_records_empty_batch(tmp_repo: Path):
    verdicts, summary = verify_records(str(tmp_repo), [])
    assert verdicts == []
    assert (summary.current, summary.stale, summary.unverifiable) == (0, 0, 0)


def test_verify_records_object_determinism(tmp_repo: Path, make_record):
    # Same inputs twice -> equal verdict/summary objects (frozen dataclass __eq__).
    records = _mixed_batch(make_record)
    v1, s1 = verify_records(str(tmp_repo), records)
    v2, s2 = verify_records(str(tmp_repo), records)
    assert v1 == v2
    assert s1 == s2
