"""Chunk 5: the single wire-shape seam. JSON <-> dataclass, exact key sets."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hive.combdrift.serialization import (
    record_from_dict,
    records_from_json,
    run_to_json,
    summary_to_dict,
    verdict_to_dict,
)
from hive.combdrift.types import AnchorResult, RecordVerdict, RunSummary
from hive.combdrift.verdict import verify_records


def test_verdict_to_dict_shape():
    rv = RecordVerdict(
        id="m1",
        verdict="stale",
        anchors=(
            AnchorResult(
                path="pkg/auth.py",
                symbol="refresh",
                found=False,
                location=None,
                reason="symbol_missing: refresh",
            ),
        ),
    )
    d = verdict_to_dict(rv)
    assert set(d.keys()) == {"id", "verdict", "anchors"}
    assert d["id"] == "m1"
    assert d["verdict"] == "stale"
    assert isinstance(d["anchors"], list) and len(d["anchors"]) == 1
    anchor = d["anchors"][0]
    assert set(anchor.keys()) == {"path", "symbol", "found", "location", "reason"}
    assert anchor["path"] == "pkg/auth.py"
    assert anchor["symbol"] == "refresh"
    assert anchor["found"] is False
    assert anchor["location"] is None
    assert anchor["reason"] == "symbol_missing: refresh"


def test_summary_to_dict_shape():
    s = RunSummary(current=2, stale=1, unverifiable=3)
    d = summary_to_dict(s)
    assert set(d.keys()) == {"current", "stale", "unverifiable"}
    assert d == {"current": 2, "stale": 1, "unverifiable": 3}


def test_record_from_dict_roundtrip():
    d = {
        "id": "m9",
        "claim_text": "parse takes three args",
        "anchors": [
            {"path": "pkg/parser.py", "symbol": "parse"},
            {"path": "pkg/auth.py"},  # symbol omitted -> None
        ],
        "recorded_at_commit": "deadbeef",
    }
    rec = record_from_dict(d)
    assert rec.id == "m9"
    assert rec.claim_text == "parse takes three args"
    assert len(rec.anchors) == 2
    assert rec.anchors[0].path == "pkg/parser.py"
    assert rec.anchors[0].symbol == "parse"
    assert rec.anchors[1].symbol is None
    # recorded_at_commit preserved untouched (informational only).
    assert rec.recorded_at_commit == "deadbeef"


def test_record_from_dict_defaults():
    rec = record_from_dict({"id": "m", "claim_text": "c"})
    assert rec.anchors == ()
    assert rec.recorded_at_commit is None


def test_anchor_fingerprint_roundtrips():
    # The optional opaque token is parsed onto the anchor when present.
    d = {
        "id": "m",
        "claim_text": "c",
        "anchors": [
            {
                "path": "p.py",
                "symbol": "f",
                "fingerprint": "combdrift-fp/1:func(req=2)",
            },
        ],
    }
    rec = record_from_dict(d)
    assert rec.anchors[0].fingerprint == "combdrift-fp/1:func(req=2)"


def test_anchor_fingerprint_absent_is_none():
    # The field is optional on the wire; absence is tolerated, never an error.
    rec = record_from_dict(
        {"id": "m", "claim_text": "c", "anchors": [{"path": "p.py", "symbol": "f"}]}
    )
    assert rec.anchors[0].fingerprint is None


def test_record_from_dict_missing_id_raises():
    with pytest.raises(ValueError, match="id"):
        record_from_dict({"claim_text": "c"})


def test_record_from_dict_missing_claim_text_raises():
    with pytest.raises(ValueError, match="claim_text"):
        record_from_dict({"id": "m"})


def test_record_from_dict_non_list_anchors_raises():
    with pytest.raises(ValueError, match="anchors"):
        record_from_dict({"id": "m", "claim_text": "c", "anchors": "nope"})


def test_record_from_dict_anchor_missing_path_raises():
    with pytest.raises(ValueError, match="path"):
        record_from_dict({"id": "m", "claim_text": "c", "anchors": [{"symbol": "x"}]})


def test_records_from_json_parses_list():
    text = json.dumps(
        [
            {"id": "a", "claim_text": "x", "anchors": []},
            {
                "id": "b",
                "claim_text": "y",
                "anchors": [{"path": "p.py", "symbol": "f"}],
            },
        ]
    )
    recs = records_from_json(text)
    assert [r.id for r in recs] == ["a", "b"]
    assert recs[1].anchors[0].symbol == "f"


def test_records_from_json_non_list_raises():
    with pytest.raises(ValueError):
        records_from_json(json.dumps({"id": "a", "claim_text": "x"}))


def test_run_to_json_top_level_shape(tmp_repo: Path, make_record):
    records = [make_record("a", ("pkg/auth.py", "refresh"))]
    verdicts, summary = verify_records(str(tmp_repo), records)
    out = json.loads(run_to_json(verdicts, summary))
    assert set(out.keys()) == {"verdicts", "summary"}
    assert isinstance(out["verdicts"], list)
    assert set(out["summary"].keys()) == {"current", "stale", "unverifiable"}
    assert out["verdicts"][0]["id"] == "a"


def test_run_is_deterministic(tmp_repo: Path, make_record):
    # Same records + repo verified twice -> byte-identical run_to_json output.
    records = [
        make_record("a", ("pkg/auth.py", "refresh")),
        make_record("b", ("pkg/missing.py", "x")),
        make_record("c", ("../escape.py", None)),
    ]
    v1, s1 = verify_records(str(tmp_repo), records)
    v2, s2 = verify_records(str(tmp_repo), records)
    assert run_to_json(v1, s1) == run_to_json(v2, s2)
