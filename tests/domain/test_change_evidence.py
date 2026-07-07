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
    ChangeEvidenceService, ChangeOutcome, IngestReport, ReceiptRefused, SubjectEvidence,
    TouchedSubject, classify_verified, classify_verify, decided_tests_state,
    derive_post_merge_tag, derive_pre_merge, match_anchors, parse_receipt,
    render_payload, subject_evidence, touched_subjects, version_stamp,
)
from hive.domain.evidence_kinds import (
    EK_CHANGE_OUTCOME, EK_OUTCOME_VERIFIED_HELPED, EK_OUTCOME_VERIFIED_HURT,
    EK_VERIFY_CURRENT, EK_VERIFY_STALE,
)

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
    """Honors the evolved AnchoredEpisodeReader contract: (id, anchor, polarity)."""

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
    """reader_rows: (id, anchor) pairs default to neutral polarity; triples pass through."""
    rows = [r if len(r) == 3 else (*r, "neutral") for r in reader_rows]
    reader, appender = FakeReader(rows), FakeAppender()
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


# ═══ Flow A: the verified-outcome + verify extension of the same ingest ═════════

_COMBDRIFT_BLOCK = {"base_sha": BASE, "head_sha": HEAD,
                    "combdrift_version": "0.1.0", "fingerprint_version": "1"}
_MATRIX_BLOCK = {
    "base": {"graph_sha256": "e" * 64, "commit_sha": BASE, "engine_version": "0.1.0"},
    "head": {"graph_sha256": "f" * 64, "commit_sha": HEAD, "engine_version": "0.1.0"},
}
_STAMPED_PROV = {"base_sha": BASE, "head_sha": HEAD, "hive_census_version": "0.1.0",
                 "combdrift": _COMBDRIFT_BLOCK, "matrix": _MATRIX_BLOCK}


def _detail_line(cls, subject, detail, *, tag="machine-checked", reason=""):
    return {"class": cls, "subject": subject, "tag": tag, "detail": detail,
            "reason": reason}


def _verified_lines(*, drift="breaking", exists_after=True,
                    reg_tests=("t_node_1",), tests_state="failed",
                    subject=_ANCHOR):
    """The three-way regression shape: contract drift + regression reach + a decided
    tests run — plus an existence line and a decided typecheck (so pre_merge derives)."""
    return [
        _detail_line("existence", subject, {"existed_before": True,
                                            "exists_after": exists_after}),
        _detail_line("contract", subject, {"drift": drift, "old_fingerprint": "fp"}),
        _detail_line("regression", subject,
                     {"drift": drift, "seed": "n1", "depth": 1,
                      "callers": ["c1"], "dependents": [], "tests": list(reg_tests)}),
        _exec_line("typecheck", "passed"),
        _exec_line("tests", tests_state),
    ]


# ── 1. subject_evidence: defensive per-subject distillation ────────────────────


def test_subject_evidence_distills_lines_defensively():
    lines = [
        "garbage",                                             # non-dict: skipped
        {"class": "contract"},                                 # no subject: skipped
        _detail_line("contract", "a.py::F", {"drift": 42}),    # wrong-typed drift: dropped
        _detail_line("contract", "a.py::F", {"drift": "breaking"}),
        _detail_line("existence", "a.py::F", {"exists_after": "yes"}),  # non-bool: dropped
        _detail_line("existence", "a.py::F", {"exists_after": False}),
        _detail_line("regression", "a.py::F", {"tests": ["t1", 7, "t2"]}),  # non-str filtered
        _detail_line("contract", "b.py::G", {}),               # drift absent ⇒ default ""
        _detail_line("blast-radius", "a.py::F", {"drift": "removed"}),  # not an evidence class
    ]
    ev = subject_evidence(lines)
    assert ev[("a.py", "F")] == SubjectEvidence(
        drift="breaking", exists_after=False, regression_tests=("t1", "t2"),
        has_regression_line=True)
    assert ev[("b.py", "G")] == SubjectEvidence()
    assert set(ev) == {("a.py", "F"), ("b.py", "G")}
    assert subject_evidence("not-a-list") == {}                # total, never raises


