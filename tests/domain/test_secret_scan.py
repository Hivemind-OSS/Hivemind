"""P1.4 — M05 secret floor: the §9 pattern set + Shannon-entropy catch-all, and the
ScanVerdict-cannot-lie invariants. The single most security-critical domain unit:
a credential must never survive the scan into a staged row/blob/log.

Each per-rule test asserts the SPECIFIC rule fired (not merely action==refuse) — so
deleting that one regex turns the test red even though the entropy catch-all would
still refuse the token (the RULE-2 per-family coverage the design review demanded).
"""
from __future__ import annotations

import hashlib

import pytest

from hive.domain.secret_scan import (
    CLEAN, REDACT, REFUSE, ScanVerdict, SecretFinding, scan, token_entropy_bits,
)


def _rules(v: ScanVerdict) -> set[str]:
    return {f.rule for f in v.findings}


# ── one test per §9 rule family (delete-the-regex ⟹ this test red) ────────────
def test_aws_akia_refused():
    v = scan("aws key AKIAIOSFODNN7EXAMPLE here")
    assert v.action == REFUSE and "aws_akia" in _rules(v)


def test_sk_refused():
    v = scan("export OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz0123456789")
    assert v.action == REFUSE and "openai_key" in _rules(v)


def test_ghp_refused():
    v = scan("token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789ab")
    assert v.action == REFUSE and "github_pat" in _rules(v)


def test_xox_refused():
    v = scan("slack xoxb-123456789012-ABCDEFabcdef0123456789")
    assert v.action == REFUSE and "slack_token" in _rules(v)


def test_jwt_refused():
    jwt = ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
           ".eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvZSJ9"
           ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c")
    v = scan(f"auth bearer {jwt}")
    assert v.action == REFUSE and "jwt" in _rules(v)


def test_pem_refused():
    v = scan("-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKC\n-----END RSA PRIVATE KEY-----")
    assert v.action == REFUSE and "pem_private_key" in _rules(v)


def test_connstring_refused():
    v = scan("DATABASE_URL=postgres://admin:s3cr3tP4ssw0rd@db.example.com:5432/prod")
    assert v.action == REFUSE and "connection_string" in _rules(v)


def test_pypi_token_refused():
    # H≈3.98 < 4.0 ⟹ the entropy catch-all does NOT fire; only the named rule does
    # (audit wf_41f1e8af-590). Deleting the pypi_token regex turns this red.
    v = scan("secret: pypi-AgEIcHlwaS5vcmc")
    assert v.action == REFUSE and "pypi_token" in _rules(v)


def test_low_entropy_residual_is_deliberately_clean():
    # Documents the NAMED low-entropy residual (audit wf_41f1e8af-590): repeated /
    # limited-alphabet tokens are NOT a credential format and a detector broad
    # enough to catch them refuses benign separators/bit-masks/placeholders. The
    # floor targets high-entropy accidental pastes; this is an accepted, conscious
    # gap (spec §9 out-of-scope), NOT an oversight — if you add a detector, update
    # the secret_scan residual note and flip these expectations deliberately.
    for benign in ("--------------------", "====================",
                   "0b1010101010101010", "XXXXXXXXXXXXXXXXXXXX"):
        assert scan(f"note {benign} end").action == CLEAN, benign


# ── entropy catch-all: the boundary pair around bits_floor=4.0 at min_len=20 ──
def test_entropy_boundary_pair():
    # below: 14-symbol alphabet padded to 20 → H≈3.72 bits/char (< 4.0) ⟹ clean
    low = "0123456789abcd012345"
    # above: 20 distinct chars → H=log2(20)≈4.32 bits/char (> 4.0) ⟹ refuse
    high = "0123456789abcdefghij"
    assert len(low) == 20 and len(high) == 20
    assert token_entropy_bits(low) < 4.0 < token_entropy_bits(high)   # genuine straddle
    assert scan(f"id {low} end").action == CLEAN
    v = scan(f"id {high} end")
    assert v.action == REFUSE and "entropy" in _rules(v)


def test_entropy_ignores_short_and_low_entropy_prose():
    # normal prose tokens are short and/or low-entropy ⟹ never refused
    assert scan("the database connection pool was exhausted under load").action == CLEAN
    # a 40-char hex git SHA sits at H≈4.0 (16-symbol alphabet) and must PASS (git corpus)
    assert scan("fixed in commit deadbeefcafe1234567890abcdef0123456789ab").action == CLEAN


# ── ScanVerdict cannot lie (frozen __post_init__) ─────────────────────────────
def test_verdict_cannot_lie():
    with pytest.raises(ValueError):
        ScanVerdict(action=CLEAN, redacted_text=None, findings=(SecretFinding("x", (0, 1)),))
    with pytest.raises(ValueError):
        ScanVerdict(action=REDACT, redacted_text=None, findings=(SecretFinding("x", (0, 1)),))
    with pytest.raises(ValueError):
        ScanVerdict(action=REFUSE, redacted_text=None, findings=())


def test_finding_carries_no_secret():
    secret = "AKIAIOSFODNN7EXAMPLE"
    v = scan(f"key {secret}")
    assert v.findings
    for f in v.findings:
        # the only fields are rule (a label) + span (offsets) — never the matched bytes
        assert secret not in f.rule
        assert secret not in repr(f)
        assert isinstance(f.span, tuple) and len(f.span) == 2
        # the span indexes back to the secret in the ORIGINAL text (offsets, not bytes)
        s, e = f.span
        if f.rule == "aws_akia":
            assert f"key {secret}"[s:e] == secret


# ── redact mode: staged text carries the offsets masked, never the raw secret ─
def test_redact_masks_secret_and_changes_hash():
    secret = "sk-abcdefghijklmnopqrstuvwxyz0123456789"
    text = f"my key is {secret} ok"
    v = scan(text, mode="redact")
    assert v.action == REDACT and v.redacted_text is not None
    assert secret not in v.redacted_text                    # raw secret gone
    assert "[REDACTED]" in v.redacted_text
    h_orig = hashlib.sha256(text.encode()).hexdigest()
    h_red = hashlib.sha256(v.redacted_text.encode()).hexdigest()
    assert h_red != h_orig                                  # dedup keys on redacted text


def test_clean_text_is_clean_no_findings():
    v = scan("the retry backoff should be exponential with jitter")
    assert v.action == CLEAN and v.findings == () and v.redacted_text is None
