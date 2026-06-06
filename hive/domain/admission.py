"""AdmissionService — the one deep module that turns a proposed insight into a
recallable memory through a single irreversible-by-construction gauntlet (M05 §1):

  1. deterministic secret scan BEFORE any persistence (refuse/redact pre-stage),
  2. content-hash-deduped staging,
  3. embed + approve in the SAME call — the value vector is computed and the row is
     flipped to ``status='approved'`` (the only recallable state) before ``write``
     returns.

CLIENT-GATED capture (HOOK-RELOCATION-PLAN v3): the server-side pending→approve QUEUE
was removed. A write is approved by a human in native chat BEFORE the tool call; the
caller passes that approver as ``approved_by`` and the server records it. There is no
``list_pending`` / ``approve`` / ``reject`` surface — the ONE non-bypassable gate that
remains is the deterministic secret scan (refuse is fail-closed, never optional).

PURE domain: depends only on injected ports (SecretScanner, EpisodeStore,
EmbeddingProvider) + an injected ``now`` clock. Imports no sqlite/torch/os/time —
the purity gate enforces it. Admission is deliberately NOT swappable (the approved-only
recall boundary is the one structural guarantee); only the SecretScanner under it is a
swap seam.

Secret-safety: on REFUSE nothing is written (0 rows, 0 blobs) and ``SecretRefused``
carries only rule names; on REDACT only the masked text is stored; no log line ever
contains the secret text (every field logged is a label/count/id) — §6.1 #5a/#5b.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Optional

from hive.domain.errors import SecretRefused
from hive.domain.models import content_hash
from hive.domain.secret_scan import REDACT, REFUSE, ScanVerdict

_log = logging.getLogger("hive.admission")


@dataclass(frozen=True, slots=True)
class WriteResult:
    """The result of a direct (client-gated) write. ``status`` ∈ {approved, redacted}
    — BOTH are approved + recallable; ``redacted`` additionally signals the secret scan
    masked one or more spans before storing. The domain NEVER returns ``refused`` (refuse
    RAISES SecretRefused, nothing written); the MCP layer maps that exception to a
    ``refused`` JSON envelope (P1.11)."""
    status: str
    episode_id: Optional[int]
    content_hash: Optional[str]  # hex; sha256(post-redaction stored text)
    scan: ScanVerdict
    deduped: bool = False


class AdmissionService:
    """Drives the M03 Store + M02 Index ports and the SecretScanner port. Owns the
    admission POLICY (non-swappable); owns no tables."""

    def __init__(self, store, scanner, embedder, *, now: Callable[[], int]) -> None:
        self._store = store              # EpisodeStore (+ its warm VectorIndex)
        self._scanner = scanner          # SecretScanner
        self._embedder = embedder        # EmbeddingProvider (text -> value[d])
        self._now = now

    # ── write: scan → (refuse 0-rows | redact-mask) → stage → embed → approve ──
    def write(self, text: str, *, approved_by: str, proposed_by: str,
              weight: float = 1.0, source: Optional[str] = None, tags: str = "",
              request_id: str = "-") -> WriteResult:
        """Capture a human-approved insight in one call. ``approved_by`` is the
        principal that approved this write in native chat (client-gated trust);
        ``proposed_by`` is the agent that proposed it — both are recorded. REFUSE raises
        SecretRefused (nothing written); CLEAN/REDACT stage → embed → approve and return
        an approved, recallable memory. // O(1) DB ops + one embed."""
        verdict = self._scanner.scan(text)
        if verdict.action == REFUSE:
            # rule NAMES + counts only — never the matched bytes (the finding has none)
            _log.warning("admission.refused", extra={
                "event": "admission.refused", "rules": [f.rule for f in verdict.findings],
                "n_findings": len(verdict.findings), "proposed_by": proposed_by,
                "approved_by": approved_by, "text_len": len(text), "request_id": request_id})
            raise SecretRefused(
                f"refused: credential detected ({len(verdict.findings)} finding(s), "
                f"rules={[f.rule for f in verdict.findings]})",
                rules=[f.rule for f in verdict.findings],
                n_findings=len(verdict.findings))

        staged_text = verdict.redacted_text if verdict.action == REDACT else text
        status = "redacted" if verdict.action == REDACT else "approved"
        if verdict.action == REDACT:
            _log.info("admission.redacted", extra={
                "event": "admission.redacted", "rules": [f.rule for f in verdict.findings],
                "n_spans": len(verdict.findings), "proposed_by": proposed_by,
                "request_id": request_id})

        # stage the (post-redaction) row + blob; dedup by content_hash.
        try:
            eid, deduped = self._store.stage(
                text=staged_text, weight=weight, source=source or "", tags=tags,
                proposed_by=proposed_by, ts=self._now())
        except Exception:
            _log.error("admission.stage_fail", extra={
                "event": "admission.stage_fail", "proposed_by": proposed_by,
                "request_id": request_id}, exc_info=True)
            raise

        h = content_hash(staged_text)
        ep = self._store.get_episode(eid)
        if ep is not None and ep.status == "approved":
            # dedup hit on an ALREADY-approved memory: it is already recallable — no
            # re-embed, no re-approve (idempotent). Re-stamping approved_by would let a
            # later caller overwrite the original approver, so leave it untouched.
            _log.info("admission.dedup_approved", extra={
                "event": "admission.dedup_approved", "episode_id": eid,
                "content_hash": h, "proposed_by": proposed_by, "request_id": request_id})
            return WriteResult(status=status, episode_id=eid, content_hash=h,
                               scan=verdict, deduped=True)

        # embed (pure, no DB) then flip pending→approved + index, in store.approve's tx.
        try:
            value = self._embedder.encode(staged_text)
        except Exception:
            # leave NO pending leak: drop the row we just staged (best-effort) and re-raise.
            _log.error("admission.embed_fail", extra={
                "event": "admission.embed_fail", "episode_id": eid,
                "proposed_by": proposed_by, "request_id": request_id}, exc_info=True)
            try:
                self._store.reject(eid)
            except Exception:                            # cleanup is best-effort
                _log.error("admission.embed_fail_cleanup_failed", extra={
                    "event": "admission.embed_fail_cleanup_failed", "episode_id": eid,
                    "request_id": request_id}, exc_info=True)
            raise

        ok = self._store.approve(
            eid, approved_by, value,
            expected_version=ep.version if ep is not None else 0,
            approved_ts=self._now())
        if not ok:
            # On a FRESH stage the CAS version matches, so this is unreachable in the
            # single-writer path; a False here means a lost-update race left the row
            # NOT approved. Fail LOUD rather than return a silently-pending (non-recallable)
            # row the caller believes is approved — and drop the dangling row.
            _log.error("admission.approve_failed", extra={
                "event": "admission.approve_failed", "episode_id": eid,
                "approved_by": approved_by, "request_id": request_id})
            try:
                self._store.reject(eid)
            except Exception:
                _log.error("admission.approve_failed_cleanup_failed", extra={
                    "event": "admission.approve_failed_cleanup_failed",
                    "episode_id": eid, "request_id": request_id}, exc_info=True)
            raise RuntimeError(
                f"admission approve failed for episode {eid} (lost-update CAS race)")

        _log.info("admission.captured", extra={
            "event": "admission.captured", "episode_id": eid, "content_hash": h,
            "deduped": deduped, "status": status, "proposed_by": proposed_by,
            "approved_by": approved_by, "request_id": request_id})
        return WriteResult(status=status, episode_id=eid, content_hash=h,
                           scan=verdict, deduped=deduped)