# ── decided_tests_state: tests-class execution state, decided only ─────────────


@pytest.mark.parametrize("lines, expected", [
    ([_exec_line("tests", "failed")], "failed"),
    ([_exec_line("tests", "passed")], "passed"),
    ([_exec_line("tests", "not_run")], None),
    ([_exec_line("tests", "errored")], None),                  # tooling broke ≠ decided
    ([_exec_line("typecheck", "failed")], None),               # not the tests class
    ([], None),
    ("not-a-list", None),
    ([_exec_line("tests", "passed"), _exec_line("tests", "failed")], "failed"),
])
def test_decided_tests_state_truth_table(lines, expected):
    assert decided_tests_state(lines) == expected


# ── 2. classify_verified: the full mechanical predicate grid ──────────────────


@pytest.mark.parametrize("polarity, drift, tests_state, reg_tests, expected", [
    # the corroborated prohibition: dont + breaking/removed drift + failing reach
    ("dont", "breaking", "failed", ("t1",), EK_OUTCOME_VERIFIED_HELPED),
    ("dont", "removed", "failed", ("t1",), EK_OUTCOME_VERIFIED_HELPED),
    # the contradicted prescription: do + failing reach (drift-independent)
    ("do", "unchanged", "failed", ("t1",), EK_OUTCOME_VERIFIED_HURT),
    ("do", "", "failed", ("t1",), EK_OUTCOME_VERIFIED_HURT),
    # each single clause removed ⇒ None (fail-closed abstention)
    ("dont", "unchanged", "failed", ("t1",), None),            # no breaking drift
    ("dont", "additive", "failed", ("t1",), None),
    ("dont", "breaking", "passed", ("t1",), None),             # decided-pass
    ("dont", "breaking", None, ("t1",), None),                 # not_run/errored/absent
    ("dont", "breaking", "failed", (), None),                  # no regression reach
    ("do", "breaking", "passed", ("t1",), None),
    ("do", "breaking", None, ("t1",), None),
    ("do", "breaking", "failed", (), None),
    ("neutral", "breaking", "failed", ("t1",), None),          # neutral polarity
])
def test_classify_verified_truth_table(polarity, drift, tests_state, reg_tests, expected):
    ev = SubjectEvidence(drift=drift, regression_tests=tuple(reg_tests),
                         has_regression_line=bool(reg_tests))
    assert classify_verified(polarity, ev, tests_state) == expected


# ── 4. classify_verify: current/stale/None per the drift/existence grid ────────


@pytest.mark.parametrize("exists_after, drift, expected", [
    (True, "unchanged", EK_VERIFY_CURRENT),
    (True, "additive", EK_VERIFY_CURRENT),
    (True, "", EK_VERIFY_CURRENT),
    (False, "", EK_VERIFY_STALE),
    (False, "unchanged", EK_VERIFY_STALE),                     # gone trumps drift
    (True, "removed", EK_VERIFY_STALE),
    (True, "breaking", EK_VERIFY_STALE),
    (None, "breaking", EK_VERIFY_STALE),                       # drift alone proves stale
    (None, "", None),                                          # nothing proven: under-claim
    (None, "unchanged", None),                                 # existence unknown
    (True, "indeterminate", None),                             # unknown drift: under-claim
])
def test_verify_kind_truth_table(exists_after, drift, expected):
    assert classify_verify(SubjectEvidence(drift=drift, exists_after=exists_after)) \
        == expected


# ── version_stamp: L7 — no stamp, no verified/verify row ───────────────────────


def test_version_stamp_extracts_the_full_version_binding():
    stamp = version_stamp(_STAMPED_PROV)
    assert stamp == {
        "base_sha": BASE, "head_sha": HEAD,
        "combdrift": {"base_sha": BASE, "head_sha": HEAD,
                      "combdrift_version": "0.1.0", "fingerprint_version": "1"},
        "matrix_head": {"graph_sha256": "f" * 64, "commit_sha": HEAD,
                        "engine_version": "0.1.0"},
    }


