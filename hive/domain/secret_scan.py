"""Deterministic credential scan — the default-on substrate floor.

This pure scan is ON by default and runs before any persistence. An operator may BYPASS it
with ``HIVE_SECRET_SCAN__ENABLED=false`` — but that toggle lives in the ``DefaultSecretScanner``
adapter (which returns CLEAN when off), never here: this module's logic is unchanged either way,
so it stays pure and the domain reacts to a verdict, never to a flag (Law 4).

PURE: stdlib ``re`` + ``math`` only (no I/O; the purity gate forbids
sqlite/torch/subprocess/os/git/time here). Side-effect-free and deterministic so
the same text always yields the same verdict — the SecretScanner port contract.

The scan runs BEFORE anything is persisted. A raw credential is refused (default,
fail-closed) or redacted; the substrate never stores the secret "the way a DB
rejects a malformed row". The verdict CANNOT lie: a clean-with-findings, a
redact-without-redacted_text, or a refuse-without-a-finding is unconstructable
(``__post_init__`` raises). A ``SecretFinding`` carries only ``rule`` + ``span``
(char offsets into the ORIGINAL text) — never the matched bytes — so there is no
"remember not to log the secret" rule: the finding has no secret to log.

Coverage: ``sk-`` / ``AKIA`` / ``ghp_`` / ``xox`` / ``pypi-`` / JWT / PEM /
connection-strings as named high-precision rules, plus a Shannon-entropy
catch-all for generic high-entropy tokens (prefix-less hex/base64 secrets) —
the catch-all narrowed by ONE structural exclusion, ``is_identifier_shaped``:
separator-joined identifier text (a path, a snake_case field list) is not a
credential run and never reaches the entropy test. Entropy measures symbol
DIVERSITY, and joining ordinary words with ``/`` or ``_`` manufactures diversity
without manufacturing a secret — the whole of BUG-018. The exclusion is
structural on purpose: the 4.0-bit floor is NOT movable, because real ``pypi-``
macaroons sit at H≈3.98 just under it (the named prefix, not a lower floor, is
how the miss side closes).

Named residual — LOW-ENTROPY tokens (adversarial audit wf_41f1e8af-590, accepted):
a token that is a repeated unit (``abababab…``, H=1.0), a repeated word
(``passwordpassword…``, H=2.75), or a limited-alphabet run (11 distinct, H≈3.46)
can sit below the 4.0 entropy floor and pass. These are NOT closed because (a) they
are not real credential FORMATS — a genuine secret is high-entropy by construction;
(b) in the natural ``key=value`` shape the ``=``/``:`` joins the token and breaks
periodicity, so naive repetition detection misses them anyway; (c) any detector
broad enough to catch them (period or distinct-cardinality) refuses common BENIGN
strings — ``----------`` / ``==========`` separators, ``0b10101010…`` bit masks,
``XXXX…`` placeholders — degrading the capture UX on a high-trust accidental-paste
floor; and (d) the human approval gate sees every staged write. This is the
"named, not solved" posture: the floor targets accidental high-entropy credential
pastes, not adversarial low-entropy evasion (explicitly out of scope).

Named residual — IDENTIFIER-SHAPED tokens (the price of closing BUG-018): a
credential whose every separator-delimited segment is a short single-case word
(``abcdefghij_klmnopqrst``) is exempted along with the paths it was written to
admit. Same "named, not solved" trade: a random credential run has case-mixed or
over-long segments with overwhelming probability, and every SINGLE-segment run —
hex, base32, the prefix-less ngrok shape — is still judged on entropy alone.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Optional

# ── action vocabulary (the locked ScanAction literals) ───────────────────────
CLEAN = "clean"
REDACT = "redact"
REFUSE = "refuse"
ScanAction = str  # Literal["clean","redact","refuse"] at the type boundary

# Config defaults (M-config secret_scan.*; entropy floor tuned so 16-symbol hex
# git SHAs sit at H≈4.0 and PASS while 64-symbol base64 secrets exceed it).
DEFAULT_ENTROPY_MIN_LEN = 20
DEFAULT_ENTROPY_BITS_FLOOR = 4.0
_REDACTION = "[REDACTED]"


@dataclass(frozen=True, slots=True)
class SecretFinding:
    """A fired rule. ``rule`` is a label (e.g. ``"aws_akia"``); ``span`` is the
    ``[start, end)`` char offset into the ORIGINAL text — NEVER the matched bytes."""

    rule: str
    span: tuple[int, int]


@dataclass(frozen=True, slots=True)
class ScanVerdict:
    """The frozen, can't-lie result of a pre-stage scan.

    ``action`` ∈ {clean, redact, refuse}. ``redacted_text`` is present iff
    ``action == redact`` (the masked payload safe to stage). ``findings`` names
    the rules that fired (never the secret value)."""

    action: ScanAction
    redacted_text: Optional[str]
    findings: tuple[SecretFinding, ...]

    def __post_init__(self) -> None:
        if self.action == CLEAN and self.findings:
            raise ValueError("clean verdict cannot carry findings")
        if self.action == REDACT and self.redacted_text is None:
            raise ValueError("redact verdict requires redacted_text")
        if self.action == REFUSE and not self.findings:
            raise ValueError("refuse verdict requires at least one finding")
        if self.action not in (CLEAN, REDACT, REFUSE):
            raise ValueError(f"bad scan action {self.action!r}")


# ── named high-precision rules. Each is one delete-able regex with a named
#    test (test_<rule>_refused) so a removed pattern turns that test red. ───────
_PATTERNS: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    ("aws_akia", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    # pypi- tokens sit at H≈3.98 (just under the 4.0 entropy floor) and would
    # otherwise scan CLEAN and persist raw (adversarial audit wf_41f1e8af-590).
    # The literal prefix is fail-safe regardless of the payload's entropy; {12,}
    # catches real macaroon tokens while skipping short `pypi-<word>` package refs.
    ("pypi_token", re.compile(r"\bpypi-[A-Za-z0-9_-]{12,}\b")),
    ("github_pat", re.compile(r"\bghp_[A-Za-z0-9]{20,}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b"),
    ),
    ("pem_private_key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
    (
        "connection_string",
        re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s:/@]+:[^\s:/@]+@[^\s/]+"),
    ),
)
# A run of credential-charset bytes (base64 / url-safe). The entropy gate looks
# at these tokens only — punctuation/whitespace are natural token boundaries.
_TOKEN_CHARS = r"[A-Za-z0-9+/=_\-]"

# ── the entropy leg's structural exclusion (BUG-018) ─────────────────────────
# The characters that JOIN identifier words in ordinary technical prose: path
# `/`, snake `_`, kebab `-`, assignment `=` (`HIVE_SECRET_SCAN__ENABLED=false`
# is one candidate token and refused at H=4.06 before this). `=` is safe to join
# on because base64 uses it as TERMINAL padding only — it can never split a blob
# into word-shaped pieces. `+` is deliberately EXCLUDED: it sits mid-string in
# standard base64, so admitting it would let a blob buy an exemption by splitting
# itself. A run carrying `+` therefore stays on the entropy path.
_IDENT_SEPARATOR = re.compile(r"[/_\-=]+")
# The longest a segment may be and still read as a WORD. Identifier words are
# short (the longest across this repo's own paths, env names, and verdict strings
# is 8 chars); a 40-char single-case run is a blob wearing a separator.
_MAX_WORD_SEGMENT = 20


def _is_word_segment(seg: str) -> bool:
    """Does one separator-delimited segment read as a WORD?  // O(n) time.

    Word-shaped ⇔ short, ASCII-alphanumeric, and free of INTERNAL case mixing:
    all-lower (``hooks``), all-upper (``DRIFT``), Capitalized (``Hivemind``), or
    digits only (``2026``, a version/date/index run). Case mixing WITHIN a
    segment (``wJalrXUtnFEMI``) is the signature of a random blob, so such a
    segment is never word-shaped — that is what keeps a slash-bearing AWS secret
    access key on the entropy path."""
    if not seg or len(seg) > _MAX_WORD_SEGMENT or not seg.isascii():
        return False
    if not seg.isalnum():  # `+` (base64) and any other non-alnum ⇒ not a word
        return False
    letters = [c for c in seg if c.isalpha()]
    if not letters:
        return True
    if letters[0].islower():
        return all(c.islower() for c in letters[1:])
    return all(c.isupper() for c in letters[1:]) or all(
        c.islower() for c in letters[1:]
    )


def is_identifier_shaped(token: str) -> bool:
    """Is ``token`` separator-joined IDENTIFIER text rather than a random run?
    // O(n) time, O(k) space (k = segment count).

    True ⇔ the token splits into ≥2 word-shaped segments on ``/``, ``_``, ``-``,
    ``=``.
    This is the entropy catch-all's structural exclusion, and it discriminates on
    SHAPE, never on the threshold — the 4.0-bit floor is unchanged, because
    lowering it would admit the ``pypi-`` macaroons that sit at H≈3.98.

    Why it exists: Shannon entropy scores symbol diversity, so slash-joining
    ordinary words manufactures a high-entropy run out of nothing secret —
    ``.claude/hooks/mint_fp.py`` (H≈4.25) and the config-field list
    ``interval_s/webhook_secret/mirror_dir/…`` (H≈4.07) both cleared a floor
    tuned for credentials and refused legitimate memories (BUG-018).

    Why it does not open the miss side: a SINGLE-segment run is never exempt
    (deleting the ``len(segments) < 2`` guard is mutation #1) — a 40-char hex SHA
    still passes on its own low entropy, an uppercase base32 secret and the
    prefix-less ``2mK9pQ7x…`` ngrok shape are still refused on theirs — and ONE
    case-mixed or over-long segment keeps the whole run on the entropy path, so
    ``wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY`` is still refused despite its two
    ``/`` separators (deleting the ``all(...)`` quantifier is mutation #2)."""
    segments = [s for s in _IDENT_SEPARATOR.split(token) if s]
    if len(segments) < 2:
        return False  # a bare run carries no identifier structure to read
    return all(_is_word_segment(s) for s in segments)


def token_entropy_bits(token: str) -> float:
    """Shannon entropy in bits per character of ``token``.  // O(n) time, O(k) space
    (k = distinct chars). A 2-symbol run → 1.0; uniform hex (16 symbols) → 4.0;
    20 distinct chars → log2(20) ≈ 4.32. The high-entropy signal."""
    if not token:
        return 0.0
    counts: dict[str, int] = {}
    for ch in token:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(token)
    h = 0.0
    for c in counts.values():
        p = c / n
        h -= p * math.log2(p)
    return h


def _overlaps(span: tuple[int, int], spans: list[tuple[int, int]]) -> bool:
    s, e = span
    return any(not (e <= a or s >= b) for a, b in spans)


def _mask(text: str, findings: tuple[SecretFinding, ...]) -> str:
    """Replace each finding span (merged) in the ORIGINAL text with the mask.  // O(n) time."""
    spans = sorted({f.span for f in findings})
    merged: list[list[int]] = []
    for s, e in spans:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    out: list[str] = []
    prev = 0
    for s, e in merged:
        out.append(text[prev:s])
        out.append(_REDACTION)
        prev = e
    out.append(text[prev:])
    return "".join(out)


def scan(
    text: str,
    *,
    mode: str = REFUSE,
    entropy_min_len: int = DEFAULT_ENTROPY_MIN_LEN,
    entropy_bits_floor: float = DEFAULT_ENTROPY_BITS_FLOOR,
) -> ScanVerdict:
    """Deterministic credential scan.  // O(n·r) time (r = rule count), O(f) space.

    Runs named rules first, then the entropy catch-all on any high-entropy token
    that is neither covered by a named span nor identifier-shaped (a path /
    snake_case run is structure, not a secret — BUG-018). No findings ⇒
    ``clean``. Findings ⇒
    ``refuse`` (default, fail-closed) or, when ``mode == "redact"``, ``redact``
    with the secret spans masked. The raw secret never appears in the verdict."""
    findings: list[SecretFinding] = []
    named_spans: list[tuple[int, int]] = []
    for rule, pat in _PATTERNS:
        for m in pat.finditer(text):
            findings.append(SecretFinding(rule=rule, span=(m.start(), m.end())))
            named_spans.append((m.start(), m.end()))

    if entropy_min_len > 0:
        token_re = re.compile(_TOKEN_CHARS + "{" + str(entropy_min_len) + ",}")
        for m in token_re.finditer(text):
            span = (m.start(), m.end())
            if _overlaps(span, named_spans):
                continue  # already flagged by a named rule — don't double-report
            if is_identifier_shaped(m.group()):
                continue  # separator-joined identifier text, not a credential run
            if token_entropy_bits(m.group()) > entropy_bits_floor:
                findings.append(SecretFinding(rule="entropy", span=span))

    if not findings:
        return ScanVerdict(action=CLEAN, redacted_text=None, findings=())
    findings_t = tuple(findings)
    if mode == REDACT:
        return ScanVerdict(
            action=REDACT, redacted_text=_mask(text, findings_t), findings=findings_t
        )
    return ScanVerdict(action=REFUSE, redacted_text=None, findings=findings_t)
