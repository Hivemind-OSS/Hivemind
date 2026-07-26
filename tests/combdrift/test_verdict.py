"""Chunk 3: per-record verdict policy. unverifiable > stale > current."""

from __future__ import annotations

from pathlib import Path

from hive.combdrift.resolution import fingerprint_anchor
from hive.combdrift.types import Anchor
from hive.combdrift.verdict import verify_record


def _current_anchor(repo: str, path: str, symbol: str) -> tuple[str, str, str]:
    """Anchor triple carrying a real minted baseline, so it can verdict `current`.

    A found callable with NO recorded fingerprint is unverifiable — its shape was
    never compared — so any anchor a test needs to be current must first be made
    measurable against the working tree it will be verified in.
    """
    token = fingerprint_anchor(repo, Anchor(path, symbol))
    assert token is not None  # the fixture symbol must be a single mintable callable
    return (path, symbol, token)


def test_verify_record_all_found_is_current(tmp_repo: Path, make_record):
    # Both anchors carry a minted baseline: `current` is a claim that a shape
    # comparison actually ran, not merely that the symbols still exist.
    repo = str(tmp_repo)
    rec = make_record(
        "m1",
        _current_anchor(repo, "pkg/auth.py", "refresh"),
        _current_anchor(repo, "pkg/parser.py", "parse"),
    )
    rv = verify_record(repo, rec)
    assert rv.id == "m1"
    assert rv.verdict == "current"
    assert all(a.found for a in rv.anchors)
    assert len(rv.anchors) == 2


def test_verify_record_missing_symbol_is_stale(tmp_repo: Path, make_record):
    # File present, symbol gone -> stale (not unverifiable, not current).
    rec = make_record("m2", ("pkg/auth.py", "renamed_away"))
    rv = verify_record(str(tmp_repo), rec)
    assert rv.verdict == "stale"


def test_verify_record_missing_file_is_stale(tmp_repo: Path, make_record):
    rec = make_record("m3", ("pkg/deleted.py", "anything"))
    rv = verify_record(str(tmp_repo), rec)
    assert rv.verdict == "stale"


def test_verify_record_indirect_is_unverifiable(tmp_repo: Path, make_record):
    # A present-but-indirect symbol (data binding) is never stale -> unverifiable.
    rec = make_record("ind", ("pkg/auth.py", "TOKEN_TTL"))
    assert verify_record(str(tmp_repo), rec).verdict == "unverifiable"


def test_verify_record_indirect_dominates_stale(tmp_repo: Path, make_record):
    # Indirect (unverifiable) + a missing file (stale-signal) -> unverifiable.
    rec = make_record(
        "ind2",
        ("pkg/auth.py", "TOKEN_TTL"),  # indirect -> unverifiable
        ("pkg/deleted.py", "x"),  # stale-signal
    )
    assert verify_record(str(tmp_repo), rec).verdict == "unverifiable"


def test_verify_record_no_anchors_unverifiable(tmp_repo: Path, make_record):
    rec = make_record("m4")  # zero anchors
    rv = verify_record(str(tmp_repo), rec)
    assert rv.verdict == "unverifiable"
    assert rv.anchors == ()


def test_verify_record_outside_repo_unverifiable(tmp_repo: Path, make_record):
    rec = make_record("m5", ("../escape.py", None))
    rv = verify_record(str(tmp_repo), rec)
    assert rv.verdict == "unverifiable"


def test_verify_record_parse_error_unverifiable(tmp_repo: Path, make_record):
    rec = make_record("m6", ("pkg/broken.py", "oops"))
    rv = verify_record(str(tmp_repo), rec)
    assert rv.verdict == "unverifiable"


def test_verify_record_precedence_unverifiable_dominates_stale(
    tmp_repo: Path, make_record
):
    # One stale anchor (missing file) + one unverifiable anchor (parse error).
    # Precedence: the record is unverifiable, not stale.
    rec = make_record(
        "m7",
        ("pkg/deleted.py", "x"),  # stale-signal
        ("pkg/broken.py", "oops"),  # unverifiable-signal
    )
    rv = verify_record(str(tmp_repo), rec)
    assert rv.verdict == "unverifiable"


def test_verify_record_precedence_stale_dominates_current(tmp_repo: Path, make_record):
    # One current anchor + one missing-symbol anchor -> stale (not current). The
    # current anchor is fingerprinted so it contributes a genuine current signal;
    # bare, it would raise the record to unverifiable and hide the precedence.
    rec = make_record(
        "m8",
        _current_anchor(str(tmp_repo), "pkg/auth.py", "refresh"),  # current
        ("pkg/auth.py", "gone"),  # stale-signal
    )
    rv = verify_record(str(tmp_repo), rec)
    assert rv.verdict == "stale"


def test_verify_record_outside_repo_dominates_missing_file(tmp_repo: Path, make_record):
    rec = make_record(
        "m9",
        ("pkg/deleted.py", "x"),  # stale-signal (file_missing)
        ("/etc/passwd", None),  # unverifiable-signal (path_outside_repo)
    )
    rv = verify_record(str(tmp_repo), rec)
    assert rv.verdict == "unverifiable"


