"""censusctl — the in-container census-receipt ingest tool (the authctl sibling).

Injection seams (connect_fn / stdin / out / now / env) keep every path unit-testable
without Docker; the REAL signed S3 receipt (tests/data/receipt.real.json) ingesting
against a seeded real :memory: store is the offline half of D7. Exit codes are the
contract: 0 ok, 65 refused (bad receipt — zero rows), 70 internal fault."""
from __future__ import annotations

import base64
import io
import json
import pathlib

import numpy as np
import pytest

from hive.adapters.sqlite_db import connect
from hive.adapters.store_sqlite import SqliteEpisodeStore
from hive.domain.evidence_kinds import EK_CHANGE_OUTCOME, EK_VERIFY_STALE
from hive.tools import censusctl

_DATA = pathlib.Path(__file__).resolve().parents[1] / "data"
REAL_RECEIPT = _DATA / "receipt.real.json"
REAL_ANCHOR = "matrix/_extract_monolith.py::LanguageConfig"   # a real subject of the receipt

DIM = 4


class RecordingConnect:
    """Records requested db paths; hands back ONE shared in-memory conn so the test
    can inspect what censusctl wrote after main() returns."""

    def __init__(self):
        self.paths: list[str] = []
        self.conn = connect(":memory:")

    def __call__(self, path: str):
        self.paths.append(path)
        return self.conn


def _seed_anchored(conn, text: str, anchor: str) -> int:
    store = SqliteEpisodeStore(conn)                 # creates the schema (IF NOT EXISTS)
    eid, _ = store.stage(text=text, weight=1.0, tags="", proposed_by="w", ts=10,
                         anchor=anchor)
    assert store.approve(eid, "h", np.eye(DIM, dtype=np.float32)[0],
                         expected_version=0, approved_ts=10)
    return eid


def _rows(conn):
    return conn.execute(
        "SELECT episode_id, kind, actor, ts, payload FROM evidence_events "
        "ORDER BY id").fetchall()


def _run(argv, *, stdin_text=None, env=None, connect_fn=None, now=lambda: 777):
    out = io.StringIO()
    cf = connect_fn if connect_fn is not None else RecordingConnect()
    rc = censusctl.main(argv, env=env or {}, connect_fn=cf,
                        stdin=io.StringIO(stdin_text or ""), out=out, now=now)
    return rc, out.getvalue(), cf


# ── db_path resolution (the exact BUG-013 zero-config shape) ───────────────────


def test_zero_config_db_path_defaults_to_data_shared_db():
    rc, _, cf = _run(["ingest", str(REAL_RECEIPT)])
    assert rc == censusctl.EX_OK
    assert cf.paths == ["/data/shared.db"]           # the in-container default, no env needed


def test_env_var_overrides_default_and_db_flag_wins():
    _, _, cf = _run(["ingest", str(REAL_RECEIPT)],
                    env={"HIVE_STORE__DB_PATH": "/tmp/env.db"})
    assert cf.paths == ["/tmp/env.db"]
    _, _, cf2 = _run(["--db", "/tmp/flag.db", "ingest", str(REAL_RECEIPT)],
                     env={"HIVE_STORE__DB_PATH": "/tmp/env.db"})
    assert cf2.paths == ["/tmp/flag.db"]


# ── input plumbing: file and stdin ('-') both parse ────────────────────────────


def test_stdin_dash_and_file_input_are_equivalent():
    receipt_text = REAL_RECEIPT.read_text()
    cf_file = RecordingConnect()
    _seed_anchored(cf_file.conn, "file-fed memory", REAL_ANCHOR)
    rc_f, out_f, _ = _run(["ingest", str(REAL_RECEIPT)], connect_fn=cf_file)

    cf_stdin = RecordingConnect()
    _seed_anchored(cf_stdin.conn, "stdin-fed memory", REAL_ANCHOR)
    rc_s, out_s, _ = _run(["ingest", "-"], stdin_text=receipt_text, connect_fn=cf_stdin)

    assert rc_f == rc_s == censusctl.EX_OK
    assert json.loads(out_f) == json.loads(out_s)    # identical machine report
    assert len(_rows(cf_file.conn)) == len(_rows(cf_stdin.conn)) == 2


# ── the REAL signed receipt against a seeded real store (offline D7 half) ─────


