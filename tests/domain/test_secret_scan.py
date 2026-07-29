"""P1.4 — M05 secret floor: the named-credential pattern set + Shannon-entropy
catch-all, and the ScanVerdict-cannot-lie invariants. The single most
security-critical domain unit: a credential must never survive the scan into a
staged row/blob/log.

Each per-rule test asserts the SPECIFIC rule fired (not merely action==refuse) — so
deleting that one regex turns the test red even though the entropy catch-all would
still refuse the token (the per-family mutation coverage the design review demanded).
"""

from __future__ import annotations

import hashlib

import pytest

from hive.domain.secret_scan import (
    CLEAN,
    REDACT,
    REFUSE,
    ScanVerdict,
    SecretFinding,
    is_identifier_shaped,
    scan,
    token_entropy_bits,
)


def _rules(v: ScanVerdict) -> set[str]:
    return {f.rule for f in v.findings}


# ── the two corpora: every row is a real string from this codebase / its memory
#    store (clean) or a SYNTHETIC credential (refused). This is the regression
#    baseline BUG-018 is judged against — the structural exclusion must close the
#    false-positive side WITHOUT opening the miss side. ─────────────────────────
MUST_NOT_FLAG = [
    # the live refusal that took the floor offline: 77 chars, H=4.07
    (
        "config_field_list",
        "interval_s/webhook_secret/mirror_dir/drift_per_tick/backfill_per_tick/workers",
    ),
    (
        "hook_path_mint_fp",
        ".claude/hooks/mint_fp.py",
    ),  # BUG-018's original case, H≈4.25
    (
        "hook_path_stop_capture",
        ".claude/hooks/stop-capture.py",
    ),  # H≈3.9, passed before too
    ("module_path", "hive/tools/entrypoint.py"),
    ("anchor_class", "hive/app/config.py::SyncConfig"),
    ("anchor_function", "hive/app/sync.py::mirror_dirname"),
    ("anchor_method", "hive/adapters/store_sqlite.py::repo_remove"),
    ("git_sha_40", "86011691f602670f562dda9fe345e2d9c4158128"),
    ("remote_url", "https://github.com/Hivemind-OSS/Hivemind.git"),
    ("env_var_sync", "HIVE_SYNC__DRIFT_PER_TICK"),
    ("env_var_scan", "HIVE_SECRET_SCAN__ENABLED"),
    (
        "env_assignment",
        "HIVE_SECRET_SCAN__ENABLED=false",
    ),  # `=` is an identifier joiner
    ("verdict_blast_radius", "blast_radius_changed"),
    ("verdict_anchor_changed", "anchor_changed"),
    ("signal_winner_near_dup", "winner_near_dup"),
]

MUST_FLAG = [
    ("github_pat", "ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8", "github_pat"),
    ("aws_akia", "AKIAIOSFODNN7EXAMPLE", "aws_akia"),
    (
        "anthropic_key",
        "sk-ant-api03-Xy9Kd0Lm2Np4Qr6St8Uv1Wx3Yz5Ab7Cd9Ef1Gh3Ij5Kl7Mn9",
        "openai_key",
    ),
    # Deliberately NOT shaped like a real Slack token (no `-<digits>-<digits>-<alnum>`
    # body): GitHub push protection blocks a fixture that mimics the real structure, so a
    # realistic-looking value here makes the repo unpushable. It still exercises the rule —
    # that is all the prefix regex reads.
    (
        "slack_token",
        "xoxb-EXAMPLEFIXTUREONLYNOTREAL",
        "slack_token",
    ),
    # H≈3.98 — the standing proof the floor must not move; the PREFIX catches it
    (
        "pypi_macaroon",
        "pypi-AgEIcHlwaS5vcmcCJDk0Y2FhZDQyLTk3ZDMtNGY2Yi1hOGY0LTZk",
        "pypi_token",
    ),
    ("pem_header", "-----BEGIN RSA PRIVATE KEY-----", "pem_private_key"),
    # the exact shape this system writes into a mirror's .git/config
    (
        "mirror_remote",
        "https://x-access-token:ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8@github.com/o/r.git",
        "connection_string",
    ),
    # NO named prefix — ONLY the entropy leg catches this, so weakening it leaks
    ("ngrok_prefixless", "2mK9pQ7xZ4vL8nR3wT6yB1cF5hJ0dG2sA9eU4iO7kM3qX8zV", "entropy"),
    # an AWS SECRET access key: 2 `/` segments, but case-MIXED ⇒ no path exemption
    ("aws_secret_key", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "entropy"),
]


@pytest.mark.parametrize("case,text", MUST_NOT_FLAG, ids=[c for c, _ in MUST_NOT_FLAG])
def test_legitimate_memory_text_is_clean(case: str, text: str):
    # bare AND embedded in prose (the real write shape) — both must be admitted
    assert scan(text).action == CLEAN, case
    assert scan(f"gotcha: the fix lives at {text} — see the note").action == CLEAN, case


@pytest.mark.parametrize("case,text,rule", MUST_FLAG, ids=[c for c, _, _ in MUST_FLAG])
def test_credential_is_refused(case: str, text: str, rule: str):
    for probe in (text, f"deploy note: {text} do not leak"):
        v = scan(probe)
        assert v.action == REFUSE, case
        assert rule in _rules(v), case


