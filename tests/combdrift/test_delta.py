"""EXTEND-1: FingerprintDelta + AnchorResult.delta — structured drift direction.

A fingerprint drift's breaking/additive direction is surfaced as a typed field
on the per-anchor result, so a consumer reads it as data instead of scraping the
reason string. Back-compatible: delta defaults None and the JSON wire is
unchanged unless a fingerprint was actually compared.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from hive.combdrift.resolution import fingerprint_anchor, resolve_anchor
from hive.combdrift.serialization import verdict_to_dict
from hive.combdrift.types import (
    REASON_FINGERPRINT_VERSION_MISMATCH,
    REASON_OK,
    REASON_SIGNATURE_CHANGED,
    Anchor,
    AnchorResult,
    FingerprintDelta,
    RecordVerdict,
)


# --- the carrier type ----------------------------------------------------------


def test_fingerprint_delta_is_frozen():
    d = FingerprintDelta(
        old_token="combdrift-fp/1:a", new_token="combdrift-fp/1:b", breaking=True
    )
    assert d.old_token == "combdrift-fp/1:a"
    assert d.new_token == "combdrift-fp/1:b"
    assert d.breaking is True
    with pytest.raises(FrozenInstanceError):
        d.breaking = False  # type: ignore[misc]


def test_anchor_result_delta_defaults_none():
    r = AnchorResult(
        path="p.py", symbol="f", found=True, location="p.py:1", reason=REASON_OK
    )
    assert r.delta is None


# --- resolution populates delta on a drift ------------------------------------


def test_breaking_drift_populates_delta(tmp_repo: Path):
    fp = fingerprint_anchor(str(tmp_repo), Anchor("pkg/parser.py", "parse"))  # 3 args
    (tmp_repo / "pkg/parser.py").write_text(
        "def parse(a, b):\n    return (a, b)\n", encoding="utf-8"
    )
    current = fingerprint_anchor(
        str(tmp_repo), Anchor("pkg/parser.py", "parse")
    )  # 2 args
    res = resolve_anchor(str(tmp_repo), Anchor("pkg/parser.py", "parse", fp))
    assert res.reason.startswith(
        REASON_SIGNATURE_CHANGED
    )  # reason unchanged (back-compat)
    assert res.delta is not None
    assert res.delta.breaking is True
    assert res.delta.old_token == fp
    assert res.delta.new_token == current


def test_additive_drift_populates_nonbreaking_delta(tmp_repo: Path):
    fp = fingerprint_anchor(str(tmp_repo), Anchor("pkg/parser.py", "parse"))  # 3 args
    (tmp_repo / "pkg/parser.py").write_text(
        "def parse(a, b, c, d=1):\n    return (a, b, c, d)\n", encoding="utf-8"
    )
    current = fingerprint_anchor(str(tmp_repo), Anchor("pkg/parser.py", "parse"))
    res = resolve_anchor(str(tmp_repo), Anchor("pkg/parser.py", "parse", fp))
    assert res.reason == REASON_OK  # additive is not stale (back-compat)
    assert res.delta is not None
    assert res.delta.breaking is False
    assert res.delta.old_token == fp
    assert res.delta.new_token == current


def test_incomparable_version_has_no_delta(tmp_repo: Path):
    token = "combdrift-fp/9:func(req=0,max=0,star=0,kw=0,kwo=0,gen=0,dec=,base=0)"
    res = resolve_anchor(str(tmp_repo), Anchor("pkg/parser.py", "parse", token))
    assert res.reason.startswith(REASON_FINGERPRINT_VERSION_MISMATCH)
    assert res.delta is None  # a version we cannot read yields no structured delta


def test_ambiguous_overload_has_no_delta(tmp_repo: Path):
    # Two top-level defs of the same name -> no single interface -> abstain, no delta.
    (tmp_repo / "pkg/over.py").write_text(
        "def dup(a):\n    return a\n\n\ndef dup(a, b):\n    return (a, b)\n",
        encoding="utf-8",
    )
    token = "combdrift-fp/1:func(req=1,max=1,star=0,kw=0,kwo=0,gen=0,dec=,base=0)"
    res = resolve_anchor(str(tmp_repo), Anchor("pkg/over.py", "dup", token))
    assert res.reason.startswith(REASON_FINGERPRINT_VERSION_MISMATCH)
    assert res.delta is None


def test_no_fingerprint_has_no_delta(tmp_repo: Path):
    res = resolve_anchor(str(tmp_repo), Anchor("pkg/parser.py", "parse"))
    assert res.reason == REASON_OK
    assert res.delta is None


# --- serialization emits delta only when present ------------------------------


def _verdict_with(anchor: AnchorResult) -> RecordVerdict:
    return RecordVerdict(id="m", verdict="current", anchors=(anchor,))


def test_wire_omits_delta_when_absent():
    a = AnchorResult(
        path="p.py", symbol="f", found=True, location="p.py:1", reason=REASON_OK
    )
    d = verdict_to_dict(_verdict_with(a))
    assert "delta" not in d["anchors"][0]  # wire shape unchanged when no delta


def test_wire_includes_delta_when_present():
    delta = FingerprintDelta(
        old_token="combdrift-fp/1:old", new_token="combdrift-fp/1:new", breaking=True
    )
    a = AnchorResult(
        path="p.py",
        symbol="f",
        found=True,
        location="p.py:1",
        reason="signature_changed: ...",
        delta=delta,
    )
    d = verdict_to_dict(_verdict_with(a))
    assert d["anchors"][0]["delta"] == {
        "old_token": "combdrift-fp/1:old",
        "new_token": "combdrift-fp/1:new",
        "breaking": True,
    }
