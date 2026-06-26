"""AdmissionService — the one deep module that turns a proposed insight into a
recallable memory through a single irreversible-by-construction gauntlet (M05):

  1. deterministic secret scan BEFORE any persistence (refuse/redact pre-stage),
  2. content-hash-deduped staging,
  3. embed + approve in the SAME call — the value vector is computed and the row is
     flipped to ``status='approved'`` (the only recallable state) before ``write``
     returns.

CLIENT-GATED capture: the server-side pending→approve QUEUE
was removed. A write is approved by a human in native chat BEFORE the tool call; the
caller passes that approver as ``approved_by`` and the server records it. There is no
``list_pending`` / ``approve`` / ``reject`` surface — the ONE gate that remains is the
deterministic secret scan, INVOKED on every write/capture (admission always calls the scanner —
non-bypassable in-domain; refuse is fail-closed). The injected scanner's STRICTNESS is the only
operator seam: ``HIVE_SECRET_SCAN__ENABLED=false`` makes the adapter return CLEAN, so the floor
is default-on but operator-disableable — admission's call is unchanged, it just sees a CLEAN
verdict (the toggle never reaches this pure module — Law 4).

PURE domain: depends only on injected ports (SecretScanner, the store,
EmbeddingProvider) + an injected ``now`` clock. Imports no sqlite/torch/os/time —
the purity gate enforces it. Admission is deliberately NOT swappable (the approved-only
recall boundary is the one structural guarantee); only the SecretScanner under it is a
swap seam.

Secret-safety: on REFUSE nothing is written (0 rows, 0 blobs) and ``SecretRefused``
carries only rule names; on REDACT only the masked text is stored; no log line ever
contains the secret text (every field logged is a label/count/id) — #5a/#5b.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Optional

from hive.domain.agi import is_agi_override
from hive.domain.errors import SecretRefused
from hive.domain.kinds import DEFAULT_KIND
from hive.domain.lifecycle import DEPRECATED, ESTABLISHED, QUARANTINED
from hive.domain.models import content_hash
from hive.domain.secret_scan import REDACT, REFUSE, ScanVerdict

_log = logging.getLogger("hive.admission")


@dataclass(frozen=True, slots=True)
class WriteResult:
    """The result of a write or capture. ``status``:
      - ``approved`` / ``redacted`` — a client-gated write; recallable immediately
        (trust='established'); ``redacted`` additionally signals masked spans.
      - ``quarantined`` — an autonomous capture; embedded but structurally
        unservable until demand promotes it.
      - ``disabled`` — autonomy is off; nothing was written (``scan`` is None —
        the text was never even scanned).
    The domain NEVER returns ``refused`` (refuse RAISES SecretRefused, nothing
    written); the MCP layer maps that exception to a ``refused`` JSON envelope.
    ``superseded`` carries the retired target id when ``write(replaces=...)``
    applied a supersession."""
    status: str
    episode_id: Optional[int]
    content_hash: Optional[str]  # hex; sha256(post-redaction stored text)
    scan: Optional[ScanVerdict]
    deduped: bool = False
    superseded: Optional[int] = None


class AdmissionService:
    """Drives the M03 Store + M02 Index ports and the SecretScanner port. Owns the
    admission POLICY (non-swappable); owns no tables."""

    def __init__(self, store, scanner, embedder, *, now: Callable[[], int],
                 lifecycle=None, autonomy_enabled: bool = True) -> None:
        self._store = store              # the store adapter (+ its warm VectorIndex)
        self._scanner = scanner          # SecretScanner
        self._embedder = embedder        # EmbeddingProvider (text -> value[d])
        self._now = now
        # LifecycleService (or None): capture's synchronous promotion trigger +
        # decay-sweep piggyback. Its entry points are fail-open by contract, so a
        # trigger fault can never break the capture that fired it.
        self._lifecycle = lifecycle
        # autonomy off ⇒ capture() refuses cleanly before scanning or staging.
        self._autonomy_enabled = bool(autonomy_enabled)

    # ── shared gauntlet steps ──────────────────────────────────────────────────
    def _scan_gate(self, text: str, *, proposed_by: str, approved_by: Optional[str],
                   request_id: str) -> ScanVerdict:
        """The ONE non-bypassable gate: deterministic secret scan BEFORE any
        persistence. REFUSE raises (0 rows, 0 blobs); REDACT returns the verdict
        whose masked text is the only thing that may be staged. Logged fields are
        rule names/counts only — never the matched bytes."""
        verdict = self._scanner.scan(text)
        if verdict.action == REFUSE:
            _log.warning("admission.refused", extra={
                "event": "admission.refused", "rules": [f.rule for f in verdict.findings],
                "n_findings": len(verdict.findings), "proposed_by": proposed_by,
                "approved_by": approved_by, "text_len": len(text), "request_id": request_id})
            raise SecretRefused(
                f"refused: credential detected ({len(verdict.findings)} finding(s), "
                f"rules={[f.rule for f in verdict.findings]})",
                rules=[f.rule for f in verdict.findings],
                n_findings=len(verdict.findings))
        if verdict.action == REDACT:
            _log.info("admission.redacted", extra={
                "event": "admission.redacted", "rules": [f.rule for f in verdict.findings],
                "n_spans": len(verdict.findings), "proposed_by": proposed_by,
                "request_id": request_id})
        return verdict

    def _apply_supersession(self, replaces: Optional[int], new_id: int, *,
                            actor: str, request_id: str) -> Optional[int]:
        """Run the human-vouched supersession AFTER the replacement landed. A
        refused supersede (self-supersede via dedup, or a target that vanished
        between validation and now) is benign — the new memory IS stored; both
        versions coexisting is the pre-supersession status quo."""
        if replaces is None:
            return None
        ok = self._store.supersede(int(replaces), int(new_id), actor=actor,
                                   ts=self._now())
        if ok:
            _log.info("admission.superseded", extra={
                "event": "admission.superseded", "target": int(replaces),
                "replacement": int(new_id), "actor": actor, "request_id": request_id})
            return int(replaces)
        _log.info("admission.supersede_noop", extra={
            "event": "admission.supersede_noop", "target": int(replaces),
            "replacement": int(new_id), "request_id": request_id})
        return None

    # ── write: scan → (refuse 0-rows | redact-mask) → stage → embed → approve ──
    def write(self, text: str, *, approved_by: str, proposed_by: str,
              weight: float = 1.0, request_id: str = "-",
              replaces: Optional[int] = None,
              polarity: str = "neutral", kind: str = DEFAULT_KIND,
              anchor: str = "") -> WriteResult:
        """Capture a human-approved insight in one call. ``approved_by`` is the
        principal that approved this write in native chat (client-gated trust);
        ``proposed_by`` is the agent that proposed it — both are recorded. REFUSE raises
        SecretRefused (nothing written); CLEAN/REDACT stage → embed → approve and return
        an approved, recallable memory (trust='established').

        Provenance is DERIVED from the ``approved_by`` VALUE — ``human`` for a named vouch,
        ``agent_reasoned`` for the reserved ``AGI_OVERRIDE`` sentinel (an agent reasoned the
        content; no human authored it — the honest under-claim, Law 2). It is the memory's
        ORIGIN, never a caller field (INV-2: no caller-asserted provenance); the sentinel is a
        transport-resolved actor, not caller-asserted origin. The dedup-onto-quarantined
        establishment path does NOT rewrite provenance: the vouch is recorded by
        approved_by/trust, not by relabelling origin (a quarantined capture stays
        ``agent_reasoned``, which an override establish leaves correct).

        ``replaces`` (human-vouched supersession): the named target is retired in
        favor of this write — validated to EXIST before anything is staged (an
        unknown target fails the WHOLE call: no stored-but-not-retired partial);
        the supersession itself runs after the new row lands. // O(1) DB ops + one embed."""
        if replaces is not None and self._store.get_episode(int(replaces)) is None:
            _log.warning("admission.replaces_unknown_target", extra={
                "event": "admission.replaces_unknown_target", "target": int(replaces),
                "proposed_by": proposed_by, "request_id": request_id})
            raise ValueError(f"replaces target {int(replaces)} does not exist — "
                             "nothing stored, nothing retired")
        verdict = self._scan_gate(text, proposed_by=proposed_by,
                                  approved_by=approved_by, request_id=request_id)
        staged_text = verdict.redacted_text if verdict.action == REDACT else text
        status = "redacted" if verdict.action == REDACT else "approved"
        # Provenance is DERIVED from the approver VALUE (the sentinel), never a caller arg
        # (INV-2). An AGI_OVERRIDE write under-claims its ORIGIN as agent_reasoned (an agent
        # reasoned it; no human authored it — Law 2); a human-named vouch stamps human. Flipping
        # this to a constant "human" is the provenance mutation (the override-establish test reds).
        provenance = "agent_reasoned" if is_agi_override(approved_by) else "human"

        # stage the (post-redaction) row + blob; dedup by content_hash.
        try:
            eid, deduped = self._store.stage(
                text=staged_text, weight=weight, tags="",
                proposed_by=proposed_by, ts=self._now(), provenance=provenance,
                polarity=polarity, kind=kind, anchor=anchor)
        except Exception:
            _log.error("admission.stage_fail", extra={
                "event": "admission.stage_fail", "proposed_by": proposed_by,
                "request_id": request_id}, exc_info=True)
            raise

        h = content_hash(staged_text)
        ep = self._store.get_episode(eid)
        if ep is not None and ep.status == "approved":
            # MATERIALIZED dedup target. Branch on TRUST (the servability axis): status
            # alone is NOT servability — a quarantined capture is status='approved' too,
            # so keying the idempotent skip on status silently dropped the vouch (BUG-001).
            if ep.trust == ESTABLISHED:
                # already human-servable → idempotent: no re-embed/re-approve, and the
                # original approver is preserved (a later caller can't overwrite the vouch).
                _log.info("admission.dedup_established", extra={
                    "event": "admission.dedup_established", "episode_id": eid,
                    "content_hash": h, "proposed_by": proposed_by, "request_id": request_id})
                superseded = self._apply_supersession(replaces, eid, actor=approved_by,
                                                      request_id=request_id)
                return WriteResult(status=status, episode_id=eid, content_hash=h,
                                   scan=verdict, deduped=True, superseded=superseded)
            if ep.trust == DEPRECATED:
                # a retired row is never silently revived by re-writing its old text;
                # re-establishment goes through replaces= only.
                _log.info("admission.dedup_deprecated_norevive", extra={
                    "event": "admission.dedup_deprecated_norevive", "episode_id": eid,
                    "content_hash": h, "proposed_by": proposed_by, "request_id": request_id})
                return WriteResult(status=status, episode_id=eid, content_hash=h,
                                   scan=verdict, deduped=True)
            # quarantined / provisional: the human vouch ESTABLISHES the existing
            # materialized row in place — value already embedded, so no re-embed. This is
            # exactly the promotion the status-keyed guard used to drop (BUG-001).
            if not self._store.set_trust(eid, ESTABLISHED, now=self._now(),
                                         approver=approved_by, approved_ts=self._now()):
                _log.error("admission.dedup_establish_failed", extra={
                    "event": "admission.dedup_establish_failed", "episode_id": eid,
                    "request_id": request_id})
                raise RuntimeError(
                    f"establish-on-dedup failed for episode {eid} (lost-update race)")
            _log.info("admission.dedup_established_promoted", extra={
                "event": "admission.dedup_established_promoted", "episode_id": eid,
                "content_hash": h, "approved_by": approved_by,
                "proposed_by": proposed_by, "request_id": request_id})
            superseded = self._apply_supersession(replaces, eid, actor=approved_by,
                                                  request_id=request_id)
            return WriteResult(status=status, episode_id=eid, content_hash=h,
                               scan=verdict, deduped=True, superseded=superseded)

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
        superseded = self._apply_supersession(replaces, eid, actor=approved_by,
                                              request_id=request_id)
        return WriteResult(status=status, episode_id=eid, content_hash=h,
                           scan=verdict, deduped=deduped, superseded=superseded)

    # ── capture: the autonomous path — lands embedded but UNSERVABLE ───────────
    def capture(self, text: str, *, proposed_by: str, weight: float = 1.0,
                request_id: str = "-", polarity: str = "neutral",
                kind: str = DEFAULT_KIND, anchor: str = "") -> WriteResult:
        """Capture WITHOUT asking: scan → stage (dedup) → embed → complete
        ``trust='quarantined'`` (``approved_by`` NULL — embedded but structurally
        unservable until measured demand promotes it) → synchronous promotion
        check → decay sweep. Deliberately has NO ``replaces`` and no approver: a
        quarantined capture must never gain retirement power. With autonomy
        disabled, returns ``status='disabled'`` before anything (even the scan)
        runs — the store is untouched. The secret floor is IDENTICAL to write's
        (refuse raises, 0 rows). Provenance is TRANSPORT-SET to ``agent_reasoned`` (an
        agent reasoned the content) — never a caller field (INV-2). // O(1) DB ops + one
        embed + the O(Q·d) trigger."""
        if not self._autonomy_enabled:
            _log.info("admission.capture_disabled", extra={
                "event": "admission.capture_disabled", "proposed_by": proposed_by,
                "request_id": request_id})
            return WriteResult(status="disabled", episode_id=None,
                               content_hash=None, scan=None)
        verdict = self._scan_gate(text, proposed_by=proposed_by, approved_by=None,
                                  request_id=request_id)
        staged_text = verdict.redacted_text if verdict.action == REDACT else text

        try:
            eid, deduped = self._store.stage(
                text=staged_text, weight=weight, tags="",
                proposed_by=proposed_by, ts=self._now(), provenance="agent_reasoned",
                polarity=polarity, kind=kind, anchor=anchor)
        except Exception:
            _log.error("admission.capture_stage_fail", extra={
                "event": "admission.capture_stage_fail", "proposed_by": proposed_by,
                "request_id": request_id}, exc_info=True)
            raise

        h = content_hash(staged_text)
        ep = self._store.get_episode(eid)
        if ep is not None and ep.status == "approved":
            # dedup onto an already-MATERIALIZED row (any trust): idempotent — a
            # capture never touches an existing row's trust (no re-embed, no
            # demotion, no promotion side door).
            _log.info("admission.capture_dedup", extra={
                "event": "admission.capture_dedup", "episode_id": eid,
                "content_hash": h, "proposed_by": proposed_by,
                "request_id": request_id})
            return WriteResult(status="quarantined", episode_id=eid, content_hash=h,
                               scan=verdict, deduped=True)

        try:
            value = self._embedder.encode(staged_text)
        except Exception:
            _log.error("admission.capture_embed_fail", extra={
                "event": "admission.capture_embed_fail", "episode_id": eid,
                "proposed_by": proposed_by, "request_id": request_id}, exc_info=True)
            try:
                self._store.reject(eid)
            except Exception:                            # cleanup is best-effort
                _log.error("admission.capture_embed_fail_cleanup_failed", extra={
                    "event": "admission.capture_embed_fail_cleanup_failed",
                    "episode_id": eid, "request_id": request_id}, exc_info=True)
            raise

        ok = self._store.complete(
            eid, value, expected_version=ep.version if ep is not None else 0,
            trust=QUARANTINED, approver=None, approved_ts=0,
            last_active_ts=self._now())
        if not ok:
            _log.error("admission.capture_complete_failed", extra={
                "event": "admission.capture_complete_failed", "episode_id": eid,
                "proposed_by": proposed_by, "request_id": request_id})
            try:
                self._store.reject(eid)
            except Exception:
                _log.error("admission.capture_complete_failed_cleanup_failed", extra={
                    "event": "admission.capture_complete_failed_cleanup_failed",
                    "episode_id": eid, "request_id": request_id}, exc_info=True)
            raise RuntimeError(
                f"admission capture complete failed for episode {eid} "
                "(lost-update CAS race)")

        _log.info("admission.capture_landed", extra={
            "event": "admission.capture_landed", "episode_id": eid,
            "content_hash": h, "deduped": deduped, "proposed_by": proposed_by,
            "request_id": request_id})
        # synchronous lifecycle moments (both fail-open inside the service): does
        # existing demand already want this candidate, then the decay piggyback.
        if self._lifecycle is not None:
            self._lifecycle.on_capture(eid)
            self._lifecycle.sweep()
        return WriteResult(status="quarantined", episode_id=eid, content_hash=h,
                           scan=verdict, deduped=deduped)
