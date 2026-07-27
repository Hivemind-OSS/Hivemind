"""hive.domain.change_evidence — the pure change-outcome feed: DSSE receipt parsing (D8:
refuse the receipt, under-claim the line), verdict/tag derivation (server-derived, never
caller-asserted — the INV-2 analog; the §6.2.5 canary rule is unconstructable to violate;
v3: ``derive_post_merge`` derives from decided execution lines, capped at bounded-estimate),
the v3 STRUCTURED repo-scoped change→episode join (exact-path + symbol tier, repo-filtered),
the canonical payload renderer (the idempotency key), and the ports-driven
ChangeEvidenceService (fake-port, ms-fast). The verified/verify/suspect riders are gated
ONLY on the full version stamp — ANY phase; the canonical post_merge ingest is their
normal carrier.
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
    ChangeEvidenceService,
    ChangeOutcome,
    IngestReport,
    ReceiptRefused,
    SubjectEvidence,
    TouchedSubject,
    classify_verified,
    decided_tests_state,
    derive_post_merge,
    derive_post_merge_tag,
    derive_pre_merge,
    match_anchors,
    parse_receipt,
    propagation_subjects,
    render_payload,
    subject_evidence,
    touched_subjects,
    version_stamp,
)
from hive.domain.evidence_kinds import (
    EK_CHANGE_OUTCOME,
    EK_OUTCOME_VERIFIED_HELPED,
    EK_OUTCOME_VERIFIED_HURT,
    EK_STALE_SUSPECT,
)

BASE = "a" * 40
HEAD = "b" * 40


def _canon(doc) -> bytes:
    return json.dumps(
        doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _receipt(
    lines=None,
    *,
    base_sha=BASE,
    head_sha=HEAD,
    schema_version="v0",
    census_version="0.1.0",
    predicate_type="urn:hive-census:receipt:v0",
    statement_type="https://in-toto.io/Statement/v1",
    payload_type="application/vnd.in-toto+json",
    digest=None,
    keyid="key-1",
    provenance=...,
):
    """A well-formed DSSE-shaped envelope around an in-toto receipt statement; every
    knob exists so one test can break one thing. The signatures block is IGNORED by
    parse_receipt (the receipt is unsigned by policy); the ``keyid`` knob exists only
    to prove the kernel never reads it."""
    if provenance is ...:
        provenance = {
            "base_sha": base_sha,
            "head_sha": head_sha,
            "hive_census_version": census_version,
        }
    predicate = {
        "schema_version": schema_version,
        "lines": lines or [],
        "provenance": provenance,
    }
    d = digest if digest is not None else hashlib.sha256(_canon(predicate)).hexdigest()
    statement = {
        "_type": statement_type,
        "subject": [{"name": "hive-census-receipt", "digest": {"sha256": d}}],
        "predicateType": predicate_type,
        "predicate": predicate,
    }
    return {
        "payload": base64.b64encode(_canon(statement)).decode("ascii"),
        "payloadType": payload_type,
        "signatures": [{"keyid": keyid, "sig": "c2ln"}],
    }


def _exec_line(cls, state, tag="machine-checked"):
    return {
        "class": cls,
        "subject": cls,
        "tag": tag,
        "detail": {"state": state},
        "reason": "",
    }


def _subj_line(cls, subject, tag="machine-checked"):
    return {"class": cls, "subject": subject, "tag": tag, "detail": {}, "reason": ""}


class FakeReader:
    """Honors the v3 AnchoredEpisodeReader contract: (id, repo, anchor, polarity)."""

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


def _reader_rows(reader_rows):
    """(id, anchor) / (id, anchor, polarity) rows default to the legacy ``""`` repo
    identity (joining legacy repo-less receipts, whose ``outcome.repo`` is ``""`` —
    the exact-match rule holds on the empty identity too); 4-tuples pass through."""
    rows = []
    for r in reader_rows:
        if len(r) == 2:
            rows.append((r[0], "", r[1], "neutral"))
        elif len(r) == 3:
            rows.append((r[0], "", r[1], r[2]))
        else:
            rows.append(tuple(r))
    return rows


def _service(reader_rows=(), *, now=lambda: 12_345):
    reader, appender = FakeReader(_reader_rows(reader_rows)), FakeAppender()
    return ChangeEvidenceService(reader=reader, appender=appender, now=now), appender


# ── parse_receipt: receipt-global malformation REFUSES (D8, zero rows) ─────────


@pytest.mark.parametrize(
    "envelope, reason_fragment",
    [
        (None, "not a JSON object"),
        ([1, 2], "not a JSON object"),
        ({}, "payloadType"),  # dict with NO payloadType key
        ({"payloadType": "application/vnd.in-toto+json"}, "payload"),
        (
            {
                "payload": "!!!not-base64!!!",
                "payloadType": "application/vnd.in-toto+json",
            },
            "base64",
        ),
        (
            {
                "payload": base64.b64encode(b"{not json").decode(),
                "payloadType": "application/vnd.in-toto+json",
            },
            "JSON",
        ),
        (
            {
                "payload": base64.b64encode(b"[1,2]").decode(),
                "payloadType": "application/vnd.in-toto+json",
            },
            "object",
        ),
    ],
)
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
    statement["predicate"]["provenance"]["head_sha"] = "c" * 40  # the tamper
    env["payload"] = base64.b64encode(_canon(statement)).decode("ascii")
    with pytest.raises(ReceiptRefused, match="digest"):
        parse_receipt(env)


@pytest.mark.parametrize(
    "prov",
    [
        {},  # both absent
        {"base_sha": BASE},  # head absent
        {"base_sha": "", "head_sha": HEAD},  # blank base
        {"base_sha": BASE, "head_sha": "   "},  # whitespace head
        "not-a-dict",  # wrong type
    ],
)
def test_missing_provenance_shas_refuse(prov):
    with pytest.raises(ReceiptRefused, match="sha"):
        parse_receipt(_receipt(provenance=prov))


def test_valid_receipt_parses_to_statement():
    statement = parse_receipt(_receipt([_exec_line("tests", "passed")]))
    assert statement["predicateType"] == "urn:hive-census:receipt:v0"
    assert statement["predicate"]["lines"][0]["class"] == "tests"


def test_signatures_are_ignored_never_a_refusal():
    # The kernel never verifies signatures — authenticity rides transport/auth, not the
    # receipt (unsigned by policy). An envelope with NO signatures block, the exact
    # census unsigned shape (signatures: [] + unsigned: true), and one bearing a
    # FABRICATED keyid all parse identically; none is a refusal, and no keyid is read.
    base = _receipt([_exec_line("tests", "passed")])
    del base["signatures"]
    assert parse_receipt(base)["predicateType"] == "urn:hive-census:receipt:v0"

    census_shape = _receipt([_exec_line("tests", "passed")])
    census_shape["signatures"] = []
    census_shape["unsigned"] = True
    assert parse_receipt(census_shape)["predicateType"] == "urn:hive-census:receipt:v0"

    forged = _receipt([_exec_line("tests", "passed")], keyid="attacker-forged-DEADBEEF")
    assert parse_receipt(forged)["predicateType"] == "urn:hive-census:receipt:v0"


# ── verdict + tag derivation (server-derived; §3.3) ────────────────────────────


def test_pre_merge_any_decided_fail_is_fail():
    verdict, _ = derive_pre_merge(
        [_exec_line("typecheck", "failed"), _exec_line("tests", "passed")]
    )
    assert verdict == "fail"


def test_pre_merge_all_decided_pass_machine_checked():
    assert derive_pre_merge(
        [_exec_line("typecheck", "passed"), _exec_line("tests", "passed")]
    ) == ("pass", "machine-checked")


def test_pre_merge_scoped_pyright_downgrades_to_bounded_estimate():
    # receipt tags are reused verbatim (PCE D3): one decided line at bounded-estimate
    # caps the outcome tag, even when the other decided line is machine-checked.
    verdict, tag = derive_pre_merge(
        [
            _exec_line("typecheck", "passed", tag="bounded-estimate"),
            _exec_line("tests", "passed"),
        ]
    )
    assert (verdict, tag) == ("pass", "bounded-estimate")


@pytest.mark.parametrize(
    "lines",
    [
        [],
        [_exec_line("tests", "not_run")],
        [_exec_line("typecheck", "errored")],  # errored is NOT decided
        [_subj_line("existence", "a.py::F"), _subj_line("regression", "b.py::G")],
    ],
)
def test_pre_merge_nothing_decided_refuses(lines):
    with pytest.raises(ReceiptRefused, match="decided"):
        derive_pre_merge(lines)


def test_pre_merge_errored_never_enters_the_verdict():
    # "tooling broke" is not "change failed": an errored typecheck beside a passing
    # test run stays a pass.
    assert (
        derive_pre_merge(
            [_exec_line("typecheck", "errored"), _exec_line("tests", "passed")]
        )[0]
        == "pass"
    )


def test_post_merge_tag_follows_the_canary_rule():
    assert derive_post_merge_tag("randomized") == "machine-checked"
    assert derive_post_merge_tag("canary") == "machine-checked"
    assert derive_post_merge_tag("none") == "unverified-judgment"
    assert derive_post_merge_tag("gut-feel") == "unverified-judgment"


# ── derive_post_merge (v3): decided lines derive; else landed-line parity ──────


def test_derive_post_merge_decided_fail_wins():
    # a decided failing execution line derives the verdict — the caller's "pass"
    # never overrides machine evidence.
    assert derive_post_merge(
        [_exec_line("typecheck", "passed"), _exec_line("tests", "failed")],
        verdict="pass",
        signal="canary",
    ) == ("fail", "bounded-estimate")


def test_derive_post_merge_decided_pass_caps_at_bounded_estimate():
    # post-merge execution lines never self-certify machine-checked — that claim
    # still needs a randomized/canary SIGNAL (the §6.2.5 canary rule the carrier
    # enforces structurally).
    assert derive_post_merge(
        [_exec_line("tests", "passed")], verdict="fail", signal="none"
    ) == ("pass", "bounded-estimate")
    assert derive_post_merge(
        [_exec_line("tests", "passed", tag="machine-checked")],
        verdict="pass",
        signal="canary",
    ) == ("pass", "bounded-estimate")


def test_derive_post_merge_nothing_decided_falls_back_to_caller_and_signal():
    # landed-line parity: not_run/errored is "tooling broke", never decided — the
    # caller's verdict lands with the signal-derived tag.
    lines = [_exec_line("tests", "not_run"), _exec_line("typecheck", "errored")]
    assert derive_post_merge(lines, verdict="pass", signal="canary") == (
        "pass",
        "machine-checked",
    )
    assert derive_post_merge(lines, verdict="fail", signal="none") == (
        "fail",
        "unverified-judgment",
    )
    assert derive_post_merge("not-a-list", verdict="pass", signal="none") == (
        "pass",
        "unverified-judgment",
    )


# ── ChangeOutcome carrier: illegal states unconstructable (Law 2) ─────────────


def _outcome(**kw):
    base = dict(
        base_sha=BASE,
        head_sha=HEAD,
        receipt_sha256="d" * 64,
        receipt_schema_version="v0",
        predicate_type="urn:hive-census:receipt:v0",
        phase="pre_merge",
        verdict="pass",
        tag="machine-checked",
    )
    base.update(kw)
    return ChangeOutcome(**base)


def test_forged_post_merge_machine_checked_without_signal_is_unconstructable():
    with pytest.raises(ValueError, match="signal"):
        _outcome(phase="post_merge", tag="machine-checked", signal="none")


def test_post_merge_machine_checked_with_canary_constructs():
    out = _outcome(phase="post_merge", tag="machine-checked", signal="canary")
    assert out.tag == "machine-checked"


@pytest.mark.parametrize(
    "kw",
    [
        {"phase": "mid_merge"},
        {"verdict": "maybe"},
        {"tag": "vibes"},
        {"signal": "hunch"},
        {"base_sha": ""},
        {"head_sha": "  "},
        {"receipt_sha256": ""},
    ],
)
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
    rep = IngestReport(inserted=(), already_recorded=0, matched=0, skipped_lines=0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        rep.matched = 9


# ── touched_subjects: joinable subjects only; malformed lines under-claim ─────


def test_touched_subjects_dedups_across_line_classes():
    subs, skipped = touched_subjects(
        [
            _subj_line("existence", "matrix/x.py::LanguageConfig"),
            _subj_line("contract", "matrix/x.py::LanguageConfig"),
            _subj_line("regression", "matrix/y.py::Other"),
        ]
    )
    assert subs == [
        TouchedSubject(path="matrix/x.py", symbol="LanguageConfig"),
        TouchedSubject(path="matrix/y.py", symbol="Other"),
    ]
    assert skipped == 0


def test_touched_subjects_summary_and_non_join_classes_never_join():
    subs, skipped = touched_subjects(
        [
            _exec_line("typecheck", "passed"),  # summary subject, no ::
            _exec_line("tests", "passed"),
            _subj_line("blast-radius", "matrix/x.py::F"),  # not a join class
            _subj_line("differential", "matrix/x.py::F"),
        ]
    )
    assert subs == [] and skipped == 0  # valid lines, just not joinable


def test_touched_subjects_malformed_lines_skipped_and_counted():
    subs, skipped = touched_subjects(
        [
            "not-a-dict",
            {"class": "existence"},  # subject missing
            {"class": "contract", "subject": 42},  # subject wrong type
            {"subject": "a.py::F"},  # class missing
            _subj_line("existence", "matrix/x.py::F"),  # the survivor ingests
        ]
    )
    assert subs == [TouchedSubject(path="matrix/x.py", symbol="F")]
    assert skipped == 4


# ── match_anchors: the v3 structured, repo-scoped join truth-table ─────────────

SUB = TouchedSubject(path="matrix/x.py", symbol="LanguageConfig")
JREPO = "widgets"


def _rows(*pairs, repo=JREPO, polarity="neutral"):
    """(id, anchor) pairs → v3 (id, repo, anchor, polarity) reader rows."""
    return [(eid, repo, anchor, polarity) for eid, anchor in pairs]


def test_join_path_plus_symbol_hit_is_symbol_level():
    got = match_anchors([SUB], _rows((7, "matrix/x.py::LanguageConfig")), repo=JREPO)
    assert got == {7: (SUB, "symbol")}


def test_join_file_scoped_anchor_is_file_level():
    got = match_anchors([SUB], _rows((7, "matrix/x.py")), repo=JREPO)
    assert got == {7: (SUB, "file")}


def test_join_filters_rows_to_the_receipt_repo():
    # THE named-mutation twin (CT-9 cross-repo join): an IDENTICAL anchor row
    # recorded under ANOTHER repo never joins — one repo's change can never write
    # evidence onto another repo's memory. Dropping the repo filter reds here.
    foreign = _rows((7, "matrix/x.py::LanguageConfig"), repo="other-repo")
    assert match_anchors([SUB], foreign, repo=JREPO) == {}
    both = foreign + _rows((9, "matrix/x.py::LanguageConfig"))
    assert match_anchors([SUB], both, repo=JREPO) == {9: (SUB, "symbol")}


def test_join_same_symbol_in_a_different_file_never_matches():
    got = match_anchors([SUB], _rows((7, "other/z.py::LanguageConfig")), repo=JREPO)
    assert got == {}


def test_join_symbol_scoped_anchor_naming_a_different_symbol_never_matches():
    # the symbol tier is EXACT comparison — near-miss identifiers never match
    # (precision-first under-claim, stricter than the free-text era).
    got = match_anchors(
        [SUB],
        _rows(
            (7, "matrix/x.py::OtherSymbol"),
            (8, "matrix/x.py::XLanguageConfigY"),
            (9, "matrix/x.py::LanguageConfig2"),
        ),
        repo=JREPO,
    )
    assert got == {}


def test_join_empty_or_degenerate_anchor_never_matches():
    got = match_anchors([SUB], _rows((7, ""), (8, "   ::"), (9, "!!!")), repo=JREPO)
    assert got == {}


def test_join_path_is_exact_no_substring_or_prose_arms():
    # structured anchors collapse the old free-text boundary-scan heuristics to
    # exact comparison: substrings, suffix paths, and prose anchors all read as
    # DIFFERENT paths — no match.
    rows = _rows(
        (7, "amatrix/x.pyx"),
        (8, "amatrix/x.py"),
        (9, "src/matrix/x.py"),
        (10, "the `matrix/x.py` loader: LanguageConfig defaults"),
    )
    assert match_anchors([SUB], rows, repo=JREPO) == {}


def test_join_symbol_tier_wins_over_file_tier_per_episode():
    rows = _rows((7, "matrix/x.py"), (7, "matrix/x.py::LanguageConfig"))
    assert match_anchors([SUB], rows, repo=JREPO) == {7: (SUB, "symbol")}


def test_join_records_first_subject_sorted_match_only():
    # one row per episode per receipt: the outcome is per-change, not per-line.
    a = TouchedSubject(path="matrix/a.py", symbol="Alpha")
    z = TouchedSubject(path="matrix/z.py", symbol="Zeta")
    rows = _rows((7, "matrix/z.py::Zeta"), (7, "matrix/a.py::Alpha"))
    got = match_anchors([z, a], rows, repo=JREPO)
    assert got == {7: (a, "symbol")}  # subject-sorted first


def test_join_is_deterministic_and_order_stable():
    subs = [TouchedSubject(path="matrix/x.py", symbol="F")]
    rows = _rows((3, "matrix/x.py"), (1, "matrix/x.py"), (2, "no/match.py"))
    got = match_anchors(subs, rows, repo=JREPO)
    assert list(got.keys()) == [3, 1]  # episode input order kept


def test_join_malformed_rows_under_claim_never_crash():
    rows = [
        ("seven", JREPO, "matrix/x.py", "neutral"),  # non-int id
        (7, JREPO),  # short row
        None,  # not a row at all
        (8, JREPO, 42, "neutral"),  # non-str anchor
        (9, JREPO, "matrix/x.py", "neutral"),
    ]  # the survivor
    assert match_anchors([SUB], rows, repo=JREPO) == {9: (SUB, "file")}


# ── _anchor_match_level: one shared tokenizer, the same tiers ─────────────────


def test_anchor_match_level_tiers_are_unchanged_by_the_shared_tokenizer():
    # the join no longer spells the (path, symbol) split itself — it reads the same
    # owner the write gate validates against, so acceptance and matching cannot
    # fork. Every tier the inline partition produced must still be produced.
    assert ce._anchor_match_level(["matrix/x.py::LanguageConfig"], SUB) == "symbol"
    assert ce._anchor_match_level(["matrix/x.py"], SUB) == "file"
    # a symbol-scoped anchor naming a DIFFERENT symbol keeps under-claiming
    assert ce._anchor_match_level(["matrix/x.py::OtherSymbol"], SUB) is None
    assert ce._anchor_match_level(["other/z.py::LanguageConfig"], SUB) is None


def test_anchor_match_level_trailing_separator_still_falls_to_the_file_tier():
    # an empty symbol reads file-scoped: "matrix/x.py::" names no symbol, so it
    # binds the file exactly as it did under the inline partition. The write gate
    # refuses that spelling for NEW anchors; rows already stored in it keep joining.
    assert ce._anchor_match_level(["matrix/x.py::"], SUB) == "file"


def test_anchor_match_level_nested_separator_belongs_to_the_symbol():
    # the FIRST "::" is the split point, so a namespaced symbol — what C++/Rust
    # receipt subjects spell — joins at the precise symbol tier instead of
    # degrading to the file tier or missing outright.
    nested = TouchedSubject(path="a.py", symbol="Ns::C.m")
    assert ce._anchor_match_level(["a.py::Ns::C.m"], nested) == "symbol"
    assert ce._anchor_match_level(["a.py::Ns"], nested) is None  # a different symbol
    assert ce._anchor_match_level(["a.py"], nested) == "file"


# ── render_payload: THE canonical-JSON single owner (the idempotency key) ─────


def test_render_payload_exact_golden_bytes():
    out = _outcome(hive_census_version="0.1.0")
    payload = render_payload(
        out, TouchedSubject(path="matrix/x.py", symbol="F"), "symbol"
    )
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
        '"verdict":"pass"}'
    )


def test_render_payload_is_byte_stable_across_calls():
    out = _outcome()
    sub = TouchedSubject(path="a.py", symbol="F")
    assert render_payload(out, sub, "file") == render_payload(out, sub, "file")


# ── ChangeEvidenceService: parse → derive → join → render → ONE batch ─────────

_ANCHOR = "matrix/x.py::LanguageConfig"
_LINES = [
    _subj_line("existence", _ANCHOR),
    _subj_line("contract", _ANCHOR),
    _exec_line("typecheck", "passed"),
    _exec_line("tests", "passed"),
]


def test_service_happy_path_appends_one_row_per_matched_episode():
    svc, appender = _service([(7, _ANCHOR), (8, "unrelated/other.py::Thing")])
    report = svc.ingest(_receipt(_LINES))
    assert report.matched == 1 and report.already_recorded == 0
    assert report.skipped_lines == 0
    assert len(report.inserted) == 1
    assert len(appender.batches) == 1 and len(appender.batches[0]) == 1
    eid, kind, actor, ts, payload = appender.batches[0][0]
    assert (eid, kind, actor, ts) == (7, EK_CHANGE_OUTCOME, "census", 12_345)
    body = json.loads(payload)
    assert body["base_sha"] == BASE and body["head_sha"] == HEAD
    assert body["schema"] == "change_outcome/v1"
    assert body["verdict"] == "pass" and body["tag"] == "machine-checked"
    assert body["matched"] == {
        "path": "matrix/x.py",
        "symbol": "LanguageConfig",
        "level": "symbol",
    }


def test_service_matched_zero_reports_only_and_never_touches_the_appender():
    svc, appender = _service([(8, "unrelated/other.py::Thing")])
    report = svc.ingest(_receipt(_LINES))
    assert report.matched == 0 and report.inserted == ()
    assert appender.batches == []  # byte-inert store


def test_service_refused_receipt_means_zero_appender_calls():
    svc, appender = _service([(7, _ANCHOR)])
    with pytest.raises(ReceiptRefused):
        svc.ingest(_receipt(_LINES, digest="0" * 64))
    with pytest.raises(ReceiptRefused):  # nothing decided
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


def test_service_post_merge_decided_lines_derive_the_verdict():
    # v3 §3.6: the canonical post_merge ingest derives verdict/tag from the
    # receipt's decided execution lines — the caller's verdict never overrides.
    svc, appender = _service([(7, _ANCHOR)])
    lines = [
        _subj_line("existence", _ANCHOR),
        _subj_line("contract", _ANCHOR),
        _exec_line("typecheck", "passed"),
        _exec_line("tests", "failed"),
    ]
    report = svc.ingest(
        _receipt(lines), phase="post_merge", verdict="pass", signal="canary"
    )
    assert report.matched == 1
    body = json.loads(appender.batches[0][0][4])
    assert body["phase"] == "post_merge" and body["verdict"] == "fail"
    assert body["tag"] == "bounded-estimate" and body["signal"] == "canary"


def test_service_post_merge_nothing_decided_lands_caller_verdict():
    # landed-line parity: no decided execution lines ⇒ the caller's verdict with
    # the signal-derived tag.
    subject_only = [_subj_line("existence", _ANCHOR), _subj_line("contract", _ANCHOR)]
    svc, appender = _service([(7, _ANCHOR)])
    svc.ingest(
        _receipt(subject_only), phase="post_merge", verdict="pass", signal="canary"
    )
    body = json.loads(appender.batches[0][0][4])
    assert body["verdict"] == "pass" and body["tag"] == "machine-checked"


def test_service_post_merge_unsignaled_fallback_is_unverified_judgment():
    subject_only = [_subj_line("existence", _ANCHOR)]
    svc, appender = _service([(7, _ANCHOR)])
    svc.ingest(
        _receipt(subject_only), phase="post_merge", verdict="pass", signal="none"
    )
    assert json.loads(appender.batches[0][0][4])["tag"] == "unverified-judgment"


def test_service_post_merge_without_a_verdict_is_an_internal_error_not_a_refusal():
    svc, _ = _service([(7, _ANCHOR)])
    with pytest.raises(ValueError, match="verdict") as exc:
        svc.ingest(_receipt(_LINES), phase="post_merge")
    assert not isinstance(exc.value, ReceiptRefused)  # caller bug, not receipt fault


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

_COMBDRIFT_BLOCK = {
    "base_sha": BASE,
    "head_sha": HEAD,
    "combdrift_version": "0.1.0",
    "fingerprint_version": "1",
}
_MATRIX_BLOCK = {
    "base": {"graph_sha256": "e" * 64, "commit_sha": BASE, "engine_version": "0.1.0"},
    "head": {"graph_sha256": "f" * 64, "commit_sha": HEAD, "engine_version": "0.1.0"},
}
_STAMPED_PROV = {
    "base_sha": BASE,
    "head_sha": HEAD,
    "hive_census_version": "0.1.0",
    "combdrift": _COMBDRIFT_BLOCK,
    "matrix": _MATRIX_BLOCK,
}


def _detail_line(cls, subject, detail, *, tag="machine-checked", reason=""):
    return {
        "class": cls,
        "subject": subject,
        "tag": tag,
        "detail": detail,
        "reason": reason,
    }


def _verified_lines(
    *,
    drift="breaking",
    exists_after=True,
    reg_tests=("t_node_1",),
    tests_state="failed",
    subject=_ANCHOR,
):
    """The three-way regression shape: contract drift + regression reach + a decided
    tests run — plus an existence line and a decided typecheck (so pre_merge derives)."""
    return [
        _detail_line(
            "existence", subject, {"existed_before": True, "exists_after": exists_after}
        ),
        _detail_line("contract", subject, {"drift": drift, "old_fingerprint": "fp"}),
        _detail_line(
            "regression",
            subject,
            {
                "drift": drift,
                "seed": "n1",
                "depth": 1,
                "callers": ["c1"],
                "dependents": [],
                "tests": list(reg_tests),
            },
        ),
        _exec_line("typecheck", "passed"),
        _exec_line("tests", tests_state),
    ]


# ── 1. subject_evidence: defensive per-subject distillation ────────────────────


def test_subject_evidence_distills_lines_defensively():
    lines = [
        "garbage",  # non-dict: skipped
        {"class": "contract"},  # no subject: skipped
        _detail_line(
            "contract", "a.py::F", {"drift": 42}
        ),  # wrong-typed drift: dropped
        _detail_line("contract", "a.py::F", {"drift": "breaking"}),
        _detail_line(
            "existence", "a.py::F", {"exists_after": "yes"}
        ),  # non-bool: dropped
        _detail_line("existence", "a.py::F", {"exists_after": False}),
        _detail_line(
            "regression", "a.py::F", {"tests": ["t1", 7, "t2"]}
        ),  # non-str filtered
        _detail_line("contract", "b.py::G", {}),  # drift absent ⇒ default ""
        _detail_line(
            "blast-radius", "a.py::F", {"drift": "removed"}
        ),  # not an evidence class
    ]
    ev = subject_evidence(lines)
    assert ev[("a.py", "F")] == SubjectEvidence(
        drift="breaking",
        exists_after=False,
        regression_tests=("t1", "t2"),
        has_regression_line=True,
    )
    assert ev[("b.py", "G")] == SubjectEvidence()
    assert set(ev) == {("a.py", "F"), ("b.py", "G")}
    assert subject_evidence("not-a-list") == {}  # total, never raises


# ── decided_tests_state: tests-class execution state, decided only ─────────────


@pytest.mark.parametrize(
    "lines, expected",
    [
        ([_exec_line("tests", "failed")], "failed"),
        ([_exec_line("tests", "passed")], "passed"),
        ([_exec_line("tests", "not_run")], None),
        ([_exec_line("tests", "errored")], None),  # tooling broke ≠ decided
        ([_exec_line("typecheck", "failed")], None),  # not the tests class
        ([], None),
        ("not-a-list", None),
        ([_exec_line("tests", "passed"), _exec_line("tests", "failed")], "failed"),
    ],
)
def test_decided_tests_state_truth_table(lines, expected):
    assert decided_tests_state(lines) == expected


# ── 2. classify_verified: the full mechanical predicate grid ──────────────────


@pytest.mark.parametrize(
    "polarity, drift, tests_state, reg_tests, expected",
    [
        # the corroborated prohibition: dont + breaking/removed drift + failing reach
        ("dont", "breaking", "failed", ("t1",), EK_OUTCOME_VERIFIED_HELPED),
        ("dont", "removed", "failed", ("t1",), EK_OUTCOME_VERIFIED_HELPED),
        # the contradicted prescription: do + failing reach (drift-independent)
        ("do", "unchanged", "failed", ("t1",), EK_OUTCOME_VERIFIED_HURT),
        ("do", "", "failed", ("t1",), EK_OUTCOME_VERIFIED_HURT),
        # each single clause removed ⇒ None (fail-closed abstention)
        ("dont", "unchanged", "failed", ("t1",), None),  # no breaking drift
        ("dont", "additive", "failed", ("t1",), None),
        ("dont", "breaking", "passed", ("t1",), None),  # decided-pass
        ("dont", "breaking", None, ("t1",), None),  # not_run/errored/absent
        ("dont", "breaking", "failed", (), None),  # no regression reach
        ("do", "breaking", "passed", ("t1",), None),
        ("do", "breaking", None, ("t1",), None),
        ("do", "breaking", "failed", (), None),
        ("neutral", "breaking", "failed", ("t1",), None),  # neutral polarity
    ],
)
def test_classify_verified_truth_table(
    polarity, drift, tests_state, reg_tests, expected
):
    ev = SubjectEvidence(
        drift=drift,
        regression_tests=tuple(reg_tests),
        has_regression_line=bool(reg_tests),
    )
    assert classify_verified(polarity, ev, tests_state) == expected


# ── version_stamp: L7 — no stamp, no verified row ─────────────────────────────


def test_version_stamp_extracts_the_full_version_binding():
    stamp = version_stamp(_STAMPED_PROV)
    assert stamp == {
        "base_sha": BASE,
        "head_sha": HEAD,
        "combdrift": {
            "base_sha": BASE,
            "head_sha": HEAD,
            "combdrift_version": "0.1.0",
            "fingerprint_version": "1",
        },
        "matrix_head": {
            "graph_sha256": "f" * 64,
            "commit_sha": HEAD,
            "engine_version": "0.1.0",
        },
    }


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.pop("combdrift"),  # verifier block absent
        lambda p: p.pop("matrix"),  # model block absent
        lambda p: p["matrix"].pop("head"),  # head graph absent
        lambda p: p["matrix"]["head"].pop("graph_sha256"),  # incomplete ModelVersion
        lambda p: p["matrix"]["head"].update(engine_version=""),  # blank version
        lambda p: p.update(combdrift="not-a-dict"),
        lambda p: p.update(base_sha=""),
    ],
)
def test_version_stamp_absent_or_partial_blocks_return_none(mutate):
    prov = json.loads(json.dumps(_STAMPED_PROV))  # deep copy per case
    mutate(prov)
    assert version_stamp(prov) is None


def test_version_stamp_keeps_only_flat_string_values_of_the_combdrift_block():
    prov = json.loads(json.dumps(_STAMPED_PROV))
    prov["combdrift"]["nested"] = {"a": 1}  # non-str: filtered out
    prov["combdrift"]["n"] = 7
    assert version_stamp(prov)["combdrift"] == _COMBDRIFT_BLOCK


# ── 3+5+7. the service extension: one batch, stamp-gated, idempotent, counted ──


def _stamped_receipt(lines):
    return _receipt(lines, provenance=json.loads(json.dumps(_STAMPED_PROV)))


def test_ingest_batch_is_atomic_and_idempotent_across_kinds():
    svc, appender = _service([(7, _ANCHOR, "dont")])
    report = svc.ingest(_stamped_receipt(_verified_lines()))
    # ONE batch carrying change_outcome + verified (never a second tx)
    assert len(appender.batches) == 1
    kinds = [row[1] for row in appender.batches[0]]
    assert kinds == [EK_CHANGE_OUTCOME, EK_OUTCOME_VERIFIED_HELPED]
    assert len(report.inserted) == 2 and report.already_recorded == 0
    assert (report.verified_helped, report.verified_hurt) == (1, 0)
    # re-ingest: content-keyed idempotency covers ALL kinds
    again = svc.ingest(_stamped_receipt(_verified_lines()))
    assert again.inserted == () and again.already_recorded == 2
    assert again.verified_helped == 1  # rendered, not re-inserted


def test_do_polarity_contradiction_writes_verified_hurt():
    svc, appender = _service([(7, _ANCHOR, "do")])
    report = svc.ingest(
        _stamped_receipt(_verified_lines(drift="unchanged", exists_after=True))
    )
    kinds = [row[1] for row in appender.batches[0]]
    assert kinds == [EK_CHANGE_OUTCOME, EK_OUTCOME_VERIFIED_HURT]
    assert report.verified_hurt == 1


def test_verified_rows_require_full_version_stamp():
    # provenance missing matrix/combdrift ⇒ change_outcome row ONLY (L7 fail-closed)
    for drop in ("matrix", "combdrift"):
        prov = json.loads(json.dumps(_STAMPED_PROV))
        prov.pop(drop)
        svc, appender = _service([(7, _ANCHOR, "dont")])
        report = svc.ingest(_receipt(_verified_lines(), provenance=prov))
        assert [row[1] for row in appender.batches[0]] == [EK_CHANGE_OUTCOME]
        assert report.matched == 1 and len(report.inserted) == 1
        assert (report.verified_helped, report.verified_hurt) == (0, 0)


def test_neutral_polarity_abstains_from_the_verified_rider():
    svc, appender = _service([(7, _ANCHOR)])  # neutral polarity
    report = svc.ingest(_stamped_receipt(_verified_lines()))
    kinds = [row[1] for row in appender.batches[0]]
    assert kinds == [EK_CHANGE_OUTCOME]  # no verified row
    assert report.verified_helped == 0


def test_post_merge_stamped_ingest_writes_rider_rows():
    # THE named-mutation twin (CT-9 rider): the verified-outcome rider is gated
    # ONLY on the full version stamp — ANY phase; the canonical post_merge ingest
    # is its normal carrier in v3. Restoring the old `phase == "pre_merge"`
    # rider condition reds here.
    svc, appender = _service([(7, _ANCHOR, "dont")])
    report = svc.ingest(
        _stamped_receipt(_verified_lines()),
        phase="post_merge",
        verdict="fail",
        signal="canary",
    )
    kinds = [row[1] for row in appender.batches[0]]
    assert kinds == [EK_CHANGE_OUTCOME, EK_OUTCOME_VERIFIED_HELPED]
    assert report.verified_helped == 1
    # the decided failing tests line derived the outcome verdict
    assert json.loads(appender.batches[0][0][4])["verdict"] == "fail"


def test_stampless_post_merge_ingest_writes_no_riders():
    # the gate is the STAMP, not the phase: a stamp-less post_merge receipt still
    # lands its plain change_outcome row and nothing else.
    svc, appender = _service([(7, _ANCHOR, "dont")])
    report = svc.ingest(
        _receipt(_verified_lines()), phase="post_merge", verdict="fail", signal="canary"
    )
    assert [row[1] for row in appender.batches[0]] == [EK_CHANGE_OUTCOME]
    assert (report.verified_helped, report.verified_hurt) == (0, 0)


def test_change_outcome_row_shape_is_unchanged_by_the_extension():
    # the S4 golden holds: the change_outcome payload keeps its exact key set (the
    # byte pin lives in test_render_payload_exact_golden_bytes) even when verified/
    # verify rows ride the same batch — the extension leaks nothing into it.
    svc, appender = _service([(7, _ANCHOR, "dont")])
    svc.ingest(_stamped_receipt(_verified_lines()))
    outcome_payload = json.loads(appender.batches[0][0][4])
    assert set(outcome_payload) == {
        "schema",
        "base_sha",
        "head_sha",
        "receipt_sha256",
        "receipt_schema_version",
        "predicate_type",
        "phase",
        "verdict",
        "tag",
        "signal",
        "matched",
        "hive_census_version",
    }
    assert outcome_payload["schema"] == "change_outcome/v1"


def test_payloads_are_ids_enums_and_stamps_only():
    prose = "REASONPROSE the loader crashed because the flange rotated"
    lines = [
        _detail_line(
            "existence",
            _ANCHOR,
            {"existed_before": True, "exists_after": False},
            reason=prose,
        ),
        _detail_line("contract", _ANCHOR, {"drift": "removed"}, reason=prose),
        _detail_line(
            "regression",
            _ANCHOR,
            {"drift": "removed", "tests": ["t_reach"]},
            reason=prose,
        ),
        _exec_line("typecheck", "passed"),
        _exec_line("tests", "failed"),
    ]
    svc, appender = _service([(7, _ANCHOR, "dont")])
    svc.ingest(_stamped_receipt(lines))
    by_kind = {row[1]: row[4] for row in appender.batches[0]}
    verified = json.loads(by_kind[EK_OUTCOME_VERIFIED_HELPED])
    assert set(verified) == {
        "schema",
        "receipt_sha256",
        "phase",
        "verdict",
        "tag",
        "matched",
        "reason",
        "stamp",
    }
    assert verified["schema"] == "outcome_verified/v1"
    assert verified["reason"] == "corroborated"  # a machine enum, never prose
    assert verified["matched"] == {
        "path": "matrix/x.py",
        "symbol": "LanguageConfig",
        "level": "symbol",
    }
    assert verified["stamp"]["matrix_head"]["graph_sha256"] == "f" * 64
    assert not any(k.startswith("verify_") for k in by_kind), by_kind
    for payload in by_kind.values():  # Law 4: no receipt prose
        assert "REASONPROSE" not in payload
    # byte-stable across calls (the idempotency key across both kinds)
    svc2, appender2 = _service([(7, _ANCHOR, "dont")])
    svc2.ingest(_stamped_receipt(lines))
    assert appender2.batches[0] == appender.batches[0]


def test_hurt_reason_is_contradicted():
    svc, appender = _service([(7, _ANCHOR, "do")])
    svc.ingest(_stamped_receipt(_verified_lines(drift="unchanged")))
    by_kind = {row[1]: row[4] for row in appender.batches[0]}
    assert json.loads(by_kind[EK_OUTCOME_VERIFIED_HURT])["reason"] == "contradicted"


def test_report_counters_default_zero_and_are_additive():
    rep = IngestReport(inserted=(), already_recorded=0, matched=0, skipped_lines=0)
    assert (rep.verified_helped, rep.verified_hurt) == (0, 0)
    assert rep.stale_suspects == 0
    # the retired verification channel keeps no counter of its own
    assert not any(f.startswith("verify_") for f in IngestReport.__dataclass_fields__)


# ═══ T2: graph-propagated staleness — the propagation block → stale_suspect rows ═

_SEED = "matrix/x.py::LanguageConfig"
_NEIGHBOR_ANCHOR = "pkg/caller.py::caller_fn"


def _prop_entry(
    *, seed=_SEED, drift="removed", depth=2, neighbors=("pkg/caller.py::caller_fn",)
):
    return {"seed": seed, "drift": drift, "depth": depth, "neighbors": list(neighbors)}


def _prop_receipt(lines, propagation, *, provenance=None):
    """A stamped receipt whose predicate carries the optional propagation block."""
    prov = (
        provenance if provenance is not None else json.loads(json.dumps(_STAMPED_PROV))
    )
    predicate = {
        "schema_version": "v0",
        "lines": lines or [],
        "provenance": prov,
        "propagation": propagation,
    }
    d = hashlib.sha256(_canon(predicate)).hexdigest()
    statement = {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": [{"name": "hive-census-receipt", "digest": {"sha256": d}}],
        "predicateType": "urn:hive-census:receipt:v0",
        "predicate": predicate,
    }
    return {
        "payload": base64.b64encode(_canon(statement)).decode("ascii"),
        "payloadType": "application/vnd.in-toto+json",
        "signatures": [{"keyid": "key-1", "sig": "c2ln"}],
    }


# ── propagation_subjects: D8 over the optional block ───────────────────────────


def test_propagation_subjects_parses_neighbor_seed_drift_triples():
    subjects, skipped = propagation_subjects(
        {
            "propagation": [
                _prop_entry(neighbors=["a.py::F", "b.py::G"]),
                _prop_entry(
                    seed="matrix/y.py::Other", drift="breaking", neighbors=["a.py::F"]
                ),  # dup subject, second seed
            ]
        }
    )
    assert skipped == 0
    assert subjects == [
        (TouchedSubject("a.py", "F"), _SEED, "removed"),
        (TouchedSubject("b.py", "G"), _SEED, "removed"),
        (TouchedSubject("a.py", "F"), "matrix/y.py::Other", "breaking"),
    ]


def test_propagation_subjects_absent_or_non_list_block_is_empty():
    assert propagation_subjects({}) == ([], 0)
    assert propagation_subjects({"propagation": "nope"}) == ([], 0)
    assert propagation_subjects("not-a-dict") == ([], 0)
    assert propagation_subjects({"propagation": []}) == ([], 0)


def test_propagation_subjects_malformed_entries_skipped_and_counted():
    subjects, skipped = propagation_subjects(
        {
            "propagation": [
                "garbage",  # non-dict entry
                {
                    "seed": 7,
                    "drift": "removed",
                    "neighbors": ["a.py::F"],
                },  # non-str seed
                {
                    "seed": _SEED,
                    "drift": "additive",
                    "neighbors": ["a.py::F"],
                },  # off-vocabulary drift
                {
                    "seed": _SEED,
                    "drift": "removed",
                    "neighbors": "a.py::F",
                },  # non-list neighbors
                _prop_entry(
                    neighbors=[
                        "ok.py::Fine",
                        7,
                        "no-separator",
                        "::NoPath",
                        "no-symbol::",
                    ]
                ),
            ]
        }
    )
    assert skipped == 4  # entry-level malformation only
    assert subjects == [(TouchedSubject("ok.py", "Fine"), _SEED, "removed")]


def test_propagation_subjects_dedupes_identical_triples():
    subjects, _ = propagation_subjects(
        {
            "propagation": [
                _prop_entry(neighbors=["a.py::F", "a.py::F"]),
                _prop_entry(neighbors=["a.py::F"]),
            ]
        }
    )
    assert subjects == [(TouchedSubject("a.py", "F"), _SEED, "removed")]


# ── the ingest rider: suspect rows in the same batch, point-dominated ───────────


def test_ingest_writes_stale_suspect_rows_in_the_same_batch():
    svc, appender = _service([(7, _NEIGHBOR_ANCHOR, "neutral")])
    report = svc.ingest(_prop_receipt(_verified_lines(), [_prop_entry()]))
    assert len(appender.batches) == 1  # ONE atomic batch
    suspect_rows = [row for row in appender.batches[0] if row[1] == EK_STALE_SUSPECT]
    assert len(suspect_rows) == 1
    eid, kind, actor, ts, payload = suspect_rows[0]
    assert (eid, actor, ts) == (7, "census", 12_345)
    body = json.loads(payload)
    assert set(body) == {"schema", "matched", "seed", "drift", "stamp"}
    assert body["schema"] == "stale_suspect/v1"
    assert body["matched"] == {"path": "pkg/caller.py", "symbol": "caller_fn"}
    assert (body["seed"], body["drift"]) == (_SEED, "removed")
    assert body["stamp"]["head_sha"] == HEAD  # the T1 version stamp rides
    assert report.stale_suspects == 1
    # re-ingest: content-keyed idempotency covers the suspect kind too
    again = svc.ingest(_prop_receipt(_verified_lines(), [_prop_entry()]))
    assert again.inserted == () and again.already_recorded >= 1
    assert again.stale_suspects == 1  # rendered, not re-inserted


def test_point_match_suppresses_the_suspect_row_for_the_same_episode():
    # the episode's anchor matches the receipt's TOUCHED subject (a point row) AND a
    # propagation neighbor names the same subject — point evidence dominates: no suspect row.
    svc, appender = _service([(7, _ANCHOR, "dont")])
    report = svc.ingest(
        _prop_receipt(_verified_lines(), [_prop_entry(neighbors=[_SEED])])
    )
    kinds = [row[1] for row in appender.batches[0]]
    assert EK_CHANGE_OUTCOME in kinds and EK_STALE_SUSPECT not in kinds
    assert report.stale_suspects == 0


def test_propagation_free_receipt_writes_zero_suspect_rows():
    # byte-inert: the S4/T1 batch is unchanged when no propagation block rides the receipt.
    svc, appender = _service([(7, _ANCHOR, "dont"), (9, _NEIGHBOR_ANCHOR, "neutral")])
    report = svc.ingest(_stamped_receipt(_verified_lines()))
    assert all(row[1] != EK_STALE_SUSPECT for row in appender.batches[0])
    assert report.stale_suspects == 0


def test_suspect_rows_require_full_version_stamp():
    # L7: a stamp-less receipt writes NO suspect rows (the payload carries the stamp).
    prov = {"base_sha": BASE, "head_sha": HEAD, "hive_census_version": "0.1.0"}
    svc, appender = _service([(7, _NEIGHBOR_ANCHOR, "neutral"), (9, _ANCHOR, "dont")])
    report = svc.ingest(
        _prop_receipt(_verified_lines(), [_prop_entry()], provenance=prov)
    )
    assert all(row[1] != EK_STALE_SUSPECT for row in appender.batches[0])
    assert report.stale_suspects == 0


def test_post_merge_stamped_ingest_writes_suspect_rows():
    # the suspect rider follows the same stamp-only gate — the canonical
    # post_merge ingest carries it too (the phase gate is gone).
    svc, appender = _service([(7, _NEIGHBOR_ANCHOR, "neutral"), (9, _ANCHOR, "dont")])
    report = svc.ingest(
        _prop_receipt(_verified_lines(), [_prop_entry()]),
        phase="post_merge",
        verdict="fail",
        signal="canary",
    )
    suspect_rows = [row for row in appender.batches[0] if row[1] == EK_STALE_SUSPECT]
    assert len(suspect_rows) == 1 and suspect_rows[0][0] == 7
    assert report.stale_suspects == 1


def test_suspect_payload_is_ids_enums_and_stamps_only_and_byte_stable():
    prose = "REASONPROSE the flange rotated"
    entry = _prop_entry()
    entry["reason"] = prose  # unknown keys never ride
    svc, appender = _service([(7, _NEIGHBOR_ANCHOR, "neutral")])
    svc.ingest(_prop_receipt(_verified_lines(), [entry]))
    suspect = next(row for row in appender.batches[0] if row[1] == EK_STALE_SUSPECT)
    assert "REASONPROSE" not in suspect[4]  # Law 4: no receipt prose
    svc2, appender2 = _service([(7, _NEIGHBOR_ANCHOR, "neutral")])
    svc2.ingest(_prop_receipt(_verified_lines(), [entry]))
    assert appender2.batches[0] == appender.batches[0]  # the idempotency bytes


def test_malformed_propagation_entries_count_into_skipped_lines():
    svc, _ = _service([(7, _NEIGHBOR_ANCHOR, "neutral")])
    report = svc.ingest(_prop_receipt(_verified_lines(), ["garbage", _prop_entry()]))
    assert report.skipped_lines == 1  # the malformed entry, counted
    assert report.stale_suspects == 1  # the good one still lands


# ═══ U3: the receipt ref stamp threaded into the ledger payloads ═════════════════


def _ref_stamped_receipt(lines, ref="master"):
    prov = json.loads(json.dumps(_STAMPED_PROV))
    prov["ref"] = ref
    return _receipt(lines, provenance=prov)


def test_change_outcome_ref_defaults_empty_and_carries():
    assert _outcome().ref == ""  # defaulted — legacy constructors compile
    assert _outcome(ref="master").ref == "master"


def test_ingest_threads_ref_into_payloads():
    # A ref-stamped receipt: EVERY rendered payload — change_outcome AND the
    # verified-outcome rider — carries {"ref": "<the measured line>"}.
    svc, appender = _service([(7, _ANCHOR, "dont")])
    report = svc.ingest(_ref_stamped_receipt(_verified_lines()))
    kinds = [row[1] for row in appender.batches[0]]
    assert kinds == [EK_CHANGE_OUTCOME, EK_OUTCOME_VERIFIED_HELPED]
    assert report.matched == 1
    for row in appender.batches[0]:
        assert json.loads(row[4])["ref"] == "master", row[1]


def test_ingest_non_string_ref_is_ignored():
    # D8 at the boundary: a non-string provenance ref coerces to "" ⇒ no key rides.
    svc, appender = _service([(7, _ANCHOR, "dont")])
    svc.ingest(_ref_stamped_receipt(_verified_lines(), ref=7))
    for row in appender.batches[0]:
        assert "ref" not in json.loads(row[4]), row[1]


def test_ingest_legacy_receipt_payloads_byte_identical():
    # A legacy (ref-less) receipt renders payloads byte-identical to pre-U3 — pinned
    # by the exact change_outcome golden bytes — and content-keyed dedup with a
    # previously-ingested row is preserved (a re-ingest is absorbed, never duplicated).
    env = _receipt(_LINES)
    statement = json.loads(base64.b64decode(env["payload"]))
    sha = statement["subject"][0]["digest"]["sha256"]
    svc, appender = _service([(7, _ANCHOR)])
    first = svc.ingest(env)
    assert len(first.inserted) == 1
    payload = appender.batches[0][0][4]
    assert payload == (
        '{"base_sha":"' + BASE + '","head_sha":"' + HEAD + '",'
        '"hive_census_version":"0.1.0",'
        '"matched":{"level":"symbol","path":"matrix/x.py","symbol":"LanguageConfig"},'
        '"phase":"pre_merge",'
        '"predicate_type":"urn:hive-census:receipt:v0",'
        '"receipt_schema_version":"v0",'
        '"receipt_sha256":"' + sha + '",'
        '"schema":"change_outcome/v1",'
        '"signal":"none",'
        '"tag":"machine-checked",'
        '"verdict":"pass"}'
    )
    again = svc.ingest(_receipt(_LINES))
    assert again.inserted == () and again.already_recorded == 1


def test_legacy_stamped_receipt_rider_payloads_carry_no_ref_key():
    # The verified-outcome rider of a ref-LESS stamped receipt keeps its exact pre-U3
    # key set (the conditional key is absent, never null) — dedup bytes unchanged.
    svc, appender = _service([(7, _ANCHOR, "dont")])
    svc.ingest(_stamped_receipt(_verified_lines()))
    by_kind = {row[1]: json.loads(row[4]) for row in appender.batches[0]}
    assert set(by_kind[EK_OUTCOME_VERIFIED_HELPED]) == {
        "schema",
        "receipt_sha256",
        "phase",
        "verdict",
        "tag",
        "matched",
        "reason",
        "stamp",
    }


# ═══ U1: the repo identity key + the repo-scoped join + the range ledger ═════════

_REPO = "widgets"


def _repo_receipt(lines, repo=_REPO):
    prov = {
        "base_sha": BASE,
        "head_sha": HEAD,
        "hive_census_version": "0.1.0",
        "repo": repo,
    }
    return _receipt(lines, provenance=prov)


def _repo_rows(*pairs, polarity="neutral"):
    return [(eid, _REPO, anchor, polarity) for eid, anchor in pairs]


class FakeRangeLedger:
    """Honors the RangeLedger contract: exact-key membership, idempotent-bool record."""

    def __init__(self, known=()):
        self.known = {tuple(k) for k in known}
        self.recorded: list[tuple] = []

    def already_ingested_range(self, repo, base_sha, head_sha, phase):
        return (repo, base_sha, head_sha, phase) in self.known

    def record_ingested_range(self, repo, base_sha, head_sha, phase, ts):
        self.recorded.append((repo, base_sha, head_sha, phase, ts))
        key = (repo, base_sha, head_sha, phase)
        if key in self.known:
            return False
        self.known.add(key)
        return True


def _ranged_service(reader_rows=(), *, known=(), now=lambda: 12_345):
    reader, appender = FakeReader(_reader_rows(reader_rows)), FakeAppender()
    ranges = FakeRangeLedger(known)
    svc = ChangeEvidenceService(
        reader=reader, appender=appender, now=now, ranges=ranges
    )
    return svc, appender, ranges


# ── the repo provenance key: parsed like ref, scopes the join, rides the payload ─


def test_change_outcome_repo_defaults_empty_and_carries():
    assert _outcome().repo == ""  # defaulted — legacy constructors compile
    assert _outcome(repo=_REPO).repo == _REPO


def test_service_cross_repo_anchor_rows_never_receive_evidence():
    # the ingest-level twin of the join filter: the SAME anchor recorded under
    # another repo gets no row from this repo's receipt.
    svc, appender = _service(
        _repo_rows((7, _ANCHOR)) + [(9, "other-repo", _ANCHOR, "neutral")]
    )
    report = svc.ingest(_repo_receipt(_LINES))
    assert report.matched == 1
    assert [row[0] for row in appender.batches[0]] == [7]


def test_ingest_threads_repo_into_the_change_outcome_payload_only():
    # repo rides the change_outcome payload; the verified-outcome rider keeps its
    # exact key set (ref is its only conditional key).
    prov = json.loads(json.dumps(_STAMPED_PROV))
    prov["repo"] = _REPO
    svc, appender = _service(_repo_rows((7, _ANCHOR), polarity="dont"))
    svc.ingest(_receipt(_verified_lines(), provenance=prov))
    by_kind = {row[1]: json.loads(row[4]) for row in appender.batches[0]}
    assert by_kind[EK_CHANGE_OUTCOME]["repo"] == _REPO
    assert "repo" not in by_kind[EK_OUTCOME_VERIFIED_HELPED]


def test_ingest_non_string_repo_is_ignored():
    # D8 at the boundary: a non-string provenance repo coerces to "" ⇒ no key
    # rides — and the join scopes to the legacy "" identity.
    svc, appender = _service([(7, _ANCHOR)])
    svc.ingest(_repo_receipt(_LINES, repo=7))
    assert "repo" not in json.loads(appender.batches[0][0][4])


def test_repo_key_absent_renders_legacy_byte_identical_payload():
    # THE repo-key legacy-bytes guard: a repo-less outcome renders the EXACT legacy
    # bytes (unconditional emission would move every already-landed row's dedup key),
    # and a repo-stamped payload differs from those bytes by exactly the repo key.
    sub = TouchedSubject(path="matrix/x.py", symbol="F")
    legacy = render_payload(_outcome(hive_census_version="0.1.0"), sub, "symbol")
    assert legacy == (
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
        '"verdict":"pass"}'
    )
    stamped = render_payload(
        _outcome(hive_census_version="0.1.0", repo="acme"), sub, "symbol"
    )
    assert json.loads(stamped)["repo"] == "acme"
    assert stamped.replace('"repo":"acme",', "") == legacy


# ── the optional range ledger: duplicate ranges absorbed BEFORE the store ──────


def test_ingest_skips_an_already_ingested_range_with_zero_rows():
    key = ("", BASE, HEAD, "pre_merge")  # legacy receipt ⇒ "" repo identity
    svc, appender, ranges = _ranged_service([(7, _ANCHOR)], known=[key])
    report = svc.ingest(_receipt(_LINES))
    assert report.range_skipped is True
    assert report.inserted == () and report.already_recorded == 0
    assert report.matched == 0 and report.skipped_lines == 0
    assert appender.batches == []  # zero rows — the store never touched
    assert ranges.recorded == []  # a skip never re-records


def test_ingest_records_the_range_after_a_successful_append():
    svc, appender, ranges = _ranged_service(_repo_rows((7, _ANCHOR)))
    report = svc.ingest(_repo_receipt(_LINES))
    assert report.range_skipped is False and len(report.inserted) == 1
    assert ranges.recorded == [(_REPO, BASE, HEAD, "pre_merge", 12_345)]
    # the second ingest of the same range is absorbed by the ledger, before dedupe
    again = svc.ingest(_repo_receipt(_LINES))
    assert again.range_skipped is True and again.inserted == ()
    assert len(appender.batches) == 1


def test_ingest_zero_match_never_records_the_range():
    # no batch appended ⇒ no range recorded: a re-ingest AFTER an anchored memory
    # lands still gets its rows (the ledger marks ingested WORK, not seen receipts).
    svc, appender, ranges = _ranged_service(
        _repo_rows((8, "unrelated/other.py::Thing"))
    )
    report = svc.ingest(_repo_receipt(_LINES))
    assert report.matched == 0 and report.range_skipped is False
    assert appender.batches == [] and ranges.recorded == []


def test_ingest_phase_rides_the_range_key_so_ff_merge_lands_twice():
    # an FF merge yields the same (base, head) pre- and post-merge — phase in the key
    # lets both receipts land.
    svc, _appender, ranges = _ranged_service(_repo_rows((7, _ANCHOR)))
    svc.ingest(_repo_receipt(_LINES))
    report = svc.ingest(_repo_receipt(_LINES), phase="post_merge", verdict="pass")
    assert report.range_skipped is False and len(report.inserted) == 1
    assert [k[3] for k in ranges.recorded] == ["pre_merge", "post_merge"]


def test_ingest_legacy_empty_repo_receipts_dedupe_on_the_empty_identity():
    svc, _appender, ranges = _ranged_service([(7, _ANCHOR)])
    first = svc.ingest(_receipt(_LINES))  # legacy: no provenance.repo
    assert first.range_skipped is False
    assert ranges.recorded[0][0] == ""  # the legacy "" identity
    again = svc.ingest(_receipt(_LINES))
    assert again.range_skipped is True


def test_ingest_without_a_ledger_is_byte_identical_to_today():
    # ranges=None (every legacy construction): the ledger leg is absent — batch bytes
    # and the report match a ledger-wired first ingest exactly, and a re-ingest is
    # absorbed by content-keyed dedupe alone (never range-skipped).
    plain_svc, plain_appender = _service(_repo_rows((7, _ANCHOR)))
    plain = plain_svc.ingest(_repo_receipt(_LINES))
    ranged_svc, ranged_appender, _ranges = _ranged_service(_repo_rows((7, _ANCHOR)))
    ranged = ranged_svc.ingest(_repo_receipt(_LINES))
    assert plain_appender.batches == ranged_appender.batches  # identical payload bytes
    assert plain == ranged
    assert plain.range_skipped is False
    again = plain_svc.ingest(_repo_receipt(_LINES))
    assert again.range_skipped is False and again.already_recorded == 1


def test_ingest_report_range_skipped_defaults_false():
    rep = IngestReport(inserted=(), already_recorded=0, matched=0, skipped_lines=0)
    assert rep.range_skipped is False


def test_refused_receipt_refuses_even_when_the_range_is_known():
    # refusal wins over the skip: the ledger absorbs only receipts the module can
    # vouch for — a malformed duplicate still refuses loudly, never a quiet "ok".
    key = ("", BASE, HEAD, "pre_merge")
    svc, appender, ranges = _ranged_service([(7, _ANCHOR)], known=[key])
    with pytest.raises(ReceiptRefused):
        svc.ingest(_receipt([_subj_line("existence", _ANCHOR)]))  # nothing decided
    assert appender.batches == [] and ranges.recorded == []


# ── IngestReport.touched_paths: what the range CONTAINED, on both exits ───────


def test_ingest_reports_the_paths_the_range_touched():
    svc, _appender = _service(_repo_rows((7, _ANCHOR)))
    report = svc.ingest(_repo_receipt(_LINES))
    # PATHS, not anchors: the daemon compares against an anchor's path component,
    # so a path::symbol member would never match and the deferral would never fire.
    assert report.touched_paths == frozenset({"matrix/x.py"})
    assert isinstance(report.touched_paths, frozenset)
    assert _ANCHOR not in report.touched_paths


def test_touched_paths_collect_every_joinable_subject_not_just_the_matched_ones():
    # a statement about the RANGE, not about the join: "matrix/y.py" is anchored by
    # no episode and still rides, and two symbols in one file collapse to one path.
    lines = [
        _subj_line("existence", "matrix/x.py::LanguageConfig"),
        _subj_line("contract", "matrix/x.py::OtherSymbol"),
        _subj_line("regression", "matrix/y.py::Thing"),
        _exec_line("typecheck", "passed"),
        _exec_line("tests", "passed"),
    ]
    svc, _appender = _service(_repo_rows((7, _ANCHOR)))
    report = svc.ingest(_repo_receipt(lines))
    assert report.matched == 1
    assert report.touched_paths == frozenset({"matrix/x.py", "matrix/y.py"})


def test_a_receipt_with_no_joinable_subjects_touches_no_paths():
    # summary subjects carry no "::" and never join, so the range contained nothing
    # an anchor could name — the empty frozenset, not a missing statement.
    svc, _appender = _service(_repo_rows((7, _ANCHOR)))
    report = svc.ingest(
        _repo_receipt(
            [_exec_line("typecheck", "passed"), _exec_line("tests", "passed")]
        )
    )
    assert report.matched == 0
    assert report.touched_paths == frozenset()


def test_a_range_skipped_report_still_states_what_the_range_contained():
    # THE load-bearing half: an already-ingested range did zero WORK but still
    # changed exactly those paths. A skip reporting nothing touched would let the
    # same tick baseline an anchor at the post-change tip and call it fresh.
    key = (_REPO, BASE, HEAD, "pre_merge")
    svc, appender, ranges = _ranged_service(_repo_rows((7, _ANCHOR)), known=[key])
    report = svc.ingest(_repo_receipt(_LINES))
    assert report.range_skipped is True
    assert report.touched_paths == frozenset({"matrix/x.py"})
    # every COUNT is still zero and the store is still untouched
    assert report.inserted == () and report.already_recorded == 0
    assert report.matched == 0 and report.skipped_lines == 0
    assert appender.batches == [] and ranges.recorded == []


def test_ingest_report_touched_paths_defaults_to_the_empty_frozenset():
    rep = IngestReport(inserted=(), already_recorded=0, matched=0, skipped_lines=0)
    assert rep.touched_paths == frozenset()


# ── BUG-058: the advertised anchor grammar must be the grammar the join parses ──
def test_advertised_anchor_grammar_joins_the_subject_feed():
    """The `path::symbol` form advertised to agents — in the `hive_write` schema,
    in the `anchors` property, and in the contract served at MCP `initialize` —
    MUST join a receipt subject at the precise `symbol` tier.

    This is the anti-fork pin for BUG-058: the join partitions the stored anchor
    on `'::'` alone, while the drift engine's `_split_anchor` accepts `':'` too.
    That asymmetry let a single-colon advertisement look healthy (drift verdicts
    kept working) while the memory silently earned no change_outcome, so it could
    never be outcome-verified, never promote, and died at the provisional TTL
    while still being correct. Re-advertising the single-colon form reds this.
    """
    from hive.app.contract import SERVER_INSTRUCTIONS
    from hive.app.tool_defs import TOOL_DEFINITIONS

    advertised = "path/file.py::symbol"
    surfaces = {"contract": SERVER_INSTRUCTIONS}
    for spec in TOOL_DEFINITIONS:
        if spec["name"] == "hive_write":
            surfaces["hive_write.description"] = spec["description"]
            surfaces["anchors.property"] = json.dumps(spec["inputSchema"])
    assert len(surfaces) == 3, f"the advertising surfaces moved: {sorted(surfaces)}"
    for where, text in surfaces.items():
        assert advertised in text, (
            f"{where} must advertise {advertised!r} — the ONE form the census join "
            f"parses; a single-colon anchor never matches any tier"
        )

    # the advertised form, made concrete, joins at the precise tier
    subject = TouchedSubject(path="path/file.py", symbol="symbol")
    assert ce._anchor_match_level([advertised], subject) == "symbol"
    # the file-scoped form still joins one tier down (unaffected by the fork)
    assert ce._anchor_match_level(["path/file.py"], subject) == "file"
    # and the form that caused the bug still does not — pinned so the asymmetry
    # stays visible rather than being silently "fixed" in the wrong half
    assert ce._anchor_match_level(["path/file.py:symbol"], subject) is None
