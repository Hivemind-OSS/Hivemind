"""Chunk 4: batch verification + summary aggregation, input order preserved."""

from __future__ import annotations

from pathlib import Path

from hive.combdrift.resolution import fingerprint_anchor
from hive.combdrift.types import Anchor
from hive.combdrift.verdict import verify_records


def _current_anchor(repo: str, path: str, symbol: str) -> tuple[str, str, str]:
    """Anchor triple carrying a real minted baseline, so it can verdict `current`.

    A found callable with NO recorded fingerprint is unverifiable — its shape was
    never compared — so a record the batch counts as current has to be measurable
    against the working tree it will be verified in.
    """
    token = fingerprint_anchor(repo, Anchor(path, symbol))
    assert token is not None  # the fixture symbol must be a single mintable callable
    return (path, symbol, token)


def _mixed_batch(repo: str, make_record):
    # Asymmetric counts (2 current, 1 stale, 1 unverifiable) in a non-sorted
    # order, so a swapped/mislabeled summary field is locally detectable.
    return [
        make_record("cur1", _current_anchor(repo, "pkg/auth.py", "refresh")),  # current
        make_record("unv", ("../escape.py", None)),  # unverifiable
        make_record("stl", ("pkg/auth.py", "gone")),  # stale
        make_record("cur2", _current_anchor(repo, "pkg/parser.py", "parse")),  # current
    ]


def test_verify_records_counts_and_length(tmp_repo: Path, make_record):
    records = _mixed_batch(str(tmp_repo), make_record)
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
    repo = str(tmp_repo)
    records = [
        make_record("a", _current_anchor(repo, "pkg/auth.py", "refresh")),  # current
        make_record("b", _current_anchor(repo, "pkg/parser.py", "parse")),  # current
        make_record("c", ("pkg/missing.py", "x")),  # stale
        make_record("d"),  # unverifiable (no anchors)
        make_record("e", ("pkg/broken.py", "oops")),  # unverifiable (parse error)
    ]
    verdicts, summary = verify_records(repo, records)
    assert len(verdicts) == len(records)
    assert summary.current + summary.stale + summary.unverifiable == len(records)
    assert (summary.current, summary.stale, summary.unverifiable) == (2, 1, 2)


def test_verify_records_empty_batch(tmp_repo: Path):
    verdicts, summary = verify_records(str(tmp_repo), [])
    assert verdicts == []
    assert (summary.current, summary.stale, summary.unverifiable) == (0, 0, 0)


def test_verify_records_object_determinism(tmp_repo: Path, make_record):
    # Same inputs twice -> equal verdict/summary objects (frozen dataclass __eq__).
    records = _mixed_batch(str(tmp_repo), make_record)
    v1, s1 = verify_records(str(tmp_repo), records)
    v2, s2 = verify_records(str(tmp_repo), records)
    assert v1 == v2
    assert s1 == s2
