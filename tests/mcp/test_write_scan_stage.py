"""M06 hive_write: the three-branch secret floor wired into the v3 tool — PASS lands
provisional + recallable, REDACT lands provisional with MASKED text (no raw secret
bytes, hash over post-redaction text), REFUSE writes NOTHING (0 rows). The secret scan
is the SOLE always-on gate — a planted credential is refused on the direct path exactly
as before (the chunk's mutation pin)."""

from __future__ import annotations

from hive.domain.models import content_hash
from hive.domain.secret_scan import REDACT
from tests.fakes._fakes import FakeScanner
from tests.mcp._helpers import build_real_server, content, register_repo, tool_call

_SECRET = "deploy key sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345 do not leak"


def test_write_clean_lands_provisional():
    server, _ = build_real_server()
    out = content(tool_call(server, "hive_write", {"text": "a clean durable insight"}))
    assert out["status"] == "approved"
    assert isinstance(out["id"], int)
    assert out["trust"] == "provisional"
    assert "approved_by" not in out  # no approver exists in v3
    assert out["scan"]["action"] == "clean"
    assert server.store.counts() == (1, 0)  # 1 approved, 0 pending


def test_write_redact_stores_masked_text_no_raw_secret():
    server, _ = build_real_server(scanner=FakeScanner(mode=REDACT))
    out = content(tool_call(server, "hive_write", {"text": _SECRET}))
    assert out["status"] == "redacted"
    assert out["trust"] == "provisional"
    # the stored row holds the MASKED text — none of the raw secret bytes
    stored = server.store.get_episode(out["id"]).text
    assert "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345" not in stored
    assert "[REDACTED]" in stored
    # the stored row's content_hash binds the POST-redaction text (over the mask, not the secret)
    assert server.store.get_episode(out["id"]).content_hash == content_hash(stored)
    # the raw secret appears nowhere in the envelope
    assert "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345" not in out["redacted_preview"]
    assert out["scan"]["action"] == "redact"
    # a redacted write is ALSO materialized + recallable (no pending state survives)
    assert server.store.get_episode(out["id"]).status == "approved"


def test_write_planted_secret_refused_before_write():
    server, _ = build_real_server(scanner=FakeScanner())  # default: REFUSE
    out = content(tool_call(server, "hive_write", {"text": _SECRET}))
    assert out["status"] == "refused"
    assert out["scan"]["action"] == "refuse"
    assert out["scan"]["rules"]  # named rule(s)
    assert out["scan"]["n_findings"] >= 1
    # NOTHING written: 0 rows, and the secret is in no blob
    assert server.store.counts() == (0, 0)
    assert (
        server.store.conn.execute(
            "SELECT 1 FROM blobs WHERE content_hash=?", (content_hash(_SECRET),)
        ).fetchone()
        is None
    )


def test_write_refused_with_declared_branch_stores_no_ref_row():
    # the scan gate runs BEFORE stage(): a declared branch on a refused write must
    # not leak an episode_refs row when nothing else is stored either.
    server, _ = build_real_server(scanner=FakeScanner())  # default: REFUSE
    register_repo(server, "alpha")
    out = content(
        tool_call(server, "hive_write", {"text": _SECRET, "repos": ["alpha@feature"]})
    )
    assert out["status"] == "refused"
    assert server.store.counts() == (0, 0)
    n = server.store.conn.execute("SELECT COUNT(*) FROM episode_refs").fetchone()[0]
    assert n == 0


def test_refused_envelope_carries_no_secret_bytes():
    server, _ = build_real_server(scanner=FakeScanner())
    out = content(tool_call(server, "hive_write", {"text": _SECRET}))
    import json as _json

    blob = _json.dumps(out)
    assert "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345" not in blob


# ── the refusal names WHICH SPAN fired (BUG-018: names-not-bytes ≠ names-only) ──
def test_refused_envelope_carries_the_finding_span():
    server, _ = build_real_server(scanner=FakeScanner())
    out = content(tool_call(server, "hive_write", {"text": _SECRET}))
    findings = out["scan"]["findings"]
    assert len(findings) == out["scan"]["n_findings"] >= 1
    f = findings[0]
    assert f["rule"] == "openai_key"
    # the offsets index back into the SUBMITTED text — the writer can locate the run
    # it must fix instead of bisecting its own memory by hand
    assert _SECRET[f["start"] : f["end"]] == "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"
    # …and the span is offsets only: deleting it from the envelope is the mutation
    assert isinstance(f["start"], int) and isinstance(f["end"], int)


def test_refused_reason_string_names_the_span_too():
    # a client that surfaces only `reason` must still see the span (the reason IS
    # the message SecretRefused builds from its findings)
    server, _ = build_real_server(scanner=FakeScanner())
    out = content(tool_call(server, "hive_write", {"text": _SECRET}))
    f = out["scan"]["findings"][0]
    assert f"spans=[[{f['start']}, {f['end']}]]" in out["reason"]
    assert "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345" not in out["reason"]


def test_capture_refusal_shares_the_same_envelope():
    # write / capture / flag render the refusal through ONE owner — they cannot fork
    server, _ = build_real_server(scanner=FakeScanner())
    out = content(tool_call(server, "hive_capture", {"text": _SECRET}))
    assert out["status"] == "refused" and out["scan"]["action"] == "refuse"
    assert out["scan"]["findings"][0]["rule"] == "openai_key"
    assert server.store.counts() == (0, 0)


def test_clean_write_reports_empty_findings_list():
    # the clean report carries the same key, empty — one ScanReport shape, always
    server, _ = build_real_server()
    out = content(tool_call(server, "hive_write", {"text": "a clean durable insight"}))
    assert out["scan"]["findings"] == [] and out["scan"]["n_findings"] == 0


def test_write_of_identifier_shaped_memory_text_is_admitted():
    """BUG-018 end-to-end: the memory text that took the live floor offline —
    a slash-joined list of config field names — now lands instead of refusing."""
    server, _ = build_real_server()
    text = (
        "sync capacity knobs: interval_s/webhook_secret/mirror_dir/"
        "drift_per_tick/backfill_per_tick/workers live on hive/app/config.py::SyncConfig"
    )
    out = content(tool_call(server, "hive_write", {"text": text}))
    assert out["status"] == "approved"
    assert out["scan"]["action"] == "clean" and out["scan"]["findings"] == []