# ── the structural exclusion itself (the BUG-018 mechanism) ───────────────────
def test_identifier_shape_requires_two_word_segments():
    # multi-segment word runs: paths, snake_case, SCREAMING_SNAKE, TitleCase, digits
    for ident in (
        "claude/hooks/mint_fp",
        "interval_s/webhook_secret/mirror_dir",
        "HIVE_SYNC__DRIFT_PER_TICK",
        "com/Hivemind-OSS/Hivemind",
        "hive/app/v2/config_2026",
    ):
        assert is_identifier_shaped(ident), ident
    # mutation #1: a SINGLE-segment run is never exempt, whatever its entropy —
    # this is what keeps prefix-less blobs (and base32/hex) on the entropy path
    for bare in (
        "2mK9pQ7xZ4vL8nR3wT6yB1cF5hJ0dG2sA9eU4iO7kM3qX8zV",
        "0123456789abcdefghij",
    ):
        assert not is_identifier_shaped(bare), bare


def test_identifier_shape_rejects_one_bad_segment():
    # mutation #2: ONE non-word segment keeps the WHOLE run on the entropy path
    assert not is_identifier_shaped(
        "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    )  # case-mixed
    assert not is_identifier_shaped("hooks/mint/" + "a" * 21)  # over-long segment
    assert not is_identifier_shaped("abc+defghij_klmnopqrst")  # `+` is not a joiner
    # …while the same run with every segment word-shaped IS exempt (the contrast)
    assert is_identifier_shaped("hooks/mint/" + "a" * 20)


def test_entropy_leg_still_fires_on_prefixless_blobs():
    # the exclusion narrows the entropy leg by SHAPE, never by threshold: single-case
    # blobs, base64 padding and `+`-bearing runs are all still refused
    for blob in (
        "2mk9pq7xz4vl8nr3wt6yb1cf5hj0dg2sa9eu4io7km3qx8zv",  # all lower
        "2MK9PQ7XZ4VL8NR3WT6YB1CF5HJ0DG2SA9EU4IO7KM3QX8ZV",  # all upper
        "YWJjZGVmZ2hpamtsbW5vcHFyc3R1dnd4eXo=",  # base64, terminal padding
        "2mK9pQ7xZ4vL/8nR3wT6yB1cF/5hJ0dG2sA9eU4",  # blob split by `/`
    ):
        assert scan(f"token {blob} end").action == REFUSE, blob


def test_identifier_shaped_residual_is_deliberately_clean():
    # The NAMED residual this exclusion buys: a credential whose every segment is a
    # short single-case word is exempted along with the paths it admits. Accepted
    # because a random run has case-mixed or over-long segments with overwhelming
    # probability, and no single-segment run is ever exempt. If you close it, update
    # the secret_scan residual note and flip this expectation deliberately.
    assert scan("token abcdefghij_klmnopqrst end").action == CLEAN


# ── one test per named-rule family (delete-the-regex ⟹ this test red) ─────────
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
    jwt = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        ".eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvZSJ9"
        ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    )
    v = scan(f"auth bearer {jwt}")
    assert v.action == REFUSE and "jwt" in _rules(v)


def test_pem_refused():
    v = scan(
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKC\n-----END RSA PRIVATE KEY-----"
    )
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
    # gap (out-of-scope for the secret floor), NOT an oversight — if you add a detector, update
    # the secret_scan residual note and flip these expectations deliberately.
    for benign in (
        "--------------------",
        "====================",
        "0b1010101010101010",
        "XXXXXXXXXXXXXXXXXXXX",
    ):
        assert scan(f"note {benign} end").action == CLEAN, benign


# ── entropy catch-all: the boundary pair around bits_floor=4.0 at min_len=20 ──
def test_entropy_boundary_pair():
    # below: 14-symbol alphabet padded to 20 → H≈3.72 bits/char (< 4.0) ⟹ clean
    low = "0123456789abcd012345"
    # above: 20 distinct chars → H=log2(20)≈4.32 bits/char (> 4.0) ⟹ refuse
    high = "0123456789abcdefghij"
    assert len(low) == 20 and len(high) == 20
    assert token_entropy_bits(low) < 4.0 < token_entropy_bits(high)  # genuine straddle
    assert scan(f"id {low} end").action == CLEAN
    v = scan(f"id {high} end")
    assert v.action == REFUSE and "entropy" in _rules(v)


def test_entropy_ignores_short_and_low_entropy_prose():
    # normal prose tokens are short and/or low-entropy ⟹ never refused
    assert scan("the database connection pool was exhausted under load").action == CLEAN
    # a 40-char hex git SHA sits at H≈4.0 (16-symbol alphabet) and must PASS (git corpus)
    assert (
        scan("fixed in commit deadbeefcafe1234567890abcdef0123456789ab").action == CLEAN
    )


# ── ScanVerdict cannot lie (frozen __post_init__) ─────────────────────────────
def test_verdict_cannot_lie():
    with pytest.raises(ValueError):
        ScanVerdict(
            action=CLEAN, redacted_text=None, findings=(SecretFinding("x", (0, 1)),)
        )
    with pytest.raises(ValueError):
        ScanVerdict(
            action=REDACT, redacted_text=None, findings=(SecretFinding("x", (0, 1)),)
        )
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
    assert secret not in v.redacted_text  # raw secret gone
    assert "[REDACTED]" in v.redacted_text
    h_orig = hashlib.sha256(text.encode()).hexdigest()
    h_red = hashlib.sha256(v.redacted_text.encode()).hexdigest()
    assert h_red != h_orig  # dedup keys on redacted text


def test_clean_text_is_clean_no_findings():
    v = scan("the retry backoff should be exponential with jitter")
    assert v.action == CLEAN and v.findings == () and v.redacted_text is None