@pytest.mark.parametrize("mutate", [
    lambda p: p.pop("combdrift"),                              # verifier block absent
    lambda p: p.pop("matrix"),                                 # model block absent
    lambda p: p["matrix"].pop("head"),                         # head graph absent
    lambda p: p["matrix"]["head"].pop("graph_sha256"),         # incomplete ModelVersion
    lambda p: p["matrix"]["head"].update(engine_version=""),   # blank version
    lambda p: p.update(combdrift="not-a-dict"),
    lambda p: p.update(base_sha=""),
])
def test_version_stamp_absent_or_partial_blocks_return_none(mutate):
    prov = json.loads(json.dumps(_STAMPED_PROV))               # deep copy per case
    mutate(prov)
    assert version_stamp(prov) is None


def test_version_stamp_keeps_only_flat_string_values_of_the_combdrift_block():
    prov = json.loads(json.dumps(_STAMPED_PROV))
    prov["combdrift"]["nested"] = {"a": 1}                     # non-str: filtered out
    prov["combdrift"]["n"] = 7
    assert version_stamp(prov)["combdrift"] == _COMBDRIFT_BLOCK


# ── 3+5+7. the service extension: one batch, stamp-gated, idempotent, counted ──


def _stamped_receipt(lines):
    return _receipt(lines, provenance=json.loads(json.dumps(_STAMPED_PROV)))


def test_ingest_batch_is_atomic_and_idempotent_across_kinds():
    svc, appender = _service([(7, _ANCHOR, "dont")])
    report = svc.ingest(_stamped_receipt(_verified_lines()))
    # ONE batch carrying change_outcome + verified + verify (never a second tx)
    assert len(appender.batches) == 1
    kinds = [row[1] for row in appender.batches[0]]
    assert kinds == [EK_CHANGE_OUTCOME, EK_OUTCOME_VERIFIED_HELPED, EK_VERIFY_STALE]
    assert len(report.inserted) == 3 and report.already_recorded == 0
    assert (report.verified_helped, report.verified_hurt) == (1, 0)
    assert (report.verify_current, report.verify_stale) == (0, 1)
    # re-ingest: content-keyed idempotency covers ALL kinds
    again = svc.ingest(_stamped_receipt(_verified_lines()))
    assert again.inserted == () and again.already_recorded == 3
    assert (again.verified_helped, again.verify_stale) == (1, 1)   # rendered, not re-inserted


def test_do_polarity_contradiction_writes_verified_hurt():
    svc, appender = _service([(7, _ANCHOR, "do")])
    report = svc.ingest(_stamped_receipt(_verified_lines(drift="unchanged",
                                                         exists_after=True)))
    kinds = [row[1] for row in appender.batches[0]]
    assert kinds == [EK_CHANGE_OUTCOME, EK_OUTCOME_VERIFIED_HURT, EK_VERIFY_CURRENT]
    assert report.verified_hurt == 1 and report.verify_current == 1


def test_verified_rows_require_full_version_stamp():
    # provenance missing matrix/combdrift ⇒ change_outcome row ONLY (L7 fail-closed)
    for drop in ("matrix", "combdrift"):
        prov = json.loads(json.dumps(_STAMPED_PROV))
        prov.pop(drop)
        svc, appender = _service([(7, _ANCHOR, "dont")])
        report = svc.ingest(_receipt(_verified_lines(), provenance=prov))
        assert [row[1] for row in appender.batches[0]] == [EK_CHANGE_OUTCOME]
        assert report.matched == 1 and len(report.inserted) == 1
        assert (report.verified_helped, report.verified_hurt,
                report.verify_current, report.verify_stale) == (0, 0, 0, 0)


def test_neutral_polarity_abstains_from_verified_but_still_verifies():
    svc, appender = _service([(7, _ANCHOR)])                   # neutral polarity
    report = svc.ingest(_stamped_receipt(_verified_lines()))
    kinds = [row[1] for row in appender.batches[0]]
    assert kinds == [EK_CHANGE_OUTCOME, EK_VERIFY_STALE]       # no verified row
    assert report.verified_helped == 0 and report.verify_stale == 1


