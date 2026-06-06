"""AdmissionService — the one deep module that turns a proposed insight into a
recallable memory through a single irreversible-by-construction gauntlet (M05 §1):

  1. deterministic secret scan BEFORE any persistence (refuse/redact pre-stage),
  2. content-hash-deduped PENDING staging (value NULL — structurally unrecallable),
  3. the SOLE pending→approved transition, the only path that computes the value
     vector and feeds the index.

PURE domain: depends only on injected ports (SecretScanner, EpisodeStore,
EmbeddingProvider) + an injected ``now`` clock. Imports no sqlite/torch/os/time —
the purity gate enforces it. Admission is deliberately NOT swappable (the
pending→approved + approved-only-recall boundary is the one structural guarantee);
only the SecretScanner under it is a swap seam.

Secret-safety: on REFUSE nothing is written (0 rows, 0 blobs) and ``SecretRefused``
carries only rule names; on REDACT only the masked text is staged; no log line ever
contains the secret text (every field logged is a label/count/id) — §6.1 #5a/#5b.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

from hive.domain.errors import SecretRefused
from hive.domain.models import content_hash
from hive.domain.secret_scan import REDACT, REFUSE, ScanVerdict

_log = logging.getLogger("hive.admission")


@dataclass(frozen=True, slots=True)
class StagedEpisode:
    """A pending proposal surfaced for approval — id + text + provenance only."""
    id: int
    text: str
    proposed_by: str
    content_hash: str            # hex; sha256(staged text) — post-redaction if REDACT
    ts: int
    status: str = "pending"


@dataclass(frozen=True, slots=True)
class WriteResult:
    """The result of a write. ``status`` ∈ {pending, redacted} — the domain NEVER
    returns ``refused`` (refuse RAISES SecretRefused, nothing written); the MCP
    layer maps the exception to a ``refused`` JSON envelope (P1.11)."""
    status: str
    pending_id: Optional[int]
    content_hash: Optional[str]  # hex; sha256(post-redaction staged text)
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

    # ── write: scan → (refuse 0-rows | redact-masked-pending | pending) ───────
    def write(self, text: str, *, weight: float = 1.0, source: Optional[str] = None,
              proposed_by: str, tags: str = "", request_id: str = "-") -> WriteResult:
        verdict = self._scanner.scan(text)
        if verdict.action == REFUSE:
            # rule NAMES + counts only — never the matched bytes (the finding has none)
            _log.warning("admission.refused", extra={
                "event": "admission.refused", "rules": [f.rule for f in verdict.findings],
                "n_findings": len(verdict.findings), "proposed_by": proposed_by,
                "text_len": len(text), "request_id": request_id})
            raise SecretRefused(
                f"refused: credential detected ({len(verdict.findings)} finding(s), "
                f"rules={[f.rule for f in verdict.findings]})",
                rules=[f.rule for f in verdict.findings],
                n_findings=len(verdict.findings))

        staged_text = verdict.redacted_text if verdict.action == REDACT else text
        status = "redacted" if verdict.action == REDACT else "pending"
        if verdict.action == REDACT:
            _log.info("admission.redacted", extra={
                "event": "admission.redacted", "rules": [f.rule for f in verdict.findings],
                "n_spans": len(verdict.findings), "proposed_by": proposed_by,
                "request_id": request_id})
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
        _log.info("admission.staged", extra={
            "event": "admission.staged", "pending_id": eid, "content_hash": h,
            "deduped": deduped, "status": status, "proposed_by": proposed_by})
        return WriteResult(status=status, pending_id=eid, content_hash=h,
                           scan=verdict, deduped=deduped)

    # ── list_pending: surface pending proposals for native-chat approval ──────
    def list_pending(self, since: int = 0) -> list[StagedEpisode]:
        out: list[StagedEpisode] = []
        for r in self._store.list_pending(since=since):
            out.append(StagedEpisode(
                id=r["id"], text=r["text"], proposed_by=r.get("proposed_by") or "",
                content_hash=content_hash(r["text"]), ts=int(r.get("ts", 0))))
        return out

    # ── approve: the ONLY path that embeds + indexes (the recall teeth) ───────
    def approve(self, ids: Sequence[int], approver: str,
                *, request_id: str = "-") -> tuple[list[int], list[int]]:
        approved: list[int] = []
        skipped: list[int] = []
        for eid in ids:
            ep = self._store.get_episode(eid)
            if ep is None or ep.status == "approved":
                skipped.append(int(eid))                # unknown or replay → no-op
                continue
            value = self._embedder.encode(ep.text)      # value computed at APPROVE, not stage
            ok = self._store.approve(eid, approver, value,
                                     expected_version=ep.version, approved_ts=self._now())
            (approved if ok else skipped).append(int(eid))
        _log.info("admission.approved", extra={
            "event": "admission.approved", "approved_ids": approved,
            "indexed": len(approved), "skipped_ids": skipped, "approver": approver,
            "request_id": request_id})
        return approved, skipped

    # ── reject: never touches the index (rejected rows were never indexed) ────
    def reject(self, ids: Sequence[int], *, keep_rejected: bool = False,
               request_id: str = "-") -> tuple[list[int], list[int]]:
        rejected: list[int] = []
        skipped: list[int] = []
        for eid in ids:
            ep = self._store.get_episode(eid)
            if ep is None or ep.status == "approved":
                skipped.append(int(eid))                # can't un-publish an approved memory
                continue
            self._store.reject(eid, keep=keep_rejected)
            rejected.append(int(eid))
        _log.info("admission.rejected", extra={
            "event": "admission.rejected", "rejected_ids": rejected,
            "skipped_ids": skipped, "kept": keep_rejected, "request_id": request_id})
        return rejected, skipped
