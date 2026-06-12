"""Deterministic credential scan — the one always-on substrate floor.

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
catch-all for generic high-entropy tokens (prefix-less hex/base64 secrets).

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
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b")),
    ("pem_private_key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
    ("connection_string",
     re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s:/@]+:[^\s:/@]+@[^\s/]+")),
)
# A run of credential-charset bytes (base64 / url-safe). The entropy gate looks
# at these tokens only — punctuation/whitespace are natural token boundaries.
_TOKEN_CHARS = r"[A-Za-z0-9+/=_\-]"


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


def scan(text: str, *, mode: str = REFUSE,
         entropy_min_len: int = DEFAULT_ENTROPY_MIN_LEN,
         entropy_bits_floor: float = DEFAULT_ENTROPY_BITS_FLOOR) -> ScanVerdict:
    """Deterministic credential scan.  // O(n·r) time (r = rule count), O(f) space.

    Runs named rules first, then the entropy catch-all on any high-entropy token
    not already covered by a named span. No findings ⇒ ``clean``. Findings ⇒
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
            if token_entropy_bits(m.group()) > entropy_bits_floor:
                findings.append(SecretFinding(rule="entropy", span=span))

    if not findings:
        return ScanVerdict(action=CLEAN, redacted_text=None, findings=())
    findings_t = tuple(findings)
    if mode == REDACT:
        return ScanVerdict(action=REDACT, redacted_text=_mask(text, findings_t),
                           findings=findings_t)
    return ScanVerdict(action=REFUSE, redacted_text=None, findings=findings_t)