def test_verify_record_preserves_anchor_order(tmp_repo: Path, make_record):
    rec = make_record(
        "m10",
        ("pkg/parser.py", "parse"),
        ("pkg/auth.py", "refresh"),
        ("pkg/deleted.py", "x"),
    )
    rv = verify_record(str(tmp_repo), rec)
    assert [a.path for a in rv.anchors] == [
        "pkg/parser.py",
        "pkg/auth.py",
        "pkg/deleted.py",
    ]


# --- Layer B: fingerprint drift verdicts --------------------------------------


def test_found_but_unfingerprinted_is_unverifiable_not_current(
    tmp_repo: Path, make_record
):
    # The recorded baseline is the ONLY difference between the two records here.
    # Resolving a symbol proves it exists, not that its call shape still fits, so
    # an uncompared callable abstains; it earns `current` once a comparison runs.
    repo = str(tmp_repo)
    bare = make_record("nofp", ("pkg/auth.py", "refresh"), ("pkg/parser.py", "parse"))
    rv = verify_record(repo, bare)
    assert rv.verdict == "unverifiable"
    assert all(a.found for a in rv.anchors)  # existence proven, shape unknown
    assert [a.reason for a in rv.anchors] == ["no_fingerprint", "no_fingerprint"]
    assert [a.location for a in rv.anchors] == ["pkg/auth.py:4", "pkg/parser.py:1"]

    minted = make_record(
        "fp",
        _current_anchor(repo, "pkg/auth.py", "refresh"),
        _current_anchor(repo, "pkg/parser.py", "parse"),
    )
    assert verify_record(repo, minted).verdict == "current"


def test_py_fingerprint_capture_then_unchanged_is_current(tmp_repo: Path, make_record):
    repo = str(tmp_repo)
    fp = fingerprint_anchor(repo, Anchor("pkg/parser.py", "parse"))
    rec = make_record("fp1", ("pkg/parser.py", "parse", fp))
    rv = verify_record(repo, rec)
    assert rv.verdict == "current"
    assert rv.anchors[0].reason == "ok"


def test_py_signature_changed_is_stale(tmp_repo: Path, make_record):
    repo = str(tmp_repo)
    fp = fingerprint_anchor(repo, Anchor("pkg/parser.py", "parse"))  # 3 args
    (tmp_repo / "pkg/parser.py").write_text(
        "def parse(a, b):\n    return (a, b)\n", encoding="utf-8"
    )
    rec = make_record("fp2", ("pkg/parser.py", "parse", fp))
    rv = verify_record(repo, rec)
    assert rv.verdict == "stale"
    assert rv.anchors[0].found is True  # the symbol is present; its shape drifted


def test_py_overloaded_symbol_with_fingerprint_is_unverifiable(
    tmp_repo: Path, make_record
):
    repo = str(tmp_repo)
    (tmp_repo / "pkg/over.py").write_text(
        "def dup(a):\n    return a\n\n\ndef dup(a, b):\n    return (a, b)\n",
        encoding="utf-8",
    )
    # Capture refuses to mint for an ambiguous symbol.
    assert fingerprint_anchor(repo, Anchor("pkg/over.py", "dup")) is None
    # A fingerprint supplied anyway compares as unverifiable, never stale.
    token = "combdrift-fp/1:func(req=1,max=1,star=0,kw=0,kwo=0,gen=0,dec=,base=0)"
    rec = make_record("fp3", ("pkg/over.py", "dup", token))
    assert verify_record(repo, rec).verdict == "unverifiable"


def test_fingerprint_version_mismatch_dominates_signature_changed(
    tmp_repo: Path, make_record
):
    repo = str(tmp_repo)
    fp = fingerprint_anchor(repo, Anchor("pkg/parser.py", "parse"))
    (tmp_repo / "pkg/parser.py").write_text(
        "def parse(a, b):\n    return (a, b)\n", encoding="utf-8"
    )
    future = "combdrift-fp/9:func(req=0,max=0,star=0,kw=0,kwo=0,gen=0,dec=,base=0)"
    rec = make_record(
        "fp4",
        ("pkg/parser.py", "parse", fp),  # signature_changed (stale-signal)
        ("pkg/auth.py", "refresh", future),  # version mismatch (unverifiable)
    )
    assert verify_record(repo, rec).verdict == "unverifiable"


# --- JS/TS verdicts -----------------------------------------------------------


def test_verify_ts_record_all_found_is_current(tmp_ts_repo: Path, make_record):
    # Minted baselines, as for Python: a TS callable is current because its shape
    # was compared, not merely because the declaration is still there.
    repo = str(tmp_ts_repo)
    rec = make_record(
        "t1",
        _current_anchor(repo, "src/auth.ts", "refresh"),
        _current_anchor(repo, "src/component.tsx", "Button"),
    )
    assert verify_record(repo, rec).verdict == "current"


