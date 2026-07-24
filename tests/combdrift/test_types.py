"""Chunk 1: data contract — construction, immutability, Verdict literal, reason codes."""

from __future__ import annotations

import dataclasses
import typing

import pytest

from hive.combdrift.types import (
    REASON_FINGERPRINT_VERSION_MISMATCH,
    REASON_SIGNATURE_CHANGED,
    REASON_SYMBOL_INDIRECT,
    Anchor,
    AnchorResult,
    Record,
    RecordVerdict,
    RunSummary,
    Verdict,
)


def test_anchor_construction_and_defaults():
    a = Anchor(path="pkg/auth.py")
    assert a.path == "pkg/auth.py"
    assert a.symbol is None
    assert a.fingerprint is None
    b = Anchor(path="pkg/auth.py", symbol="refresh")
    assert b.symbol == "refresh"


def test_anchor_fingerprint_field():
    # The opaque capture-side token rides on the anchor; default is None so
    # every existing call site keeps the same shape.
    a = Anchor(
        path="pkg/auth.py", symbol="refresh", fingerprint="combdrift-fp/1:func(req=1)"
    )
    assert a.fingerprint == "combdrift-fp/1:func(req=1)"


def test_new_reason_codes_are_pinned():
    # The closed reason-code set this increment adds; consumers branch on these
    # exact spellings, so they are contract.
    assert REASON_SYMBOL_INDIRECT == "symbol_indirect"
    assert REASON_SIGNATURE_CHANGED == "signature_changed"
    assert REASON_FINGERPRINT_VERSION_MISMATCH == "fingerprint_version_mismatch"


def test_record_construction_and_defaults():
    r = Record(id="m1", claim_text="x")
    assert r.id == "m1"
    assert r.claim_text == "x"
    assert r.anchors == ()
    assert r.recorded_at_commit is None
    r2 = Record(
        id="m2",
        claim_text="y",
        anchors=(Anchor("p.py", "f"),),
        recorded_at_commit="abc123",
    )
    assert r2.anchors[0].symbol == "f"
    assert r2.recorded_at_commit == "abc123"


def test_anchor_result_fields():
    ar = AnchorResult(
        path="pkg/auth.py",
        symbol="refresh",
        found=True,
        location="pkg/auth.py:4",
        reason="ok",
    )
    assert ar.found is True
    assert ar.location == "pkg/auth.py:4"
    assert ar.reason == "ok"


def test_record_verdict_fields():
    rv = RecordVerdict(id="m1", verdict="current", anchors=())
    assert rv.id == "m1"
    assert rv.verdict == "current"
    assert rv.anchors == ()


def test_run_summary_fields():
    s = RunSummary(current=1, stale=2, unverifiable=3)
    assert (s.current, s.stale, s.unverifiable) == (1, 2, 3)


@pytest.mark.parametrize(
    "dc_cls,kwargs",
    [
        (Anchor, {"path": "p.py"}),
        (Record, {"id": "m", "claim_text": "c"}),
        (
            AnchorResult,
            {
                "path": "p.py",
                "symbol": None,
                "found": False,
                "location": None,
                "reason": "file_missing",
            },
        ),
        (RecordVerdict, {"id": "m", "verdict": "stale", "anchors": ()}),
        (RunSummary, {"current": 0, "stale": 0, "unverifiable": 0}),
    ],
)
def test_dataclasses_are_frozen(dc_cls, kwargs):
    inst = dc_cls(**kwargs)
    field_name = next(iter(kwargs))
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(inst, field_name, "mutated")


def test_verdict_literal_membership():
    # Verdict is a typing.Literal of exactly these three wire strings.
    assert set(typing.get_args(Verdict)) == {"current", "stale", "unverifiable"}