def test_post_merge_ingest_writes_no_verified_or_verify_rows():
    # no per-subject machine evidence rides a post-merge assertion (v1 ruling)
    svc, appender = _service([(7, _ANCHOR, "dont")])
    report = svc.ingest(_stamped_receipt(_verified_lines()), phase="post_merge",
                        verdict="fail", signal="canary")
    assert [row[1] for row in appender.batches[0]] == [EK_CHANGE_OUTCOME]
    assert (report.verified_helped, report.verified_hurt,
            report.verify_current, report.verify_stale) == (0, 0, 0, 0)


def test_change_outcome_row_shape_is_unchanged_by_the_extension():
    # the S4 golden holds: the change_outcome payload keeps its exact key set (the
    # byte pin lives in test_render_payload_exact_golden_bytes) even when verified/
    # verify rows ride the same batch — the extension leaks nothing into it.
    svc, appender = _service([(7, _ANCHOR, "dont")])
    svc.ingest(_stamped_receipt(_verified_lines()))
    outcome_payload = json.loads(appender.batches[0][0][4])
    assert set(outcome_payload) == {
        "schema", "base_sha", "head_sha", "receipt_sha256", "receipt_schema_version",
        "predicate_type", "phase", "verdict", "tag", "signal", "matched",
        "hive_census_version"}
    assert outcome_payload["schema"] == "change_outcome/v1"


def test_payloads_are_ids_enums_and_stamps_only():
    prose = "REASONPROSE the loader crashed because the flange rotated"
    lines = [
        _detail_line("existence", _ANCHOR,
                     {"existed_before": True, "exists_after": False}, reason=prose),
        _detail_line("contract", _ANCHOR, {"drift": "removed"}, reason=prose),
        _detail_line("regression", _ANCHOR,
                     {"drift": "removed", "tests": ["t_reach"]}, reason=prose),
        _exec_line("typecheck", "passed"), _exec_line("tests", "failed"),
    ]
    svc, appender = _service([(7, _ANCHOR, "dont")])
    svc.ingest(_stamped_receipt(lines))
    by_kind = {row[1]: row[4] for row in appender.batches[0]}
    verified = json.loads(by_kind[EK_OUTCOME_VERIFIED_HELPED])
    assert set(verified) == {"schema", "receipt_sha256", "phase", "verdict", "tag",
                             "matched", "reason", "stamp"}
    assert verified["schema"] == "outcome_verified/v1"
    assert verified["reason"] == "corroborated"                # a machine enum, never prose
    assert verified["matched"] == {"path": "matrix/x.py", "symbol": "LanguageConfig",
                                   "level": "symbol"}
    assert verified["stamp"]["matrix_head"]["graph_sha256"] == "f" * 64
    verify = json.loads(by_kind[EK_VERIFY_STALE])
    assert set(verify) == {"schema", "matched", "exists_after", "drift", "stamp"}
    assert verify["schema"] == "verify/v1"
    assert (verify["exists_after"], verify["drift"]) == (False, "removed")
    assert verify["stamp"]["head_sha"] == HEAD
    for payload in by_kind.values():                           # Law 4: no receipt prose
        assert "REASONPROSE" not in payload
    # byte-stable across calls (the idempotency key across all three kinds)
    svc2, appender2 = _service([(7, _ANCHOR, "dont")])
    svc2.ingest(_stamped_receipt(lines))
    assert appender2.batches[0] == appender.batches[0]


def test_hurt_reason_is_contradicted():
    svc, appender = _service([(7, _ANCHOR, "do")])
    svc.ingest(_stamped_receipt(_verified_lines(drift="unchanged")))
    by_kind = {row[1]: row[4] for row in appender.batches[0]}
    assert json.loads(by_kind[EK_OUTCOME_VERIFIED_HURT])["reason"] == "contradicted"


def test_report_counters_default_zero_and_are_additive():
    rep = IngestReport(inserted=(), already_recorded=0, matched=0,
                       skipped_lines=0, keyid="")
    assert (rep.verified_helped, rep.verified_hurt,
            rep.verify_current, rep.verify_stale) == (0, 0, 0, 0)
