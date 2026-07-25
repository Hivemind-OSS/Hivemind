"""DefaultSecretScanner adapter — the config-bound wrapper over the pure scan, including
the operator opt-OUT (`enabled`). When enabled (default) the floor is byte-identical to the
pure scan; when disabled it returns a valid CLEAN verdict unconditionally (the bypass the
HIVE_SECRET_SCAN__ENABLED knob selects), so raw text is staged unscanned by operator choice.
"""

from __future__ import annotations

from hive.adapters.scanner_regex import DefaultSecretScanner
from hive.domain.secret_scan import CLEAN, REDACT, REFUSE, ScanVerdict

SECRET = "AKIAIOSFODNN7EXAMPLE"  # matches the aws_akia named rule


# ── enabled (default): byte-identical to the always-on floor ──────────────────
def test_enabled_scanner_refuses_secret():
    # the default floor still REFUSEs a credential — the byte-identity proof for §9.9.
    verdict = DefaultSecretScanner().scan(f"my key {SECRET}")
    assert verdict.action == REFUSE
    assert verdict.findings  # the fired rule(s) are reported


def test_enabled_scanner_clean_on_benign():
    assert DefaultSecretScanner().scan("a perfectly ordinary note").action == CLEAN


def test_enabled_scanner_admits_identifier_shaped_memory_text():
    # BUG-018 through the injected-threshold wrapper: the structural exclusion is a
    # property of the pure scan, so the configured floor carries it unchanged and an
    # operator can leave HIVE_SECRET_SCAN__ENABLED on without losing legitimate writes.
    s = DefaultSecretScanner()
    for benign in (
        "interval_s/webhook_secret/mirror_dir/drift_per_tick/backfill_per_tick/workers",
        ".claude/hooks/mint_fp.py",
        "hive/adapters/store_sqlite.py::repo_remove",
    ):
        assert s.scan(f"the fix is at {benign}").action == CLEAN, benign


def test_configured_floor_is_still_the_entropy_lever_not_the_exclusion():
    # the exclusion is structural and threshold-free: a prefix-less blob is refused at
    # the shipped floor, and RAISING the injected floor (never lowering it — pypi
    # macaroons sit at H≈3.98) is what changes the entropy leg's reach.
    blob = "2mK9pQ7xZ4vL8nR3wT6yB1cF5hJ0dG2sA9eU4iO7kM3qX8zV"
    assert DefaultSecretScanner().scan(blob).action == REFUSE
    assert DefaultSecretScanner(entropy_bits_floor=6.0).scan(blob).action == CLEAN


# ── disabled: the operator opt-OUT bypasses the scan ──────────────────────────
def test_disabled_scanner_returns_clean_on_secret():
    # enabled=False ⇒ a credential-bearing text scans CLEAN (the floor is bypassed).
    verdict = DefaultSecretScanner(enabled=False).scan(f"my key {SECRET}")
    assert verdict.action == CLEAN
    assert verdict.findings == ()
    assert verdict.redacted_text is None


def test_disabled_scanner_clean_even_in_redact_mode():
    # disabling overrides redact_mode too — the floor is OFF, not "redact instead of refuse".
    verdict = DefaultSecretScanner(redact_mode=REDACT, enabled=False).scan(
        f"key {SECRET}"
    )
    assert verdict.action == CLEAN
    assert verdict.redacted_text is None


def test_disabled_scanner_verdict_is_valid():
    # the bypass returns a structurally valid CLEAN ScanVerdict (the can't-lie invariant holds:
    # a CLEAN with no findings is legal; "not scanning" is never encoded as a false verdict).
    verdict = DefaultSecretScanner(enabled=False).scan(SECRET)
    assert isinstance(verdict, ScanVerdict)
    ScanVerdict(
        action=verdict.action,
        redacted_text=verdict.redacted_text,
        findings=verdict.findings,
    )  # re-construct ⇒ __post_init__ re-validates
