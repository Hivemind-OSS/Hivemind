"""The change-outcome evidence feed — pure domain: parse, derive, join, render, serve.

A signed census receipt (a DSSE envelope over an in-toto Statement v1) reports what a
verifier could prove about ONE change (base_sha → head_sha). This module turns it into
ids-only ledger rows: parse + validate the envelope shape (D8), derive the verdict/tag
SERVER-SIDE (the caller can never assert them — the INV-2 analog; the post-merge canary
rule is structurally unconstructable to violate), join the receipt's touched
``path::Symbol`` subjects against episode anchors (deterministic, precision-first), and
render ONE canonical-JSON payload per matched episode (byte-stable — the idempotency key).

Named safe directions (Law 6): receipt-global malformation REFUSES loudly
(``ReceiptRefused`` — no row is ever written from a receipt this module cannot vouch
for); a malformed individual line is SKIPPED AND COUNTED (under-claim — one bad line
never aborts the batch); an unmatchable anchor simply never matches. Signature
verification is deliberately NOT performed here — no crypto dependency enters the
kernel; the envelope keyid is surfaced in the report and authenticity rides the
operator channel.

Trust-untouched by construction (O7): this module holds NO trust handle — it sees two
narrow ports (an anchored-episode read and an append-only evidence write) and nothing
else, so the feed structurally cannot mutate any episode's standing.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

from hive.domain.evidence_kinds import EK_CHANGE_OUTCOME
from hive.domain.ports import AnchoredEpisodeReader, ChangeEvidenceAppender

# ── the receipt contract (version-bound; never guess a schema) ─────────────────
RECEIPT_PREDICATE_TYPE = "urn:hive-census:receipt:v0"
_PAYLOAD_TYPE = "application/vnd.in-toto+json"
_STATEMENT_TYPE = "https://in-toto.io/Statement/v1"

PAYLOAD_SCHEMA = "change_outcome/v1"      # versions the rendered evidence payload
CHANGE_ACTOR = "census"                   # the feeding organ (provenance rides the payload)

_PHASES = frozenset({"pre_merge", "post_merge"})
_VERDICTS = frozenset({"pass", "fail"})
_TAGS = frozenset({"machine-checked", "bounded-estimate", "unverified-judgment"})
_SIGNALS_MACHINE = frozenset({"randomized", "canary"})

_JOIN_CLASSES = frozenset({"existence", "contract", "regression"})
_EXECUTION_CLASSES = frozenset({"typecheck", "tests"})
_DECIDED_STATES = frozenset({"passed", "failed"})

# §3.4 boundary sets: where a path may start/end inside a free-text anchor.
_PRE_BOUNDARY = frozenset({" ", "(", "`", "'", '"'})           # or string start
_POST_BOUNDARY = frozenset({":", " ", ")", "`", "'", '"', "#"})  # or string end
_IDENT = re.compile(r"[A-Za-z_]\w*")


class ReceiptRefused(ValueError):
    """The loud-refusal error (D8): the message IS the reason; zero rows follow."""


# ── carriers (frozen; illegal states unconstructable — Law 2) ──────────────────


@dataclass(frozen=True, slots=True)
class ChangeOutcome:
    """The derived, SHA-bound outcome of one change — enums + hashes only, no prose."""
    base_sha: str
    head_sha: str
    receipt_sha256: str
    receipt_schema_version: str
    predicate_type: str
    phase: str                      # pre_merge | post_merge
    verdict: str                    # pass | fail
    tag: str                        # machine-checked | bounded-estimate | unverified-judgment
    signal: str = "none"            # randomized | canary | none
    hive_census_version: str = ""

    def __post_init__(self) -> None:
        if self.phase not in _PHASES:
            raise ValueError(f"unknown phase {self.phase!r}")
        if self.verdict not in _VERDICTS:
            raise ValueError(f"unknown verdict {self.verdict!r}")
        if self.tag not in _TAGS:
            raise ValueError(f"unknown tag {self.tag!r}")
        if self.signal not in _SIGNALS_MACHINE | {"none"}:
            raise ValueError(f"unknown signal {self.signal!r}")
        for name in ("base_sha", "head_sha", "receipt_sha256"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must be non-empty")
        # THE §6.2.5 CANARY RULE: a post-merge outcome is machine-checked ONLY with a
        # randomized/canary signal — the over-claim cannot be constructed.
        if (self.phase == "post_merge" and self.tag == "machine-checked"
                and self.signal not in _SIGNALS_MACHINE):
            raise ValueError(
                "post_merge machine-checked requires a randomized/canary signal")


@dataclass(frozen=True, slots=True)
class TouchedSubject:
    """One joinable ``path::Symbol`` subject of the receipt."""
    path: str
    symbol: str


@dataclass(frozen=True, slots=True)
class IngestReport:
    """The machine-readable result of one ingest (ids + counts + keyid, never prose)."""
    inserted: tuple[int, ...]
    already_recorded: int
    matched: int
    skipped_lines: int
    keyid: str


# ── parsing (D8: .get() + coerce everywhere; refuse the receipt, never crash) ──


def _canonical_bytes(doc: dict) -> bytes:
    """The census ``canonical_bytes`` convention — the one serialization the subject
    digest is computed over (sha256 of the canonical PREDICATE bytes)."""
    return json.dumps(doc, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def parse_receipt(envelope: object) -> tuple[dict, str]:
    """DSSE envelope dict → (in-toto statement dict, keyid). Every receipt-global
    malformation raises ``ReceiptRefused`` with the reason: non-dict, wrong
    payloadType, missing/invalid b64 payload, payload not a JSON object, wrong
    statement/predicate type, subject-digest mismatch (integrity), absent provenance
    SHAs. The keyid is surfaced verbatim (verification stays census-side)."""
    if not isinstance(envelope, dict):
        raise ReceiptRefused("envelope is not a JSON object")
    payload_type = envelope.get("payloadType")
    if payload_type != _PAYLOAD_TYPE:
        raise ReceiptRefused(
            f"unexpected payloadType {payload_type!r} — not a DSSE in-toto envelope")
    raw = envelope.get("payload")
    if not isinstance(raw, str) or not raw:
        raise ReceiptRefused("envelope carries no payload")
    try:
        payload = base64.b64decode(raw, validate=True)
    except ValueError as error:                    # binascii.Error is a ValueError
        raise ReceiptRefused(f"payload is not valid base64: {error}") from error
    try:
        statement = json.loads(payload)
    except ValueError as error:                    # JSONDecodeError/UnicodeDecodeError
        raise ReceiptRefused(f"payload is not b64-encoded JSON: {error}") from error
    if not isinstance(statement, dict):
        raise ReceiptRefused("payload is not a JSON object")
    if statement.get("_type") != _STATEMENT_TYPE:
        raise ReceiptRefused(f"payload is not an in-toto {_STATEMENT_TYPE} Statement")
    if statement.get("predicateType") != RECEIPT_PREDICATE_TYPE:
        raise ReceiptRefused(
            f"unknown predicateType {statement.get('predicateType')!r} "
            f"(expected {RECEIPT_PREDICATE_TYPE!r}) — never guess a schema")
    predicate = statement.get("predicate")
    if not isinstance(predicate, dict):
        raise ReceiptRefused("statement carries no predicate object")
    subjects = statement.get("subject")
    subject0 = subjects[0] if isinstance(subjects, list) and subjects else None
    digest = subject0.get("digest") if isinstance(subject0, dict) else None
    claimed = digest.get("sha256") if isinstance(digest, dict) else None
    recomputed = hashlib.sha256(_canonical_bytes(predicate)).hexdigest()
    if claimed != recomputed:
        raise ReceiptRefused(
            "subject digest does not match the embedded predicate — integrity refused")
    provenance = predicate.get("provenance")
    provenance = provenance if isinstance(provenance, dict) else {}
    for name in ("base_sha", "head_sha"):
        value = provenance.get(name)
        if not isinstance(value, str) or not value.strip():
            raise ReceiptRefused(
                f"provenance {name} absent — the outcome cannot be sha-bound")
    keyid = ""
    signatures = envelope.get("signatures")
    if isinstance(signatures, list) and signatures and isinstance(signatures[0], dict):
        raw_keyid = signatures[0].get("keyid")
        keyid = raw_keyid if isinstance(raw_keyid, str) else ""
    return statement, keyid


def touched_subjects(lines: list) -> tuple[list[TouchedSubject], int]:
    """The joinable ``path::Symbol`` subjects of existence/contract/regression lines,
    deduplicated in first-seen order. A malformed line dict (non-dict, missing or
    wrongly-typed class/subject) is skipped AND counted (the per-case fallback — one
    bad line never aborts the receipt). Summary subjects without ``::`` and non-join
    classes are valid lines that simply never join (not counted)."""
    subjects: list[TouchedSubject] = []
    seen: set[tuple[str, str]] = set()
    skipped = 0
    for line in lines if isinstance(lines, list) else []:
        if not isinstance(line, dict):
            skipped += 1
            continue
        cls, subject = line.get("class"), line.get("subject")
        if not isinstance(cls, str) or not isinstance(subject, str):
            skipped += 1
            continue
        if cls not in _JOIN_CLASSES or "::" not in subject:
            continue
        path, _, symbol = subject.partition("::")
        if not path or not symbol:
            continue                               # not a path::Symbol subject
        if (path, symbol) not in seen:
            seen.add((path, symbol))
            subjects.append(TouchedSubject(path=path, symbol=symbol))
    return subjects, skipped


# ── derivation (§3.3 — server-derived, never caller-asserted) ──────────────────


def derive_pre_merge(lines: list) -> tuple[str, str]:
    """(verdict, tag) from the decided execution lines: class ∈ {typecheck, tests}
    with state ∈ {passed, failed}. ``errored`` is "tooling broke", not "change
    failed" — never decided. Nothing decided ⇒ ``ReceiptRefused`` (an outcome row
    without an outcome is noise; recording not_run as an outcome would be the
    fail-open the verifier model exists to prevent). The tag reuses the receipt's
    tags verbatim: machine-checked iff EVERY decided line is machine-checked."""
    decided: list[tuple[str, str]] = []
    for line in lines if isinstance(lines, list) else []:
        if not isinstance(line, dict) or line.get("class") not in _EXECUTION_CLASSES:
            continue
        detail = line.get("detail")
        state = detail.get("state") if isinstance(detail, dict) else None
        if state not in _DECIDED_STATES:
            continue
        tag = line.get("tag")
        decided.append((state, tag if isinstance(tag, str) else ""))
    if not decided:
        raise ReceiptRefused("no decided execution evidence — nothing to record")
    verdict = "fail" if any(state == "failed" for state, _ in decided) else "pass"
    tag = ("machine-checked"
           if all(t == "machine-checked" for _, t in decided) else "bounded-estimate")
    return verdict, tag


def derive_post_merge_tag(signal: str) -> str:
    """The §6.2.5 rule: machine-checked IFF the signal is randomized/canary; any other
    claim about a post-merge outcome is unverified judgment."""
    return "machine-checked" if signal in _SIGNALS_MACHINE else "unverified-judgment"


# ── the change→episode join (§3.4 — deterministic, pure, precision-first) ─────


def _find_path_span(anchor: str, path: str) -> Optional[tuple[int, int]]:
    """The path gate: the first occurrence of ``path`` in ``anchor`` at a boundary
    (preceding char ∈ start/space/(/`/'/" and following ∈ end/:/space/)/`/'/"/#),
    else the ``anchor endswith /path`` arm. None ⇒ no path hit ⇒ no match."""
    if not path:
        return None
    start = 0
    while True:
        i = anchor.find(path, start)
        if i < 0:
            break
        j = i + len(path)
        pre_ok = i == 0 or anchor[i - 1] in _PRE_BOUNDARY
        post_ok = j == len(anchor) or anchor[j] in _POST_BOUNDARY
        if pre_ok and post_ok:
            return (i, j)
        start = i + 1
    if anchor.endswith("/" + path):
        return (len(anchor) - len(path), len(anchor))
    return None


def _names_symbol(anchor: str, symbol: str) -> bool:
    """True iff ``symbol`` occurs in ``anchor`` with identifier boundaries (the chars
    around it are non-[A-Za-z0-9_])."""
    if not symbol:
        return False
    start = 0
    while True:
        i = anchor.find(symbol, start)
        if i < 0:
            return False
        j = i + len(symbol)
        pre = anchor[i - 1] if i > 0 else ""
        post = anchor[j] if j < len(anchor) else ""
        pre_ident = bool(pre) and (pre.isalnum() or pre == "_")
        post_ident = bool(post) and (post.isalnum() or post == "_")
        if not pre_ident and not post_ident:
            return True
        start = i + 1


def match_anchors(subjects: Sequence[TouchedSubject],
                  episodes: Sequence[tuple[int, str]],
                  ) -> dict[int, tuple[TouchedSubject, str]]:
    """{episode_id: (subject, level)} — the §3.4 rule. Per episode, subjects are tried
    in sorted (path, symbol) order and the FIRST match wins (one row per episode per
    receipt: the outcome is per-change, not per-line). Path gate first; then the
    symbol tier: a residue (anchor minus the matched path span) containing any
    identifier token makes the anchor symbol-scoped — the subject's symbol must then
    ALSO be named (a symbol-scoped anchor naming a different symbol does NOT match);
    an identifier-free residue is file-scoped and the path hit alone matches. An
    empty/unparseable anchor simply never matches (under-claim, never a crash).
    Insertion order follows the episode input order (deterministic, order-stable)."""
    ordered = sorted(set(subjects), key=lambda s: (s.path, s.symbol))
    out: dict[int, tuple[TouchedSubject, str]] = {}
    for episode_id, anchor in episodes:
        if not isinstance(anchor, str) or not anchor:
            continue
        for subject in ordered:
            span = _find_path_span(anchor, subject.path)
            if span is None:
                continue
            residue = anchor[:span[0]] + anchor[span[1]:]
            if _IDENT.search(residue):
                if _names_symbol(anchor, subject.symbol):
                    out[int(episode_id)] = (subject, "symbol")
                    break
                continue                           # names a different symbol: no match
            out[int(episode_id)] = (subject, "file")
            break
    return out


# ── the canonical payload renderer (THE single owner of the idempotency bytes) ─


def render_payload(outcome: ChangeOutcome, subject: TouchedSubject, level: str) -> str:
    """Canonical JSON (sort_keys + tight separators) — byte-stable, so the store's
    content-keyed idempotency holds across re-ingests. Ids/enums/hashes/versions only:
    no memory text, no source code, no receipt prose (Law 4)."""
    return json.dumps({
        "schema": PAYLOAD_SCHEMA,
        "base_sha": outcome.base_sha,
        "head_sha": outcome.head_sha,
        "receipt_sha256": outcome.receipt_sha256,
        "receipt_schema_version": outcome.receipt_schema_version,
        "predicate_type": outcome.predicate_type,
        "phase": outcome.phase,
        "verdict": outcome.verdict,
        "tag": outcome.tag,
        "signal": outcome.signal,
        "matched": {"path": subject.path, "symbol": subject.symbol, "level": level},
        "hive_census_version": outcome.hive_census_version,
    }, sort_keys=True, separators=(",", ":"))


# ── the orchestrating service (ports in, ONE batch out) ────────────────────────


class ChangeEvidenceService:
    """parse → derive → join → render → ONE append_evidence batch. Holds NO trust
    handle — the two injected ports read anchors and append evidence, nothing else
    (O7-safe by construction, the hive_outcome idiom)."""

    def __init__(self, *, reader: AnchoredEpisodeReader,
                 appender: ChangeEvidenceAppender, now: Callable[[], int]) -> None:
        self._reader = reader
        self._appender = appender
        self._now = now

    def ingest(self, envelope: object, *, phase: str = "pre_merge",
               verdict: Optional[str] = None, signal: str = "none") -> IngestReport:
        """One receipt in, one atomic batch out. pre_merge derives the verdict from
        the receipt (a caller-passed verdict is IGNORED — never caller-asserted);
        post_merge requires the operator's verdict and derives the tag from the
        signal. Zero matches ⇒ a report only (the store is never touched)."""
        statement, keyid = parse_receipt(envelope)
        predicate = statement["predicate"]           # shape vouched by parse_receipt
        provenance = predicate.get("provenance") or {}
        lines = predicate.get("lines")
        lines = lines if isinstance(lines, list) else []
        if phase == "pre_merge":
            derived_verdict, tag = derive_pre_merge(lines)
        elif phase == "post_merge":
            if verdict not in _VERDICTS:
                raise ValueError(
                    "post_merge ingest requires an explicit verdict ('pass'|'fail')")
            derived_verdict, tag = verdict, derive_post_merge_tag(signal)
        else:
            raise ValueError(f"unknown phase {phase!r}")
        subjects, skipped_lines = touched_subjects(lines)
        outcome = ChangeOutcome(
            base_sha=str(provenance.get("base_sha")),
            head_sha=str(provenance.get("head_sha")),
            receipt_sha256=str(statement["subject"][0]["digest"]["sha256"]),
            receipt_schema_version=str(predicate.get("schema_version") or ""),
            predicate_type=str(statement.get("predicateType")),
            phase=phase, verdict=derived_verdict, tag=tag, signal=signal,
            hive_census_version=str(provenance.get("hive_census_version") or ""))
        matches = match_anchors(subjects, self._reader.anchored_episodes())
        ts = int(self._now())
        rows = [(episode_id, EK_CHANGE_OUTCOME, CHANGE_ACTOR, ts,
                 render_payload(outcome, subject, level))
                for episode_id, (subject, level) in matches.items()]
        inserted, already = self._appender.append_evidence(rows) if rows else ([], 0)
        return IngestReport(inserted=tuple(inserted), already_recorded=already,
                            matched=len(matches), skipped_lines=skipped_lines,
                            keyid=keyid)
