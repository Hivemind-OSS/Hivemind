"""censusctl — the in-container census-receipt ingest tool (the authctl sibling).

Injection seams (connect_fn / stdin / out / now / env) keep every path unit-testable
without Docker; the REAL unsigned S3 receipt (tests/data/receipt.real.json) ingesting
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
REAL_ANCHOR = (
    "matrix/_extract_monolith.py::LanguageConfig"  # a real subject of the receipt
)

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
    """One approved episode anchored under the LEGACY '' repo identity — the real
    receipt fixtures carry no provenance.repo, and the §3.6 join is repo-exact, so
    only ''-scoped anchor rows can meet them (v3 stage(anchors=)+complete(trust=))."""
    store = SqliteEpisodeStore(conn)  # creates the schema (IF NOT EXISTS)
    eid, _ = store.stage(
        text=text, weight=1.0, proposed_by="w", ts=10, anchors=[("", anchor)]
    )
    assert store.complete(
        eid,
        np.eye(DIM, dtype=np.float32)[0],
        expected_version=0,
        trust="provisional",
        last_active_ts=10,
    )
    return eid


def _rows(conn):
    return conn.execute(
        "SELECT episode_id, kind, actor, ts, payload FROM evidence_events ORDER BY id"
    ).fetchall()


def _run(argv, *, stdin_text=None, env=None, connect_fn=None, now=lambda: 777):
    out = io.StringIO()
    cf = connect_fn if connect_fn is not None else RecordingConnect()
    rc = censusctl.main(
        argv,
        env=env or {},
        connect_fn=cf,
        stdin=io.StringIO(stdin_text or ""),
        out=out,
        now=now,
    )
    return rc, out.getvalue(), cf


# ── db_path resolution (the exact BUG-013 zero-config shape) ───────────────────


def test_zero_config_db_path_defaults_to_data_shared_db():
    rc, _, cf = _run(["ingest", str(REAL_RECEIPT)])
    assert rc == censusctl.EX_OK
    assert cf.paths == ["/data/shared.db"]  # the in-container default, no env needed


def test_env_var_overrides_default_and_db_flag_wins():
    _, _, cf = _run(
        ["ingest", str(REAL_RECEIPT)], env={"HIVE_STORE__DB_PATH": "/tmp/env.db"}
    )
    assert cf.paths == ["/tmp/env.db"]
    _, _, cf2 = _run(
        ["--db", "/tmp/flag.db", "ingest", str(REAL_RECEIPT)],
        env={"HIVE_STORE__DB_PATH": "/tmp/env.db"},
    )
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
    assert json.loads(out_f) == json.loads(out_s)  # identical machine report
    assert len(_rows(cf_file.conn)) == len(_rows(cf_stdin.conn)) == 2


# ── the REAL signed receipt against a seeded real store (offline D7 half) ─────


def test_real_receipt_lands_change_outcome_and_verify_rows_with_the_receipt_shas():
    cf = RecordingConnect()
    eid = _seed_anchored(cf.conn, "LanguageConfig gotcha", REAL_ANCHOR)
    _seed_anchored(cf.conn, "unrelated memory", "other/place.py::Elsewhere")
    rc, out, _ = _run(["ingest", str(REAL_RECEIPT)], connect_fn=cf, now=lambda: 424242)
    assert rc == censusctl.EX_OK

    rows = _rows(cf.conn)
    assert len(rows) == 2  # one matched episode, two kinds
    row = rows[0]
    assert row["episode_id"] == eid
    assert row["kind"] == EK_CHANGE_OUTCOME and row["actor"] == "census"
    assert row["ts"] == 424242  # the injected clock

    envelope = json.loads(REAL_RECEIPT.read_text())
    statement = json.loads(base64.b64decode(envelope["payload"]))
    prov = statement["predicate"]["provenance"]
    body = json.loads(row["payload"])
    assert body["base_sha"] == prov["base_sha"]  # SHA-bound to the real change
    assert body["head_sha"] == prov["head_sha"]
    assert body["receipt_sha256"] == statement["subject"][0]["digest"]["sha256"]
    assert body["matched"] == {
        "path": "matrix/_extract_monolith.py",
        "symbol": "LanguageConfig",
        "level": "symbol",
    }
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
    assert (
        vbody["stamp"]["matrix_head"]["graph_sha256"]
        == prov["matrix"]["head"]["graph_sha256"]
    )
    # …and ZERO verified rows: the tests line is not_run and the regression reach is
    # empty — the honest abstention (only a DECIDED failing run with reach backwrites).
    report = json.loads(out)
    assert report["matched"] == 1 and len(report["inserted"]) == 2
    assert (report["verified_helped"], report["verified_hurt"]) == (0, 0)
    assert (report["verify_current"], report["verify_stale"]) == (0, 1)


SINGLEROOT_RECEIPT = _DATA / "receipt.singleroot.json"
SINGLEROOT_ANCHOR = "pkg/mod.py::fn"  # the v3 canonical path::Symbol spelling


def test_single_source_root_receipt_joins_a_repo_relative_anchor():
    """BUG-038's ingest leg: a receipt built by the REAL hive-census on a checkout
    whose parsed source ALL lives under one top-level dir (pkg/) must spell its
    subjects repo-root-relative, so an episode anchored ``pkg/mod.py::fn`` (the v3
    canonical ``path::Symbol`` grammar — the old single-colon agent dialect died
    with v2) joins and earns its change_outcome + verify evidence. Pre-fix, such
    receipts carried ZERO existence/contract lines (the graph's flattened dialect
    never met the diff's repo dialect) and this join matched nothing, silently."""
    cf = RecordingConnect()
    eid = _seed_anchored(cf.conn, "fn arity gotcha", SINGLEROOT_ANCHOR)
    _seed_anchored(cf.conn, "unrelated memory", "other/place.py::Elsewhere")
    rc, out, _ = _run(["ingest", str(SINGLEROOT_RECEIPT)], connect_fn=cf)
    assert rc == censusctl.EX_OK

    report = json.loads(out)
    assert report["matched"] == 1, report  # the repo-relative subject joined
    rows = _rows(cf.conn)
    assert [r["episode_id"] for r in rows] == [eid, eid]
    outcome = json.loads(rows[0]["payload"])
    assert rows[0]["kind"] == EK_CHANGE_OUTCOME
    assert outcome["matched"] == {
        "path": "pkg/mod.py",
        "symbol": "fn",
        "level": "symbol",
    }
    # fn was NARROWED at head (drift=breaking, still exists) ⇒ one verify_stale row.
    assert rows[1]["kind"] == EK_VERIFY_STALE
    vbody = json.loads(rows[1]["payload"])
    assert (vbody["exists_after"], vbody["drift"]) == (True, "breaking")


def test_reingest_of_the_same_receipt_is_idempotent():
    # layered dedupe: the range ledger absorbs a duplicate range first; on a store
    # whose rows landed BEFORE the ledger existed (range row absent — the legacy
    # transition case), content-keyed dedupe still absorbs every row, and the range
    # gets recorded for next time.
    cf = RecordingConnect()
    _seed_anchored(cf.conn, "LanguageConfig gotcha", REAL_ANCHOR)
    rc1, _, _ = _run(["ingest", str(REAL_RECEIPT)], connect_fn=cf)
    cf.conn.execute("DELETE FROM ingested_ranges")  # simulate a pre-ledger store
    rc2, out2, _ = _run(["ingest", str(REAL_RECEIPT)], connect_fn=cf)
    assert rc1 == rc2 == censusctl.EX_OK
    assert len(_rows(cf.conn)) == 2  # still the same two rows
    report = json.loads(out2)
    assert report["already_recorded"] == 2 and report["inserted"] == []
    assert report["range_skipped"] is False  # content dedupe absorbed, not the ledger
    assert (
        cf.conn.execute("SELECT COUNT(*) AS c FROM ingested_ranges").fetchone()["c"]
        == 1
    )


# ── the range ledger: manual ingest obeys the same durable dedupe ──────────────


def test_range_dedupe_skips():
    # the same receipt twice against ONE store: the second ingest is absorbed by the
    # durable range ledger (zero rows, range_skipped) yet still exits 0. The real
    # receipt carries no provenance.repo ⇒ this is the legacy "" repo identity.
    cf = RecordingConnect()
    _seed_anchored(cf.conn, "LanguageConfig gotcha", REAL_ANCHOR)
    rc1, out1, _ = _run(["ingest", str(REAL_RECEIPT)], connect_fn=cf)
    assert rc1 == censusctl.EX_OK
    assert json.loads(out1)["range_skipped"] is False
    rc2, out2, _ = _run(["ingest", str(REAL_RECEIPT)], connect_fn=cf)
    assert rc2 == censusctl.EX_OK
    report = json.loads(out2)
    assert report["range_skipped"] is True
    assert report["inserted"] == [] and report["matched"] == 0
    assert len(_rows(cf.conn)) == 2  # nothing new landed
    ledger = cf.conn.execute("SELECT repo, phase FROM ingested_ranges").fetchall()
    assert [(r["repo"], r["phase"]) for r in ledger] == [("", "pre_merge")]


def test_phase_distinguishes_ff_merge():
    # an FF merge yields the SAME (base, head) range pre- AND post-merge — phase in
    # the ledger key lets both receipts land instead of the post-merge one being
    # absorbed as a duplicate.
    cf = RecordingConnect()
    _seed_anchored(cf.conn, "LanguageConfig gotcha", REAL_ANCHOR)
    rc1, _, _ = _run(["ingest", str(REAL_RECEIPT)], connect_fn=cf)
    rc2, out2, _ = _run(
        [
            "ingest",
            str(REAL_RECEIPT),
            "--post-merge",
            "--verdict",
            "pass",
            "--signal",
            "canary",
        ],
        connect_fn=cf,
    )
    assert rc1 == rc2 == censusctl.EX_OK
    r2 = json.loads(out2)
    assert r2["range_skipped"] is False and len(r2["inserted"]) == 1
    phases = {
        json.loads(r["payload"])["phase"]
        for r in _rows(cf.conn)
        if json.loads(r["payload"]).get("schema") == "change_outcome/v1"
    }
    assert phases == {"pre_merge", "post_merge"}
    ledger_phases = [
        r["phase"]
        for r in cf.conn.execute(
            "SELECT phase FROM ingested_ranges ORDER BY phase DESC"
        )
    ]
    assert ledger_phases == ["pre_merge", "post_merge"]


# ── refusal semantics: exit 65, reason on stderr, ZERO rows, no report ─────────


def test_refused_receipt_exits_dataerr_with_zero_rows(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text('{"payloadType": "application/vnd.in-toto+json"}')  # no payload
    cf = RecordingConnect()
    _seed_anchored(cf.conn, "LanguageConfig gotcha", REAL_ANCHOR)
    rc, out, _ = _run(["ingest", str(bad)], connect_fn=cf)
    assert rc == censusctl.EX_DATAERR
    assert _rows(cf.conn) == []  # zero rows written
    assert out == ""  # no machine report on refusal
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
    predicate = {
        "schema_version": "v0",
        "lines": [
            {
                "class": "tests",
                "subject": "tests",
                "tag": "unverified",
                "detail": {"state": "not_run"},
                "reason": "",
            }
        ],
        "provenance": {"base_sha": "a" * 40, "head_sha": "b" * 40},
    }
    canon = json.dumps(
        predicate, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    import hashlib

    statement = {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": [
            {
                "name": "hive-census-receipt",
                "digest": {"sha256": hashlib.sha256(canon).hexdigest()},
            }
        ],
        "predicateType": "urn:hive-census:receipt:v0",
        "predicate": predicate,
    }
    envelope = {
        "payload": base64.b64encode(
            json.dumps(
                statement, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode()
        ).decode(),
        "payloadType": "application/vnd.in-toto+json",
        "signatures": [{"keyid": "k", "sig": "cw=="}],
    }
    p = tmp_path / "undecided.json"
    p.write_text(json.dumps(envelope))
    cf = RecordingConnect()
    _seed_anchored(cf.conn, "LanguageConfig gotcha", REAL_ANCHOR)
    rc, out, _ = _run(["ingest", str(p)], connect_fn=cf)
    assert rc == censusctl.EX_DATAERR and out == ""
    assert _rows(cf.conn) == []
    assert "decided" in capsys.readouterr().err.lower()


# ── post-merge flags thread through to the derived tag ─────────────────────────


def test_post_merge_decided_lines_derive_the_verdict_never_the_caller():
    # v3 §3.3: DECIDED execution lines derive the verdict even post-merge — the real
    # S3 receipt's typecheck decided-FAILED, so the caller's asserted `pass` never
    # rides, and the tag caps at bounded-estimate (post-merge execution lines never
    # self-certify machine-checked; that claim needs a randomized/canary signal on an
    # UNDECIDED receipt). The signal still threads verbatim.
    cf = RecordingConnect()
    _seed_anchored(cf.conn, "LanguageConfig gotcha", REAL_ANCHOR)
    rc, out, _ = _run(
        [
            "ingest",
            str(REAL_RECEIPT),
            "--post-merge",
            "--verdict",
            "pass",
            "--signal",
            "canary",
        ],
        connect_fn=cf,
    )
    assert rc == censusctl.EX_OK
    body = json.loads(_rows(cf.conn)[0]["payload"])
    assert body["phase"] == "post_merge" and body["verdict"] == "fail"
    assert body["tag"] == "bounded-estimate" and body["signal"] == "canary"


def _undecided_envelope() -> dict:
    """A shape-valid receipt whose only execution evidence is not_run — nothing
    decided, so pre-merge REFUSES it and post-merge falls to the caller's verdict.
    Carries ONE joinable existence line so a matched episode earns the row."""
    predicate = {
        "schema_version": "v0",
        "lines": [
            {
                "class": "tests",
                "subject": "tests",
                "tag": "unverified",
                "detail": {"state": "not_run"},
                "reason": "",
            },
            {
                "class": "existence",
                "subject": REAL_ANCHOR,
                "detail": {"existed_before": True, "exists_after": True},
                "reason": "",
                "tag": "machine-checked",
            },
        ],
        "provenance": {"base_sha": "a" * 40, "head_sha": "b" * 40},
    }
    canon = json.dumps(
        predicate, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    import hashlib

    statement = {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": [
            {
                "name": "hive-census-receipt",
                "digest": {"sha256": hashlib.sha256(canon).hexdigest()},
            }
        ],
        "predicateType": "urn:hive-census:receipt:v0",
        "predicate": predicate,
    }
    return {
        "payload": base64.b64encode(
            json.dumps(
                statement, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode()
        ).decode(),
        "payloadType": "application/vnd.in-toto+json",
        "signatures": [{"keyid": "k", "sig": "cw=="}],
    }


def test_post_merge_undecided_takes_caller_verdict_with_signal_tag(tmp_path):
    # the landed-line parity fallback: nothing decided ⇒ the CALLER's verdict rides,
    # tagged by the signal — none ⇒ unverified-judgment (the §6.2.5 canary rule).
    p = tmp_path / "undecided.json"
    p.write_text(json.dumps(_undecided_envelope()))
    cf = RecordingConnect()
    _seed_anchored(cf.conn, "LanguageConfig gotcha", REAL_ANCHOR)
    rc, _, _ = _run(
        ["ingest", str(p), "--post-merge", "--verdict", "fail"], connect_fn=cf
    )
    assert rc == censusctl.EX_OK
    body = json.loads(_rows(cf.conn)[0]["payload"])
    assert body["verdict"] == "fail"  # caller-asserted (nothing decided)
    assert body["tag"] == "unverified-judgment" and body["signal"] == "none"


def test_post_merge_without_verdict_is_an_argparse_usage_error():
    with pytest.raises(SystemExit) as exc:
        censusctl.main(
            ["ingest", str(REAL_RECEIPT), "--post-merge"],
            env={},
            connect_fn=RecordingConnect(),
            stdin=io.StringIO(""),
            out=io.StringIO(),
        )
    assert exc.value.code == 2  # argparse usage-error convention


# ── the machine report: ONE JSON line on stdout, exact key set ─────────────────


def test_report_golden_one_json_line_on_stdout():
    cf = RecordingConnect()
    _seed_anchored(cf.conn, "LanguageConfig gotcha", REAL_ANCHOR)
    rc, out, _ = _run(["ingest", str(REAL_RECEIPT)], connect_fn=cf)
    assert rc == censusctl.EX_OK
    lines = out.splitlines()
    assert len(lines) == 1  # stdout is the report, nothing else
    report = json.loads(lines[0])
    assert set(report) == {
        "inserted",
        "already_recorded",
        "matched",
        "range_skipped",
        "skipped_lines",
        "verified_helped",
        "verified_hurt",
        "verify_current",
        "verify_stale",
        "stale_suspects",
    }
    assert lines[0] == json.dumps(
        {
            "already_recorded": 0,
            "inserted": report["inserted"],
            "matched": 1,
            "range_skipped": False,
            "skipped_lines": 0,
            "stale_suspects": 0,
            "verified_helped": 0,
            "verified_hurt": 0,
            "verify_current": 0,
            "verify_stale": 1,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def test_matched_zero_is_success_with_an_honest_report():
    rc, out, cf = _run(["ingest", str(REAL_RECEIPT)])  # empty store: nothing anchored
    assert rc == censusctl.EX_OK
    report = json.loads(out)
    assert report["matched"] == 0 and report["inserted"] == []
    assert _rows(cf.conn) == []  # byte-inert store


# ── internal faults fail fast (70) ─────────────────────────────────────────────


def test_internal_fault_exits_software(capsys):
    def _boom(path):
        raise RuntimeError("db locked beyond busy_timeout")

    rc = censusctl.main(
        ["ingest", str(REAL_RECEIPT)],
        env={},
        connect_fn=_boom,
        stdin=io.StringIO(""),
        out=io.StringIO(),
    )
    assert rc == censusctl.EX_SOFTWARE
    assert "internal" in capsys.readouterr().err.lower()
