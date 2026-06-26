"""DefaultSecretScanner adapter — the config-bound wrapper over the pure scan, including
the operator opt-OUT (`enabled`). When enabled (default) the floor is byte-identical to the
pure scan; when disabled it returns a valid CLEAN verdict unconditionally (the bypass the
HIVE_SECRET_SCAN__ENABLED knob selects), so raw text is staged unscanned by operator choice.
"""
from __future__ import annotations

from hive.adapters.scanner_regex import DefaultSecretScanner
from hive.domain.secret_scan import CLEAN, REDACT, REFUSE, ScanVerdict

SECRET = "AKIAIOSFODNN7EXAMPLE"          # matches the aws_akia named rule


# ── enabled (default): byte-identical to the always-on floor ──────────────────
def test_enabled_scanner_refuses_secret():
    # the default floor still REFUSEs a credential — the byte-identity proof for §9.9.
    verdict = DefaultSecretScanner().scan(f"my key {SECRET}")
    assert verdict.action == REFUSE
    assert verdict.findings                       # the fired rule(s) are reported


def test_enabled_scanner_clean_on_benign():
    assert DefaultSecretScanner().scan("a perfectly ordinary note").action == CLEAN


# ── disabled: the operator opt-OUT bypasses the scan ──────────────────────────
def test_disabled_scanner_returns_clean_on_secret():
    # enabled=False ⇒ a credential-bearing text scans CLEAN (the floor is bypassed).
    verdict = DefaultSecretScanner(enabled=False).scan(f"my key {SECRET}")
    assert verdict.action == CLEAN
    assert verdict.findings == ()
    assert verdict.redacted_text is None


def test_disabled_scanner_clean_even_in_redact_mode():
    # disabling overrides redact_mode too — the floor is OFF, not "redact instead of refuse".
    verdict = DefaultSecretScanner(redact_mode=REDACT, enabled=False).scan(f"key {SECRET}")
    assert verdict.action == CLEAN
    assert verdict.redacted_text is None


def test_disabled_scanner_verdict_is_valid():
    # the bypass returns a structurally valid CLEAN ScanVerdict (the can't-lie invariant holds:
    # a CLEAN with no findings is legal; "not scanning" is never encoded as a false verdict).
    verdict = DefaultSecretScanner(enabled=False).scan(SECRET)
    assert isinstance(verdict, ScanVerdict)
    ScanVerdict(action=verdict.action, redacted_text=verdict.redacted_text,
                findings=verdict.findings)        # re-construct ⇒ __post_init__ re-validates