def test_real_receipt_lands_change_outcome_and_verify_rows_with_the_receipt_shas():
    cf = RecordingConnect()
    eid = _seed_anchored(cf.conn, "LanguageConfig gotcha", REAL_ANCHOR)
    _seed_anchored(cf.conn, "unrelated memory", "other/place.py::Elsewhere")
    rc, out, _ = _run(["ingest", str(REAL_RECEIPT)], connect_fn=cf, now=lambda: 424242)
    assert rc == censusctl.EX_OK

    rows = _rows(cf.conn)
    assert len(rows) == 2                            # one matched episode, two kinds
    row = rows[0]
    assert row["episode_id"] == eid
    assert row["kind"] == EK_CHANGE_OUTCOME and row["actor"] == "census"
    assert row["ts"] == 424242                       # the injected clock

    envelope = json.loads(REAL_RECEIPT.read_text())
    statement = json.loads(base64.b64decode(envelope["payload"]))
    prov = statement["predicate"]["provenance"]
    body = json.loads(row["payload"])
    assert body["base_sha"] == prov["base_sha"]      # SHA-bound to the real change
    assert body["head_sha"] == prov["head_sha"]
    assert body["receipt_sha256"] == statement["subject"][0]["digest"]["sha256"]
    assert body["matched"] == {"path": "matrix/_extract_monolith.py",
                               "symbol": "LanguageConfig", "level": "symbol"}
    # the real S3 receipt: typecheck decided-failed at bounded-estimate
    assert body["phase"] == "pre_merge"
    assert body["verdict"] == "fail" and body["tag"] == "bounded-estimate"

    # the Flow A rider on the real receipt: LanguageConfig was REMOVED at head
    # (existence exists_after=false, contract drift=removed) ⇒ one verify_stale row,
    # SHA-and-version-stamped from the receipt's combdrift + matrix.head blocks…
    verify = rows[1]
    assert verify["episode_id"] == eid and verify["kind"] == EK_VERIFY_STALE
    vbody = json.loads(verify["payload"])
    assert vbody["schema"] == "verify/v1"
    assert (vbody["exists_after"], vbody["drift"]) == (False, "removed")
    assert vbody["stamp"]["head_sha"] == prov["head_sha"]
    assert vbody["stamp"]["matrix_head"]["graph_sha256"] == \
        prov["matrix"]["head"]["graph_sha256"]
    # …and ZERO verified rows: the tests line is not_run and the regression reach is
    # empty — the honest abstention (only a DECIDED failing run with reach backwrites).
    report = json.loads(out)
    assert report["matched"] == 1 and len(report["inserted"]) == 2
    assert report["keyid"] == envelope["signatures"][0]["keyid"]
    assert (report["verified_helped"], report["verified_hurt"]) == (0, 0)
    assert (report["verify_current"], report["verify_stale"]) == (0, 1)


def test_reingest_of_the_same_receipt_is_idempotent():
    cf = RecordingConnect()
    _seed_anchored(cf.conn, "LanguageConfig gotcha", REAL_ANCHOR)
    rc1, _, _ = _run(["ingest", str(REAL_RECEIPT)], connect_fn=cf)
    rc2, out2, _ = _run(["ingest", str(REAL_RECEIPT)], connect_fn=cf)
    assert rc1 == rc2 == censusctl.EX_OK
    assert len(_rows(cf.conn)) == 2                  # still the same two rows
    report = json.loads(out2)
    assert report["already_recorded"] == 2 and report["inserted"] == []


# ── refusal semantics: exit 65, reason on stderr, ZERO rows, no report ─────────


