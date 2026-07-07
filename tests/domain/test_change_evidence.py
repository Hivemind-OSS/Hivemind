"""hive.domain.change_evidence — the pure change-outcome feed: DSSE receipt parsing (D8:
refuse the receipt, under-claim the line), verdict/tag derivation (server-derived, never
caller-asserted — the INV-2 analog; the §6.2.5 canary rule is unconstructable to violate),
the deterministic precision-first change→episode join, the canonical payload renderer
(the idempotency key), and the ports-driven ChangeEvidenceService (fake-port, ms-fast).
"""
from __future__ import annotations

import base64
import dataclasses
import hashlib
import inspect
import json

import pytest

from hive.domain import change_evidence as ce
from hive.domain.change_evidence import (
    ChangeEvidenceService, ChangeOutcome, IngestReport, ReceiptRefused, TouchedSubject,
    derive_post_merge_tag, derive_pre_merge, match_anchors, parse_receipt,
    render_payload, touched_subjects,
)
from hive.domain.evidence_kinds import EK_CHANGE_OUTCOME

BASE = "a" * 40
HEAD = "b" * 40


def _canon(doc) -> bytes:
    return json.dumps(doc, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def _receipt(lines=None, *, base_sha=BASE, head_sha=HEAD, schema_version="v0",
             census_version="0.1.0",
             predicate_type="urn:hive-census:receipt:v0",
             statement_type="https://in-toto.io/Statement/v1",
             payload_type="application/vnd.in-toto+json",
             digest=None, keyid="key-1", provenance=...):
    """A well-formed DSSE envelope around an in-toto receipt statement (the census
    envelope.py shape); every knob exists so one test can break one thing."""
    if provenance is ...:
        provenance = {"base_sha": base_sha, "head_sha": head_sha,
                      "hive_census_version": census_version}
    predicate = {"schema_version": schema_version, "lines": lines or [],
                 "provenance": provenance}
    d = digest if digest is not None else hashlib.sha256(_canon(predicate)).hexdigest()
    statement = {"_type": statement_type,
                 "subject": [{"name": "hive-census-receipt", "digest": {"sha256": d}}],
                 "predicateType": predicate_type, "predicate": predicate}
    return {"payload": base64.b64encode(_canon(statement)).decode("ascii"),
            "payloadType": payload_type,
            "signatures": [{"keyid": keyid, "sig": "c2ln"}]}


def _exec_line(cls, state, tag="machine-checked"):
    return {"class": cls, "subject": cls, "tag": tag,
            "detail": {"state": state}, "reason": ""}


def _subj_line(cls, subject, tag="machine-checked"):
    return {"class": cls, "subject": subject, "tag": tag, "detail": {}, "reason": ""}


class FakeReader:
    def __init__(self, rows):
        self.rows = list(rows)

    def anchored_episodes(self):
        return list(self.rows)


class FakeAppender:
    """Honors the ChangeEvidenceAppender contract: content-keyed skip, batch-atomic."""

    def __init__(self):
        self.batches: list[list[tuple]] = []
        self._seen: set[tuple] = set()
        self._next_id = 0

    def append_evidence(self, rows):
        self.batches.append(list(rows))
        inserted, skipped = [], 0
        for eid, kind, actor, ts, payload in rows:
            key = (eid, kind, payload)
            if key in self._seen:
                skipped += 1
                continue
            self._seen.add(key)
            self._next_id += 1
            inserted.append(self._next_id)
        return inserted, skipped


def _service(reader_rows=(), *, now=lambda: 12_345):
    reader, appender = FakeReader(reader_rows), FakeAppender()
    return ChangeEvidenceService(reader=reader, appender=appender, now=now), appender


# ── parse_receipt: receipt-global malformation REFUSES (D8, zero rows) ─────────


@pytest.mark.parametrize("envelope, reason_fragment", [
    (None, "not a JSON object"),
    ([1, 2], "not a JSON object"),
    ({}, "payloadType"),                           # dict with NO payloadType key
    ({"payloadType": "application/vnd.in-toto+json"}, "payload"),
    ({"payload": "!!!not-base64!!!", "payloadType": "application/vnd.in-toto+json"},
     "base64"),
    ({"payload": base64.b64encode(b"{not json").decode(),
      "payloadType": "application/vnd.in-toto+json"}, "JSON"),
    ({"payload": base64.b64encode(b"[1,2]").decode(),
      "payloadType": "application/vnd.in-toto+json"}, "object"),
])
def test_malformed_envelope_refuses_never_crashes(envelope, reason_fragment):
    with pytest.raises(ReceiptRefused) as exc:
        parse_receipt(envelope)
    assert reason_fragment.lower() in str(exc.value).lower()


def test_wrong_payload_type_refuses():
    with pytest.raises(ReceiptRefused, match="payloadType"):
        parse_receipt(_receipt(payload_type="application/json"))


def test_wrong_statement_type_refuses():
    with pytest.raises(ReceiptRefused, match="Statement"):
        parse_receipt(_receipt(statement_type="https://example.com/other/v9"))


def test_unknown_predicate_type_refuses_never_guesses():
    with pytest.raises(ReceiptRefused, match="predicateType"):
        parse_receipt(_receipt(predicate_type="urn:other:receipt:v9"))


def test_digest_mismatch_refuses():
    with pytest.raises(ReceiptRefused, match="digest"):
        parse_receipt(_receipt(digest="0" * 64))


def test_tampered_predicate_bytes_refuse():
    # bit-flip the embedded predicate while keeping the original digest: integrity red.
    env = _receipt([_exec_line("tests", "passed")])
    statement = json.loads(base64.b64decode(env["payload"]))
    statement["predicate"]["provenance"]["head_sha"] = "c" * 40   # the tamper
    env["payload"] = base64.b64encode(_canon(statement)).decode("ascii")
    with pytest.raises(ReceiptRefused, match="digest"):
        parse_receipt(env)


@pytest.mark.parametrize("prov", [
    {},                                              # both absent
    {"base_sha": BASE},                              # head absent
    {"base_sha": "", "head_sha": HEAD},              # blank base
    {"base_sha": BASE, "head_sha": "   "},           # whitespace head
    "not-a-dict",                                    # wrong type
])
def test_missing_provenance_shas_refuse(prov):
    with pytest.raises(ReceiptRefused, match="sha"):
        parse_receipt(_receipt(provenance=prov))


def test_valid_receipt_parses_and_surfaces_keyid():
    statement, keyid = parse_receipt(_receipt([_exec_line("tests", "passed")]))
    assert statement["predicateType"] == "urn:hive-census:receipt:v0"
    assert keyid == "key-1"


def test_absent_signatures_surface_empty_keyid_not_a_refusal():
    # verification stays census-side; the kernel surfaces the keyid, no crypto dep.
    env = _receipt([_exec_line("tests", "passed")])
    del env["signatures"]
    _, keyid = parse_receipt(env)
    assert keyid == ""


# ── verdict + tag derivation (server-derived; §3.3) ────────────────────────────


def test_pre_merge_any_decided_fail_is_fail():
    verdict, _ = derive_pre_merge([_exec_line("typecheck", "failed"),
                                   _exec_line("tests", "passed")])
    assert verdict == "fail"


def test_pre_merge_all_decided_pass_machine_checked():
    assert derive_pre_merge([_exec_line("typecheck", "passed"),
                             _exec_line("tests", "passed")]) == ("pass", "machine-checked")


def test_pre_merge_scoped_pyright_downgrades_to_bounded_estimate():
    # receipt tags are reused verbatim (PCE D3): one decided line at bounded-estimate
    # caps the outcome tag, even when the other decided line is machine-checked.
    verdict, tag = derive_pre_merge([
        _exec_line("typecheck", "passed", tag="bounded-estimate"),
        _exec_line("tests", "passed")])
    assert (verdict, tag) == ("pass", "bounded-estimate")


@pytest.mark.parametrize("lines", [
    [],
    [_exec_line("tests", "not_run")],
    [_exec_line("typecheck", "errored")],                      # errored is NOT decided
    [_subj_line("existence", "a.py::F"), _subj_line("regression", "b.py::G")],
])
def test_pre_merge_nothing_decided_refuses(lines):
    with pytest.raises(ReceiptRefused, match="decided"):
        derive_pre_merge(lines)


def test_pre_merge_errored_never_enters_the_verdict():
    # "tooling broke" is not "change failed": an errored typecheck beside a passing
    # test run stays a pass.
    assert derive_pre_merge([_exec_line("typecheck", "errored"),
                             _exec_line("tests", "passed")])[0] == "pass"


def test_post_merge_tag_follows_the_canary_rule():
    assert derive_post_merge_tag("randomized") == "machine-checked"
    assert derive_post_merge_tag("canary") == "machine-checked"
    assert derive_post_merge_tag("none") == "unverified-judgment"
    assert derive_post_merge_tag("gut-feel") == "unverified-judgment"


# ── ChangeOutcome carrier: illegal states unconstructable (Law 2) ─────────────


def _outcome(**kw):
    base = dict(base_sha=BASE, head_sha=HEAD, receipt_sha256="d" * 64,
                receipt_schema_version="v0",
                predicate_type="urn:hive-census:receipt:v0",
                phase="pre_merge", verdict="pass", tag="machine-checked")
    base.update(kw)
    return ChangeOutcome(**base)


def test_forged_post_merge_machine_checked_without_signal_is_unconstructable():
    with pytest.raises(ValueError, match="signal"):
        _outcome(phase="post_merge", tag="machine-checked", signal="none")


def test_post_merge_machine_checked_with_canary_constructs():
    out = _outcome(phase="post_merge", tag="machine-checked", signal="canary")
    assert out.tag == "machine-checked"


@pytest.mark.parametrize("kw", [
    {"phase": "mid_merge"}, {"verdict": "maybe"}, {"tag": "vibes"},
    {"signal": "hunch"}, {"base_sha": ""}, {"head_sha": "  "}, {"receipt_sha256": ""},
])
def test_carrier_enum_and_sha_guards_raise(kw):
    with pytest.raises(ValueError):
        _outcome(**kw)


def test_carriers_are_frozen():
    out = _outcome()
    with pytest.raises(dataclasses.FrozenInstanceError):
        out.verdict = "fail"
    sub = TouchedSubject(path="a.py", symbol="F")
    with pytest.raises(dataclasses.FrozenInstanceError):
        sub.path = "b.py"
    rep = IngestReport(inserted=(), already_recorded=0, matched=0,
                       skipped_lines=0, keyid="")
    with pytest.raises(dataclasses.FrozenInstanceError):
        rep.matched = 9


# ── touched_subjects: joinable subjects only; malformed lines under-claim ─────


def test_touched_subjects_dedups_across_line_classes():
    subs, skipped = touched_subjects([
        _subj_line("existence", "matrix/x.py::LanguageConfig"),
        _subj_line("contract", "matrix/x.py::LanguageConfig"),
        _subj_line("regression", "matrix/y.py::Other"),
    ])
    assert subs == [TouchedSubject(path="matrix/x.py", symbol="LanguageConfig"),
                    TouchedSubject(path="matrix/y.py", symbol="Other")]
    assert skipped == 0


def test_touched_subjects_summary_and_non_join_classes_never_join():
    subs, skipped = touched_subjects([
        _exec_line("typecheck", "passed"),                     # summary subject, no ::
        _exec_line("tests", "passed"),
        _subj_line("blast-radius", "matrix/x.py::F"),          # not a join class
        _subj_line("differential", "matrix/x.py::F"),
    ])
    assert subs == [] and skipped == 0                         # valid lines, just not joinable


def test_touched_subjects_malformed_lines_skipped_and_counted():
    subs, skipped = touched_subjects([
        "not-a-dict",
        {"class": "existence"},                                # subject missing
        {"class": "contract", "subject": 42},                  # subject wrong type
        {"subject": "a.py::F"},                                # class missing
        _subj_line("existence", "matrix/x.py::F"),             # the survivor ingests
    ])
    assert subs == [TouchedSubject(path="matrix/x.py", symbol="F")]
    assert skipped == 4


# ── match_anchors: the §3.4 precision-first join truth-table ──────────────────

SUB = TouchedSubject(path="matrix/x.py", symbol="LanguageConfig")


def test_join_path_plus_symbol_hit_is_symbol_level():
    got = match_anchors([SUB], [(7, "matrix/x.py::LanguageConfig")])
    assert got == {7: (SUB, "symbol")}


def test_join_file_scoped_anchor_is_file_level():
    assert match_anchors([SUB], [(7, "matrix/x.py")]) == {7: (SUB, "file")}


def test_join_same_symbol_in_a_different_file_never_matches():
    assert match_anchors([SUB], [(7, "other/z.py::LanguageConfig")]) == {}


def test_join_symbol_scoped_anchor_naming_a_different_symbol_never_matches():
    assert match_anchors([SUB], [(7, "matrix/x.py::OtherSymbol")]) == {}


def test_join_empty_or_unparseable_anchor_never_matches():
    assert match_anchors([SUB], [(7, ""), (8, "   ::"), (9, "!!!")]) == {}


def test_join_path_needs_a_boundary_not_a_substring():
    # 'amatrix/x.py' contains the path only mid-token — precision-first: no match.
    assert match_anchors([SUB], [(7, "amatrix/x.pyx"), (8, "amatrix/x.py")]) == {}


def test_join_anchor_ending_with_slash_path_hits():
    # the `A ends with /P` arm; the prefix residue makes it symbol-scoped, so the
    # symbol must also appear for a match (precision-first under-claim otherwise).
    assert match_anchors([SUB], [(7, "src/matrix/x.py")]) == {}
    got = match_anchors([SUB], [(7, "LanguageConfig in src/matrix/x.py")])
    assert got == {7: (SUB, "symbol")}


def test_join_symbol_needs_identifier_boundaries():
    # 'XLanguageConfigY' does not name the symbol; 'LanguageConfig2' neither.
    assert match_anchors([SUB], [(7, "matrix/x.py::XLanguageConfigY"),
                                 (8, "matrix/x.py::LanguageConfig2")]) == {}


def test_join_prose_anchor_with_backticks_and_quotes_matches():
    got = match_anchors([SUB], [(7, "the `matrix/x.py` loader: LanguageConfig defaults")])
    assert got == {7: (SUB, "symbol")}


def test_join_records_first_subject_sorted_match_only():
    # one row per episode per receipt: the outcome is per-change, not per-line.
    a = TouchedSubject(path="matrix/a.py", symbol="Alpha")
    z = TouchedSubject(path="matrix/z.py", symbol="Zeta")
    got = match_anchors([z, a], [(7, "matrix/z.py Zeta and matrix/a.py Alpha")])
    assert got == {7: (a, "symbol")}                           # subject-sorted first


def test_join_is_deterministic_and_order_stable():
    subs = [TouchedSubject(path="matrix/x.py", symbol="F")]
    eps = [(3, "matrix/x.py"), (1, "matrix/x.py"), (2, "no match here")]
    got = match_anchors(subs, eps)
    assert list(got.keys()) == [3, 1]                          # episode input order kept


# ── render_payload: THE canonical-JSON single owner (the idempotency key) ─────


def test_render_payload_exact_golden_bytes():
    out = _outcome(hive_census_version="0.1.0")
    payload = render_payload(out, TouchedSubject(path="matrix/x.py", symbol="F"), "symbol")
    assert payload == (
        '{"base_sha":"' + BASE + '","head_sha":"' + HEAD + '",'
        '"hive_census_version":"0.1.0",'
        '"matched":{"level":"symbol","path":"matrix/x.py","symbol":"F"},'
        '"phase":"pre_merge",'
        '"predicate_type":"urn:hive-census:receipt:v0",'
        '"receipt_schema_version":"v0",'
        '"receipt_sha256":"' + "d" * 64 + '",'
        '"schema":"change_outcome/v1",'
        '"signal":"none",'
        '"tag":"machine-checked",'
        '"verdict":"pass"}')


def test_render_payload_is_byte_stable_across_calls():
    out = _outcome()
    sub = TouchedSubject(path="a.py", symbol="F")
    assert render_payload(out, sub, "file") == render_payload(out, sub, "file")


# ── ChangeEvidenceService: parse → derive → join → render → ONE batch ─────────

_ANCHOR = "matrix/x.py::LanguageConfig"
_LINES = [_subj_line("existence", _ANCHOR), _subj_line("contract", _ANCHOR),
          _exec_line("typecheck", "passed"), _exec_line("tests", "passed")]


def test_service_happy_path_appends_one_row_per_matched_episode():
    svc, appender = _service([(7, _ANCHOR), (8, "unrelated/other.py::Thing")])
    report = svc.ingest(_receipt(_LINES))
    assert report.matched == 1 and report.already_recorded == 0
    assert report.skipped_lines == 0 and report.keyid == "key-1"
    assert len(report.inserted) == 1
    assert len(appender.batches) == 1 and len(appender.batches[0]) == 1
    eid, kind, actor, ts, payload = appender.batches[0][0]
    assert (eid, kind, actor, ts) == (7, EK_CHANGE_OUTCOME, "census", 12_345)
    body = json.loads(payload)
    assert body["base_sha"] == BASE and body["head_sha"] == HEAD
    assert body["schema"] == "change_outcome/v1"
    assert body["verdict"] == "pass" and body["tag"] == "machine-checked"
    assert body["matched"] == {"path": "matrix/x.py", "symbol": "LanguageConfig",
                               "level": "symbol"}


def test_service_matched_zero_reports_only_and_never_touches_the_appender():
    svc, appender = _service([(8, "unrelated/other.py::Thing")])
    report = svc.ingest(_receipt(_LINES))
    assert report.matched == 0 and report.inserted == ()
    assert appender.batches == []                              # byte-inert store


def test_service_refused_receipt_means_zero_appender_calls():
    svc, appender = _service([(7, _ANCHOR)])
    with pytest.raises(ReceiptRefused):
        svc.ingest(_receipt(_LINES, digest="0" * 64))
    with pytest.raises(ReceiptRefused):                        # nothing decided
        svc.ingest(_receipt([_subj_line("existence", _ANCHOR)]))
    assert appender.batches == []


def test_service_malformed_line_skipped_while_the_rest_ingest():
    lines = ["garbage", {"class": "existence"}] + list(_LINES)
    svc, appender = _service([(7, _ANCHOR)])
    report = svc.ingest(_receipt(lines))
    assert report.skipped_lines == 2
    assert report.matched == 1 and len(report.inserted) == 1


def test_service_re_ingest_is_idempotent_through_the_appender_contract():
    svc, appender = _service([(7, _ANCHOR)])
    first = svc.ingest(_receipt(_LINES))
    assert len(first.inserted) == 1
    second = svc.ingest(_receipt(_LINES))
    assert second.inserted == () and second.already_recorded == 1
    assert second.matched == 1


def test_service_post_merge_flags_thread_to_verdict_and_tag():
    svc, appender = _service([(7, _ANCHOR)])
    report = svc.ingest(_receipt(_LINES), phase="post_merge", verdict="fail",
                        signal="canary")
    assert report.matched == 1
    body = json.loads(appender.batches[0][0][4])
    assert body["phase"] == "post_merge" and body["verdict"] == "fail"
    assert body["tag"] == "machine-checked" and body["signal"] == "canary"


def test_service_post_merge_without_a_verdict_is_an_internal_error_not_a_refusal():
    svc, _ = _service([(7, _ANCHOR)])
    with pytest.raises(ValueError, match="verdict") as exc:
        svc.ingest(_receipt(_LINES), phase="post_merge")
    assert not isinstance(exc.value, ReceiptRefused)           # caller bug, not receipt fault


def test_service_post_merge_unsignaled_is_unverified_judgment():
    svc, appender = _service([(7, _ANCHOR)])
    svc.ingest(_receipt(_LINES), phase="post_merge", verdict="pass", signal="none")
    assert json.loads(appender.batches[0][0][4])["tag"] == "unverified-judgment"


def test_service_pre_merge_verdict_arg_never_overrides_the_derivation():
    # INV-2 analog: the caller cannot assert a pre-merge verdict — the receipt decides.
    svc, appender = _service([(7, _ANCHOR)])
    failing = [_subj_line("existence", _ANCHOR), _exec_line("tests", "failed")]
    svc.ingest(_receipt(failing), verdict="pass")
    assert json.loads(appender.batches[0][0][4])["verdict"] == "fail"


# ── O7 by construction: the module holds NO trust handle ──────────────────────


def test_module_references_no_trust_mutation_surface():
    src = inspect.getsource(ce)
    for forbidden in ("set_trust", "approve", "supersede", "deprecate"):
        assert forbidden not in src, f"change_evidence must not reference {forbidden!r}"