def test_verify_ts_truly_absent_symbol_is_stale(tmp_ts_repo: Path, make_record):
    # A name bound nowhere in a clean TS file is provably absent -> stale.
    rec = make_record("t2", ("src/auth.ts", "deletedFn"))
    assert verify_record(str(tmp_ts_repo), rec).verdict == "stale"


def test_verify_ts_interface_is_unverifiable(tmp_ts_repo: Path, make_record):
    # A type-only declaration is present-but-indirect -> unverifiable, not stale.
    rec = make_record("t2b", ("src/auth.ts", "User"))
    assert verify_record(str(tmp_ts_repo), rec).verdict == "unverifiable"


def test_verify_ts_found_despite_error_is_current(tmp_ts_repo: Path, make_record):
    # A symbol declared cleanly ahead of the file's syntax error still mints and
    # compares, so error recovery costs it nothing.
    repo = str(tmp_ts_repo)
    rec = make_record("t3", _current_anchor(repo, "src/broken.ts", "good"))
    assert verify_record(repo, rec).verdict == "current"


def test_verify_ts_error_region_symbol_is_unverifiable(tmp_ts_repo: Path, make_record):
    rec = make_record("t4", ("src/broken.ts", "bad"))
    assert verify_record(str(tmp_ts_repo), rec).verdict == "unverifiable"


def test_verify_unsupported_file_is_unverifiable(tmp_ts_repo: Path, make_record):
    rec = make_record("t5", ("src/data.json", None))
    assert verify_record(str(tmp_ts_repo), rec).verdict == "unverifiable"


def test_verify_unsupported_dominates_stale(tmp_ts_repo: Path, make_record):
    # Precedence: an unsupported anchor makes the record unverifiable even
    # alongside a stale (missing-symbol) anchor.
    rec = make_record(
        "t6",
        ("src/auth.ts", "deletedFn"),  # stale-signal (provably absent)
        ("src/data.json", None),  # unverifiable-signal (unsupported)
    )
    assert verify_record(str(tmp_ts_repo), rec).verdict == "unverifiable"


def test_verify_mixed_python_and_ts_record_is_current(tmp_ts_repo: Path, make_record):
    # One record spanning two languages: both anchors compare against a minted
    # baseline, so the cross-language record is current on measured evidence.
    repo = str(tmp_ts_repo)
    rec = make_record(
        "t7",
        _current_anchor(repo, "src/auth.ts", "refresh"),
        _current_anchor(repo, "src/legacy.py", "old"),
    )
    assert verify_record(repo, rec).verdict == "current"


# --- schema verdicts ----------------------------------------------------------


def test_verify_schema_table_and_column_is_current(tmp_schema_repo: Path, make_record):
    rec = make_record(
        "s1", ("db/schema.sql", "users"), ("db/schema.sql", "users.email")
    )
    assert verify_record(str(tmp_schema_repo), rec).verdict == "current"


def test_verify_schema_absent_column_is_stale(tmp_schema_repo: Path, make_record):
    # A column absent from a closed, fully-parsed table is provably gone -> stale.
    rec = make_record("s2", ("db/schema.sql", "users.phone"))
    assert verify_record(str(tmp_schema_repo), rec).verdict == "stale"


def test_verify_schema_open_view_member_is_unverifiable(
    tmp_schema_repo: Path, make_record
):
    # A SELECT * view's member is never provably absent -> unverifiable, not stale.
    rec = make_record("s3", ("db/schema.sql", "active_users.anything"))
    assert verify_record(str(tmp_schema_repo), rec).verdict == "unverifiable"


def test_verify_schema_indirect_dominates_stale(tmp_schema_repo: Path, make_record):
    # An open-view member (unverifiable) alongside a missing-file anchor (stale):
    # precedence makes the record unverifiable.
    rec = make_record(
        "s4",
        ("db/schema.sql", "active_users.anything"),  # unverifiable-signal
        ("db/gone.sql", "ghost"),  # stale-signal (file missing)
    )
    assert verify_record(str(tmp_schema_repo), rec).verdict == "unverifiable"


def test_verify_schema_malformed_is_unverifiable(tmp_schema_repo: Path, make_record):
    rec = make_record("s5", ("db/broken.sql", "users"))
    assert verify_record(str(tmp_schema_repo), rec).verdict == "unverifiable"


def test_verify_json_absent_field_is_stale(tmp_schema_repo: Path, make_record):
    rec = make_record("s6", ("db/users.schema.json", "users.phone"))
    assert verify_record(str(tmp_schema_repo), rec).verdict == "stale"


def test_verify_schema_fingerprint_set_is_unverifiable_never_stale(
    tmp_schema_repo: Path, make_record
):
    # A fingerprint hand-set on a present schema member must never make it stale.
    token = "combdrift-fp/1:func(req=0,max=0,star=0,kw=0,kwo=0,gen=0,dec=,base=0)"
    rec = make_record("s7", ("db/schema.sql", "users.email", token))
    assert verify_record(str(tmp_schema_repo), rec).verdict == "unverifiable"