def test_refused_receipt_exits_dataerr_with_zero_rows(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text('{"payloadType": "application/vnd.in-toto+json"}')   # no payload
    cf = RecordingConnect()
    _seed_anchored(cf.conn, "LanguageConfig gotcha", REAL_ANCHOR)
    rc, out, _ = _run(["ingest", str(bad)], connect_fn=cf)
    assert rc == censusctl.EX_DATAERR
    assert _rows(cf.conn) == []                      # zero rows written
    assert out == ""                                 # no machine report on refusal
    assert "refused" in capsys.readouterr().err.lower()


def test_not_json_receipt_exits_dataerr(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text("this is not json {")
    rc, out, _ = _run(["ingest", str(bad)])
    assert rc == censusctl.EX_DATAERR and out == ""
    assert "json" in capsys.readouterr().err.lower()


def test_unreadable_receipt_path_exits_dataerr(capsys):
    rc, out, _ = _run(["ingest", "/no/such/receipt.json"])
    assert rc == censusctl.EX_DATAERR and out == ""
    assert "read" in capsys.readouterr().err.lower()


def test_nothing_decided_receipt_refuses(tmp_path, capsys):
    # a shape-valid receipt whose only execution evidence is not_run: REFUSE, no row.
    predicate = {"schema_version": "v0",
                 "lines": [{"class": "tests", "subject": "tests", "tag": "unverified",
                            "detail": {"state": "not_run"}, "reason": ""}],
                 "provenance": {"base_sha": "a" * 40, "head_sha": "b" * 40}}
    canon = json.dumps(predicate, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False).encode()
    import hashlib
    statement = {"_type": "https://in-toto.io/Statement/v1",
                 "subject": [{"name": "hive-census-receipt",
                              "digest": {"sha256": hashlib.sha256(canon).hexdigest()}}],
                 "predicateType": "urn:hive-census:receipt:v0", "predicate": predicate}
    envelope = {"payload": base64.b64encode(json.dumps(
        statement, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False).encode()).decode(),
        "payloadType": "application/vnd.in-toto+json",
        "signatures": [{"keyid": "k", "sig": "cw=="}]}
    p = tmp_path / "undecided.json"
    p.write_text(json.dumps(envelope))
    cf = RecordingConnect()
    _seed_anchored(cf.conn, "LanguageConfig gotcha", REAL_ANCHOR)
    rc, out, _ = _run(["ingest", str(p)], connect_fn=cf)
    assert rc == censusctl.EX_DATAERR and out == ""
    assert _rows(cf.conn) == []
    assert "decided" in capsys.readouterr().err.lower()


# ── post-merge flags thread through to the derived tag ─────────────────────────


def test_post_merge_flags_thread_to_the_payload():
    cf = RecordingConnect()
    _seed_anchored(cf.conn, "LanguageConfig gotcha", REAL_ANCHOR)
    rc, out, _ = _run(["ingest", str(REAL_RECEIPT), "--post-merge",
                       "--verdict", "pass", "--signal", "canary"], connect_fn=cf)
    assert rc == censusctl.EX_OK
    body = json.loads(_rows(cf.conn)[0]["payload"])
    assert body["phase"] == "post_merge" and body["verdict"] == "pass"
    assert body["tag"] == "machine-checked" and body["signal"] == "canary"


def test_post_merge_unsignaled_is_unverified_judgment():
    cf = RecordingConnect()
    _seed_anchored(cf.conn, "LanguageConfig gotcha", REAL_ANCHOR)
    _run(["ingest", str(REAL_RECEIPT), "--post-merge", "--verdict", "fail"],
         connect_fn=cf)
    body = json.loads(_rows(cf.conn)[0]["payload"])
    assert body["tag"] == "unverified-judgment" and body["signal"] == "none"


def test_post_merge_without_verdict_is_an_argparse_usage_error():
    with pytest.raises(SystemExit) as exc:
        censusctl.main(["ingest", str(REAL_RECEIPT), "--post-merge"],
                       env={}, connect_fn=RecordingConnect(),
                       stdin=io.StringIO(""), out=io.StringIO())
    assert exc.value.code == 2                       # argparse usage-error convention


# ── the machine report: ONE JSON line on stdout, exact key set ─────────────────


def test_report_golden_one_json_line_on_stdout():
    cf = RecordingConnect()
    _seed_anchored(cf.conn, "LanguageConfig gotcha", REAL_ANCHOR)
    rc, out, _ = _run(["ingest", str(REAL_RECEIPT)], connect_fn=cf)
    assert rc == censusctl.EX_OK
    lines = out.splitlines()
    assert len(lines) == 1                           # stdout is the report, nothing else
    report = json.loads(lines[0])
    assert set(report) == {"inserted", "already_recorded", "matched",
                           "skipped_lines", "keyid", "verified_helped",
                           "verified_hurt", "verify_current", "verify_stale",
                           "stale_suspects"}
    envelope = json.loads(REAL_RECEIPT.read_text())
    assert lines[0] == json.dumps(
        {"already_recorded": 0, "inserted": report["inserted"],
         "keyid": envelope["signatures"][0]["keyid"], "matched": 1,
         "skipped_lines": 0, "stale_suspects": 0, "verified_helped": 0,
         "verified_hurt": 0, "verify_current": 0, "verify_stale": 1},
        sort_keys=True, separators=(",", ":"))


def test_matched_zero_is_success_with_an_honest_report():
    rc, out, cf = _run(["ingest", str(REAL_RECEIPT)])   # empty store: nothing anchored
    assert rc == censusctl.EX_OK
    report = json.loads(out)
    assert report["matched"] == 0 and report["inserted"] == []
    assert _rows(cf.conn) == []                      # byte-inert store


# ── internal faults fail fast (70) ─────────────────────────────────────────────


def test_internal_fault_exits_software(capsys):
    def _boom(path):
        raise RuntimeError("db locked beyond busy_timeout")
    rc = censusctl.main(["ingest", str(REAL_RECEIPT)], env={}, connect_fn=_boom,
                        stdin=io.StringIO(""), out=io.StringIO())
    assert rc == censusctl.EX_SOFTWARE
    assert "internal" in capsys.readouterr().err.lower()
