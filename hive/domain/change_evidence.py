"""The change-outcome evidence feed — pure domain: parse, derive, join, render, serve.

A census receipt (an unsigned, DSSE-shaped envelope over an in-toto Statement v1) reports
what a verifier could prove about ONE change (base_sha → head_sha). This module turns it into
ids-only ledger rows: parse + validate the envelope shape (D8), derive the verdict/tag
SERVER-SIDE (the caller can never assert them — the INV-2 analog; the post-merge canary
rule is structurally unconstructable to violate), join the receipt's touched
``path::Symbol`` subjects against episode anchors (deterministic, precision-first), and
render ONE canonical-JSON payload per matched episode (byte-stable — the idempotency key).

The same batch also carries the verified-outcome rider (Flow A): for each matched
episode — pre-merge only, and only under a full ``ModelVersion ⊕ VerifierVersion ⊕ SHA``
version stamp — a mechanical ``outcome_verified_helped``/``_hurt`` row when the
polarity-aware three-way regression clause decides (drift at the anchor ∧ a DECIDED
failing test run ∧ blast-radius reach; abstention is the default), plus a
``verify_current``/``verify_stale`` row recording what the verifier proved about the
anchor itself at head SHA — and, when the receipt carries the optional census
``propagation`` block, an advisory ``stale_suspect`` row per episode whose anchor sits
in a breaking/removed seed's blast radius (neighbourhood suspicion; a directly-matched
episode's point row dominates and suppresses it).

Named safe directions (Law 6): receipt-global malformation REFUSES loudly
(``ReceiptRefused`` — no row is ever written from a receipt this module cannot vouch
for); a malformed individual line is SKIPPED AND COUNTED (under-claim — one bad line
never aborts the batch); an unmatchable anchor simply never matches. The receipt is
unsigned by policy (census emits no signature), so there is no signature to verify and no
crypto dependency enters the kernel; authenticity rides the transport/auth channel
(loopback or per-seat token) while integrity rides the subject digest, which this module
re-checks against the embedded predicate.

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

from hive.domain.evidence_kinds import (
    EK_CHANGE_OUTCOME, EK_OUTCOME_VERIFIED_HELPED, EK_OUTCOME_VERIFIED_HURT,
    EK_STALE_SUSPECT, EK_VERIFY_CURRENT, EK_VERIFY_STALE,
)
from hive.domain.ports import AnchoredEpisodeReader, ChangeEvidenceAppender

# ── the receipt contract (version-bound; never guess a schema) ─────────────────
RECEIPT_PREDICATE_TYPE = "urn:hive-census:receipt:v0"
_PAYLOAD_TYPE = "application/vnd.in-toto+json"
_STATEMENT_TYPE = "https://in-toto.io/Statement/v1"

PAYLOAD_SCHEMA = "change_outcome/v1"      # versions the rendered evidence payload
VERIFIED_PAYLOAD_SCHEMA = "outcome_verified/v1"   # versions the verified-outcome payload
VERIFY_PAYLOAD_SCHEMA = "verify/v1"       # versions the anchor-verification payload
SUSPECT_PAYLOAD_SCHEMA = "stale_suspect/v1"       # versions the neighbourhood-staleness payload
CHANGE_ACTOR = "census"                   # the feeding organ (provenance rides the payload)

_PHASES = frozenset({"pre_merge", "post_merge"})
_VERDICTS = frozenset({"pass", "fail"})
_TAGS = frozenset({"machine-checked", "bounded-estimate", "unverified-judgment"})
_SIGNALS_MACHINE = frozenset({"randomized", "canary"})

_JOIN_CLASSES = frozenset({"existence", "contract", "regression"})
_EXECUTION_CLASSES = frozenset({"typecheck", "tests"})
_DECIDED_STATES = frozenset({"passed", "failed"})

# the verified/verify drift vocabularies (VISION_LIFT §5.2, pinned exactly):
# breaking/removed prove the anchor moved; unchanged/additive/"" prove it held.
# Anything else (indeterminate, unknown) is an under-claim — no row.
_BREAKING_DRIFTS = frozenset({"breaking", "removed"})
_CURRENT_DRIFTS = frozenset({"unchanged", "additive", ""})

# the machine reason enum riding the verified payload (never receipt prose)
_VERIFIED_REASONS = {EK_OUTCOME_VERIFIED_HELPED: "corroborated",
                     EK_OUTCOME_VERIFIED_HURT: "contradicted"}

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
class SubjectEvidence:
    """The per-(path, symbol) distillation of a receipt's evidence lines (D8: every
    field defaults to the under-claiming absent value, so a malformed line can only
    LOSE evidence, never fabricate it). ``drift`` is the contract line's verdict;
    ``exists_after`` the existence line's; ``regression_tests`` the regression line's
    blast-radius test reach (``has_regression_line`` distinguishes an absent line
    from an empty reach)."""
    drift: str = ""                          # contract line detail.drift ("" when absent)
    exists_after: Optional[bool] = None      # existence line detail.exists_after
    regression_tests: tuple[str, ...] = ()   # regression line detail.tests
    has_regression_line: bool = False


@dataclass(frozen=True, slots=True)
class IngestReport:
    """The machine-readable result of one ingest (ids + counts, never prose).
    The four verified/verify counters and ``stale_suspects`` count rows RENDERED into
    the batch (mirroring ``matched``'s join-level semantics) — a re-ingest keeps the
    counts while ``already_recorded`` absorbs the skips."""
    inserted: tuple[int, ...]
    already_recorded: int
    matched: int
    skipped_lines: int
    verified_helped: int = 0
    verified_hurt: int = 0
    verify_current: int = 0
    verify_stale: int = 0
    stale_suspects: int = 0


# ── parsing (D8: .get() + coerce everywhere; refuse the receipt, never crash) ──


def _canonical_bytes(doc: dict) -> bytes:
    """The census ``canonical_bytes`` convention — the one serialization the subject
    digest is computed over (sha256 of the canonical PREDICATE bytes)."""
    return json.dumps(doc, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def parse_receipt(envelope: object) -> dict:
    """DSSE-shaped envelope dict → in-toto statement dict. Every receipt-global
    malformation raises ``ReceiptRefused`` with the reason: non-dict, wrong
    payloadType, missing/invalid b64 payload, payload not a JSON object, wrong
    statement/predicate type, subject-digest mismatch (integrity), absent provenance
    SHAs. The envelope's signatures are IGNORED — the receipt is unsigned by policy and
    authenticity is not a receipt signature; integrity is the subject-digest re-check."""
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
    return statement


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


def propagation_subjects(predicate: object
                         ) -> tuple[list[tuple[TouchedSubject, str, str]], int]:
    """The ``(neighbour subject, seed, drift)`` triples of the receipt's OPTIONAL
    ``propagation`` block (T2 — census ``--propagate`` blast-radius expansion), in
    block order, deduplicated on the exact triple. D8 at the write boundary: an
    absent / non-list block is simply empty (byte-inert); a malformed ENTRY — non-dict,
    non-str seed, drift outside breaking/removed (the only drifts that justify
    suspicion), neighbors not a list — is skipped AND counted; an individual neighbour
    that is not a ``path::Symbol`` string is a benign skip (mirrors
    ``touched_subjects``' non-counting for ``::``-less subjects)."""
    if not isinstance(predicate, dict):
        return [], 0
    block = predicate.get("propagation")
    if not isinstance(block, list):
        return [], 0
    triples: list[tuple[TouchedSubject, str, str]] = []
    seen: set[tuple[TouchedSubject, str, str]] = set()
    skipped = 0
    for entry in block:
        if not isinstance(entry, dict):
            skipped += 1
            continue
        seed, drift = entry.get("seed"), entry.get("drift")
        neighbors = entry.get("neighbors")
        if (not isinstance(seed, str) or not seed
                or drift not in _BREAKING_DRIFTS
                or not isinstance(neighbors, list)):
            skipped += 1
            continue
        for neighbor in neighbors:
            if not isinstance(neighbor, str) or "::" not in neighbor:
                continue
            path, _, symbol = neighbor.partition("::")
            if not path or not symbol:
                continue
            triple = (TouchedSubject(path=path, symbol=symbol), seed, drift)
            if triple not in seen:
                seen.add(triple)
                triples.append(triple)
    return triples, skipped


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


# ── Flow A: the verified/verify distillation + classifiers (mechanical, D8) ────


def subject_evidence(lines: list) -> dict[tuple[str, str], SubjectEvidence]:
    """Distill the evidence lines into one ``SubjectEvidence`` per ``path::Symbol``.
    Total and defensive: a malformed line/detail/field is simply DROPPED (the
    under-claim direction — the defaults assert nothing), never a raise. A field
    stated by more than one line is last-write-wins over the parseable statements
    (receipts state each once; the rule only needs determinism)."""
    acc: dict[tuple[str, str], dict] = {}
    for line in lines if isinstance(lines, list) else []:
        if not isinstance(line, dict):
            continue
        cls, subject = line.get("class"), line.get("subject")
        if cls not in _JOIN_CLASSES or not isinstance(subject, str) or "::" not in subject:
            continue
        path, _, symbol = subject.partition("::")
        if not path or not symbol:
            continue
        detail = line.get("detail")
        detail = detail if isinstance(detail, dict) else {}
        slot = acc.setdefault((path, symbol), {})
        if cls == "contract":
            drift = detail.get("drift")
            if isinstance(drift, str):
                slot["drift"] = drift
        elif cls == "existence":
            exists_after = detail.get("exists_after")
            if isinstance(exists_after, bool):
                slot["exists_after"] = exists_after
        elif cls == "regression":
            slot["has_regression_line"] = True
            tests = detail.get("tests")
            if isinstance(tests, list):
                slot["regression_tests"] = tuple(
                    t for t in tests if isinstance(t, str))
    return {key: SubjectEvidence(**fields) for key, fields in acc.items()}


def decided_tests_state(lines: list) -> Optional[str]:
    """The DECIDED state of the ``tests`` execution line(s): ``"failed"`` if any
    decided run failed, ``"passed"`` if every decided run passed, ``None`` when
    nothing is decided (not_run / errored / absent — "tooling broke" is never
    "change failed"). Total, never raises."""
    states: list[str] = []
    for line in lines if isinstance(lines, list) else []:
        if not isinstance(line, dict) or line.get("class") != "tests":
            continue
        detail = line.get("detail")
        state = detail.get("state") if isinstance(detail, dict) else None
        if state in _DECIDED_STATES:
            states.append(state)
    if not states:
        return None
    return "failed" if "failed" in states else "passed"


def classify_verified(polarity: str, ev: SubjectEvidence,
                      tests_state: Optional[str]) -> Optional[str]:
    """The mechanical corroborated/contradicted join over the regression line
    (VISION_LIFT §5.2, pinned exactly). HELPED iff a ``dont`` memory's anchor drifted
    breaking/removed AND a decided-failing test run reached it (the three-way clause:
    drift at X ∧ a failing affected-test run ∧ tests reaching X). HURT iff a ``do``
    memory's anchor took a decided-failing run with reach. Everything else — neutral
    polarity, decided-pass, not_run/errored, no reach, no drift — is ``None``:
    fail-closed, abstention is the default; only a DECIDED failing run with
    blast-radius reach backwrites."""
    if tests_state != "failed" or not ev.regression_tests:
        return None
    if polarity == "dont" and ev.drift in _BREAKING_DRIFTS:
        return EK_OUTCOME_VERIFIED_HELPED
    if polarity == "do":
        return EK_OUTCOME_VERIFIED_HURT
    return None


def classify_verify(ev: SubjectEvidence) -> Optional[str]:
    """The anchor-verification classifier: STALE iff the symbol is gone
    (``exists_after is False``) or its contract drifted breaking/removed (gone trumps
    a benign drift reading); CURRENT iff it verifiably exists AND the drift is
    benign (unchanged/additive/absent). Indeterminate/unknown ⇒ ``None``
    (under-claim)."""
    if ev.exists_after is False or ev.drift in _BREAKING_DRIFTS:
        return EK_VERIFY_STALE
    if ev.exists_after is True and ev.drift in _CURRENT_DRIFTS:
        return EK_VERIFY_CURRENT
    return None


def version_stamp(provenance: object) -> Optional[dict]:
    """The full ``ModelVersion ⊕ VerifierVersion ⊕ SHA`` binding (L7): base/head SHAs
    + the combdrift block (flat string entries only — the VerifierVersion) + the
    matrix HEAD graph identity (graph_sha256/commit_sha/engine_version — the
    ModelVersion). ``None`` unless EVERY part parses (D8 ``.get()`` walks): a
    verified/verify row is never written without its full stamp — the plain
    change_outcome row still lands."""
    if not isinstance(provenance, dict):
        return None
    base_sha, head_sha = provenance.get("base_sha"), provenance.get("head_sha")
    if not (isinstance(base_sha, str) and base_sha.strip()
            and isinstance(head_sha, str) and head_sha.strip()):
        return None
    combdrift_raw = provenance.get("combdrift")
    if not isinstance(combdrift_raw, dict):
        return None
    combdrift = {k: v for k, v in combdrift_raw.items()
                 if isinstance(k, str) and isinstance(v, str)}
    if not combdrift:
        return None
    matrix = provenance.get("matrix")
    head_block = matrix.get("head") if isinstance(matrix, dict) else None
    if not isinstance(head_block, dict):
        return None
    matrix_head: dict[str, str] = {}
    for key in ("graph_sha256", "commit_sha", "engine_version"):
        value = head_block.get(key)
        if not (isinstance(value, str) and value.strip()):
            return None
        matrix_head[key] = value
    return {"base_sha": base_sha, "head_sha": head_sha,
            "combdrift": combdrift, "matrix_head": matrix_head}


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


def render_verified_payload(outcome: ChangeOutcome, subject: TouchedSubject,
                            level: str, kind_reason: str, stamp: dict) -> str:
    """The verified-outcome payload: canonical JSON, ids/enums/stamps only.
    ``kind_reason`` is the machine enum (corroborated/contradicted), never receipt
    prose; the full version ``stamp`` binds the row (L7)."""
    return json.dumps({
        "schema": VERIFIED_PAYLOAD_SCHEMA,
        "receipt_sha256": outcome.receipt_sha256,
        "phase": outcome.phase,
        "verdict": outcome.verdict,
        "tag": outcome.tag,
        "matched": {"path": subject.path, "symbol": subject.symbol, "level": level},
        "reason": kind_reason,
        "stamp": stamp,
    }, sort_keys=True, separators=(",", ":"))


def render_verify_payload(subject: TouchedSubject, ev: SubjectEvidence,
                          stamp: dict) -> str:
    """The anchor-verification payload: the observed existence/drift facts + the full
    version stamp. Deliberately receipt-digest-free — the verification state of an
    anchor at a head SHA is a fact about the CHANGE, so a re-issued receipt for the
    same change dedups to the same row (content-keyed idempotency)."""
    return json.dumps({
        "schema": VERIFY_PAYLOAD_SCHEMA,
        "matched": {"path": subject.path, "symbol": subject.symbol},
        "exists_after": ev.exists_after,
        "drift": ev.drift,
        "stamp": stamp,
    }, sort_keys=True, separators=(",", ":"))


def render_suspect_payload(subject: TouchedSubject, seed: str, drift: str,
                           stamp: dict) -> str:
    """The neighbourhood-staleness payload: the suspected neighbour + the
    breaking/removed seed whose blast radius reached it + the full version stamp
    (the SHAs ride the stamp). Ids/enums/hashes only — never receipt prose (Law 4);
    receipt-digest-free like the verify payload, so a re-issued receipt for the same
    change dedups to the same row (content-keyed idempotency)."""
    return json.dumps({
        "schema": SUSPECT_PAYLOAD_SCHEMA,
        "matched": {"path": subject.path, "symbol": subject.symbol},
        "seed": seed,
        "drift": drift,
        "stamp": stamp,
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
        statement = parse_receipt(envelope)
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
        anchored = self._reader.anchored_episodes()
        polarity_by_id = {int(eid): pol for eid, _anchor, pol in anchored}
        matches = match_anchors(
            subjects, [(eid, anchor) for eid, anchor, _pol in anchored])
        ts = int(self._now())
        # Flow A rider: pre-merge only, and ONLY under a full version stamp (L7) — a
        # post-merge assertion carries no per-subject machine evidence, and a
        # stamp-less receipt still lands its plain change_outcome rows unchanged.
        stamp = version_stamp(provenance) if phase == "pre_merge" else None
        ev_by_subject = subject_evidence(lines) if stamp is not None else {}
        tests_state = decided_tests_state(lines) if stamp is not None else None
        counts = {EK_OUTCOME_VERIFIED_HELPED: 0, EK_OUTCOME_VERIFIED_HURT: 0,
                  EK_VERIFY_CURRENT: 0, EK_VERIFY_STALE: 0}
        rows: list[tuple[int, str, str, int, str]] = []
        for episode_id, (subject, level) in matches.items():
            rows.append((episode_id, EK_CHANGE_OUTCOME, CHANGE_ACTOR, ts,
                         render_payload(outcome, subject, level)))
            if stamp is None:
                continue
            ev = ev_by_subject.get((subject.path, subject.symbol), SubjectEvidence())
            verified_kind = classify_verified(
                polarity_by_id.get(episode_id, "neutral"), ev, tests_state)
            if verified_kind is not None:
                rows.append((episode_id, verified_kind, CHANGE_ACTOR, ts,
                             render_verified_payload(
                                 outcome, subject, level,
                                 _VERIFIED_REASONS[verified_kind], stamp)))
                counts[verified_kind] += 1
            verify_kind = classify_verify(ev)
            if verify_kind is not None:
                rows.append((episode_id, verify_kind, CHANGE_ACTOR, ts,
                             render_verify_payload(subject, ev, stamp)))
                counts[verify_kind] += 1
        # T2 rider: graph-propagated staleness — join the receipt's OPTIONAL propagation
        # neighbours with the SAME anchor rule, in the same atomic batch. Stamp-gated like
        # the verified/verify rows (the payload carries the version stamp, so pre-merge
        # only); an episode already carrying a POINT row this ingest gets no suspect row
        # (point evidence dominates neighbourhood suspicion). Detection fuel only — the
        # worklist proposes re-verification, nothing here retires anything.
        stale_suspects = 0
        if stamp is not None:
            triples, prop_skipped = propagation_subjects(predicate)
            skipped_lines += prop_skipped
            if triples:
                seed_drift_by_subject: dict[TouchedSubject, tuple[str, str]] = {}
                for subject, seed, drift in triples:
                    seed_drift_by_subject.setdefault(subject, (seed, drift))
                neighbour_matches = match_anchors(
                    list(seed_drift_by_subject),
                    [(eid, anchor) for eid, anchor, _pol in anchored])
                for episode_id, (subject, _level) in neighbour_matches.items():
                    if episode_id in matches:
                        continue               # point evidence dominates suspicion
                    seed, drift = seed_drift_by_subject[subject]
                    rows.append((episode_id, EK_STALE_SUSPECT, CHANGE_ACTOR, ts,
                                 render_suspect_payload(subject, seed, drift, stamp)))
                    stale_suspects += 1
        inserted, already = self._appender.append_evidence(rows) if rows else ([], 0)
        return IngestReport(inserted=tuple(inserted), already_recorded=already,
                            matched=len(matches), skipped_lines=skipped_lines,
                            verified_helped=counts[EK_OUTCOME_VERIFIED_HELPED],
                            verified_hurt=counts[EK_OUTCOME_VERIFIED_HURT],
                            verify_current=counts[EK_VERIFY_CURRENT],
                            verify_stale=counts[EK_VERIFY_STALE],
                            stale_suspects=stale_suspects)
