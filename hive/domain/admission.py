"""AdmissionService — the one deep module that turns a proposed insight into a
recallable memory through a single irreversible-by-construction gauntlet (M05):

  1. deterministic secret scan BEFORE any persistence (refuse/redact pre-stage),
  2. content-hash-deduped staging (anchors + repo scope land in the same tx),
  3. embed + complete in the SAME call — the value vector is computed and the row
     is flipped to ``status='approved'`` with its trust label before the call
     returns.

v3 (thin-agent contract): there is NO approver anywhere — ``write`` lands
``trust='provisional'`` (servable immediately, labeled, actively healed by the
outcome loop) and ``capture`` lands ``trust='quarantined'`` (embedded but
structurally unservable until demand promotes it). The ONE gate that remains is the
deterministic secret scan, INVOKED on every write/capture (admission always calls the
scanner — non-bypassable in-domain; refuse is fail-closed). The injected scanner's
STRICTNESS is the only operator seam: ``HIVE_SECRET_SCAN__ENABLED=false`` makes the
adapter return CLEAN, so the floor is default-on but operator-disableable —
admission's call is unchanged, it just sees a CLEAN verdict (the toggle never
reaches this pure module — Law 4).

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

import json
import logging
from dataclasses import dataclass
from typing import Callable, Optional, Protocol, Sequence

import numpy as np  # permitted in domain (compute carrier, not I/O)

from hive.domain.errors import SecretRefused
from hive.domain.evidence_kinds import EK_PROMOTE
from hive.domain.kinds import DEFAULT_KIND
from hive.domain.lifecycle import (
    DEPRECATED,
    ESTABLISHED,
    PROVISIONAL,
    QUARANTINED,
    LifecycleService,
)
from hive.domain.models import AnchorRef, Episode, content_hash
from hive.domain.secret_scan import REDACT, REFUSE, ScanVerdict

from hive.domain.ports import EmbeddingProvider, SecretScanner

_log = logging.getLogger("hive.admission")


@dataclass(frozen=True, slots=True)
class WriteResult:
    """The result of a write or capture. ``status``:
      - ``approved`` / ``redacted`` — a write; servable immediately with
        ``trust='provisional'``; ``redacted`` additionally signals masked spans.
      - ``quarantined`` — an autonomous capture; embedded but structurally
        unservable until demand promotes it.
      - ``disabled`` — autonomy is off; nothing was written (``scan`` is None —
        the text was never even scanned).
    The domain NEVER returns ``refused`` (refuse RAISES SecretRefused, nothing
    written); the MCP layer maps that exception to a ``refused`` JSON envelope.
    There is no retirement field: retirement-with-replacement has ONE owner at the
    boundary, which runs it after this call returns (the signals a correction
    produces are measured between the loser and the already-stored winner)."""

    status: str
    episode_id: Optional[int]
    content_hash: Optional[str]  # hex; sha256(post-redaction stored text)
    scan: Optional[ScanVerdict]
    deduped: bool = False


class _AdmissionStore(Protocol):
    """The store surface admission actually consumes (typing-only; the concrete
    SqliteEpisodeStore satisfies it structurally). NOT a swap-seam port —
    episode-CRUD deliberately stays off ports.py (see ports docstring)."""

    def stage(
        self,
        *,
        text: str,
        weight: float,
        proposed_by: str,
        tenant_id: str = "default",
        ts: int = 0,
        polarity: str = "neutral",
        kind: str = ...,
        meta: str = "",
        anchors: Sequence[tuple[str, str]] = (),
        repos: Sequence[tuple[str, str]] = (),
    ) -> tuple[int, bool]: ...
    def complete(
        self,
        episode_id: int,
        value: "np.ndarray",
        *,
        expected_version: int,
        trust: str,
        last_active_ts: int = 0,
    ) -> bool: ...
    def reject(self, episode_id: int, *, keep: bool = False) -> None: ...
    def set_trust(self, episode_id: int, new_trust: str, *, now: int) -> bool: ...
    def get_episode(self, episode_id: int) -> Optional[Episode]: ...
    def insert_audit(
        self, episode_id: int, kind: str, actor: str, ts: int, payload: str
    ) -> int: ...


class AdmissionService:
    """Drives the M03 Store + M02 Index ports and the SecretScanner port. Owns the
    admission POLICY (non-swappable); owns no tables."""

    def __init__(
        self,
        store: "_AdmissionStore",
        scanner: SecretScanner,
        embedder: EmbeddingProvider,
        *,
        now: Callable[[], int],
        lifecycle: Optional[LifecycleService] = None,
        autonomy_enabled: bool = True,
    ) -> None:
        self._store = store  # the store adapter (+ its warm VectorIndex)
        self._scanner = scanner
        self._embedder = embedder  # text -> value[d]
        self._now = now
        # LifecycleService (or None): capture's synchronous promotion trigger +
        # decay-sweep piggyback. Its entry points are fail-open by contract, so a
        # trigger fault can never break the capture that fired it.
        self._lifecycle = lifecycle
        # autonomy off ⇒ capture() refuses cleanly before scanning or staging.
        self._autonomy_enabled = bool(autonomy_enabled)

    # ── shared gauntlet steps ──────────────────────────────────────────────────
    def _scan_gate(
        self, text: str, *, proposed_by: str, request_id: str
    ) -> ScanVerdict:
        """The ONE non-bypassable gate: deterministic secret scan BEFORE any
        persistence. REFUSE raises (0 rows, 0 blobs); REDACT returns the verdict
        whose masked text is the only thing that may be staged. Logged fields are
        rule names/counts only — never the matched bytes."""
        verdict = self._scanner.scan(text)
        if verdict.action == REFUSE:
            _log.warning(
                "admission.refused",
                extra={
                    "event": "admission.refused",
                    "rules": [f.rule for f in verdict.findings],
                    "n_findings": len(verdict.findings),
                    "spans": [list(f.span) for f in verdict.findings],
                    "proposed_by": proposed_by,
                    "text_len": len(text),
                    "request_id": request_id,
                },
            )
            raise SecretRefused("credential detected", findings=verdict.findings)
        if verdict.action == REDACT:
            _log.info(
                "admission.redacted",
                extra={
                    "event": "admission.redacted",
                    "rules": [f.rule for f in verdict.findings],
                    "n_spans": len(verdict.findings),
                    "proposed_by": proposed_by,
                    "request_id": request_id,
                },
            )
        return verdict

    def _meta_gate(self, meta: str, *, proposed_by: str, request_id: str) -> None:
        """The REFUSE-ONLY secret floor on the serialized ``meta`` carrier: ANY finding
        raises SecretRefused (0 rows) — meta is never redacted, because a masked handle
        is garbage to its consumer. Runs BEFORE the text gauntlet so a credential-bearing
        handle never reaches staging. Logged fields are rule names/counts only."""
        if not meta:
            return
        verdict = self._scanner.scan(meta)
        if verdict.findings:
            _log.warning(
                "admission.meta_refused",
                extra={
                    "event": "admission.meta_refused",
                    "rules": [f.rule for f in verdict.findings],
                    "n_findings": len(verdict.findings),
                    "spans": [list(f.span) for f in verdict.findings],
                    "proposed_by": proposed_by,
                    "request_id": request_id,
                },
            )
            raise SecretRefused(
                "credential detected in meta", findings=verdict.findings
            )

    @staticmethod
    def _anchor_tuples(anchors: Sequence[AnchorRef]) -> list[tuple[str, str]]:
        """The store-facing projection of the anchor carriers: ``(repo, anchor)``
        pairs (fingerprints are server-minted later — never caller-supplied)."""
        return [(a.repo, a.anchor) for a in anchors]

    # ── write: scan → (refuse 0-rows | redact-mask) → stage → embed → complete ──
    def write(
        self,
        text: str,
        *,
        proposed_by: str,
        weight: float = 1.0,
        request_id: str = "-",
        polarity: str = "neutral",
        kind: str = DEFAULT_KIND,
        anchors: Sequence[AnchorRef] = (),
        repos: Sequence[tuple[str, str]] = (),
        meta: str = "",
    ) -> WriteResult:
        """Store an insight in one call, servable NOW as ``trust='provisional'``
        (v3 §3.1: serve-as-provisional-then-heal — no approver exists; the top
        tier is reached only via canonical-line outcome verification). REFUSE
        raises SecretRefused (nothing written); CLEAN/REDACT stage → embed →
        complete and return a provisional, recallable memory.

        ``anchors`` (validated at the boundary — ``normalize_anchors`` owns the
        grammar and the registered-repo check) and ``repos`` are threaded to
        ``stage`` in the same tx: anchors as ``(repo, anchor)`` pairs, repos as
        the caller's own ``(repo, ref)`` pairs — a non-empty ``ref`` is the
        memory's DECLARED line for that repo (sourced from the boundary's
        ``name@branch`` scope grammar), stored verbatim in ``episode_refs``;
        ``ref == ''`` stays canonical (no declared line, no row).

        ``meta`` is the already-normalized serialized carrier (the boundary owns the
        grammar); it is secret-scanned refuse-only BEFORE the text gauntlet. On a
        dedup hit the existing row is returned UNCHANGED — meta is NOT merged
        (immutability: a new meta needs new text or supersession). // O(1) DB ops + one embed."""
        self._meta_gate(meta, proposed_by=proposed_by, request_id=request_id)
        verdict = self._scan_gate(text, proposed_by=proposed_by, request_id=request_id)
        staged_text = (
            (verdict.redacted_text or "") if verdict.action == REDACT else text
        )
        status = "redacted" if verdict.action == REDACT else "approved"

        # stage the (post-redaction) row + blob + anchor/scope rows; dedup by content_hash.
        try:
            eid, deduped = self._store.stage(
                text=staged_text,
                weight=weight,
                proposed_by=proposed_by,
                ts=self._now(),
                polarity=polarity,
                kind=kind,
                meta=meta,
                anchors=self._anchor_tuples(anchors),
                repos=list(repos),
            )
        except Exception:
            _log.error(
                "admission.stage_fail",
                extra={
                    "event": "admission.stage_fail",
                    "proposed_by": proposed_by,
                    "request_id": request_id,
                },
                exc_info=True,
            )
            raise

        h = content_hash(staged_text)
        ep = self._store.get_episode(eid)
        if ep is not None and ep.status == "approved":
            # MATERIALIZED dedup target. Branch on TRUST (the servability axis): status
            # alone is NOT servability — a quarantined capture is status='approved' too,
            # so keying the idempotent skip on status silently dropped the lift (BUG-001).
            if ep.trust in (ESTABLISHED, PROVISIONAL):
                # already servable → idempotent: no re-embed, no trust change (a
                # re-write can never demote an established row back to provisional).
                _log.info(
                    "admission.dedup_servable",
                    extra={
                        "event": "admission.dedup_servable",
                        "episode_id": eid,
                        "trust": ep.trust,
                        "content_hash": h,
                        "proposed_by": proposed_by,
                        "request_id": request_id,
                    },
                )
                return WriteResult(
                    status=status,
                    episode_id=eid,
                    content_hash=h,
                    scan=verdict,
                    deduped=True,
                )
            if ep.trust == DEPRECATED:
                # a retired row is never silently revived by re-writing its old text;
                # re-establishment goes through replaces= only.
                _log.info(
                    "admission.dedup_deprecated_norevive",
                    extra={
                        "event": "admission.dedup_deprecated_norevive",
                        "episode_id": eid,
                        "content_hash": h,
                        "proposed_by": proposed_by,
                        "request_id": request_id,
                    },
                )
                return WriteResult(
                    status=status,
                    episode_id=eid,
                    content_hash=h,
                    scan=verdict,
                    deduped=True,
                )
            # quarantined: the write LIFTS the existing materialized row to
            # provisional in place (value already embedded, no re-embed) and
            # stamps the transition into the ledger.
            if not self._store.set_trust(eid, PROVISIONAL, now=self._now()):
                _log.error(
                    "admission.dedup_lift_failed",
                    extra={
                        "event": "admission.dedup_lift_failed",
                        "episode_id": eid,
                        "request_id": request_id,
                    },
                )
                raise RuntimeError(
                    f"lift-on-dedup failed for episode {eid} (lost-update race)"
                )
            # marker: the dedup-lift AUDIT row — a quarantined→provisional lift
            # must land its evidence row, never flip trust silently. Dropping this
            # insert_audit is the named mutation: CT-1's audit assertion
            # (tests/contract/test_write_provisional.py, dedup-onto-quarantined
            # scenario) and its unit twin (tests/domain/test_admission.py::
            # test_write_dedup_onto_quarantined_lifts_with_audit) red.
            self._store.insert_audit(
                eid,
                EK_PROMOTE,
                "server",
                self._now(),
                json.dumps(
                    {
                        "rule": "write_lift",
                        "from": QUARANTINED,
                        "proposed_by": proposed_by,
                    }
                ),
            )
            _log.info(
                "admission.dedup_lifted",
                extra={
                    "event": "admission.dedup_lifted",
                    "episode_id": eid,
                    "content_hash": h,
                    "proposed_by": proposed_by,
                    "request_id": request_id,
                },
            )
            return WriteResult(
                status=status,
                episode_id=eid,
                content_hash=h,
                scan=verdict,
                deduped=True,
            )

        # embed (pure, no DB) then flip pending→approved as PROVISIONAL + index,
        # in store.complete's tx.
        try:
            value = self._embedder.encode(staged_text)
        except Exception:
            # leave NO pending leak: drop the row we just staged (best-effort) and re-raise.
            _log.error(
                "admission.embed_fail",
                extra={
                    "event": "admission.embed_fail",
                    "episode_id": eid,
                    "proposed_by": proposed_by,
                    "request_id": request_id,
                },
                exc_info=True,
            )
            try:
                self._store.reject(eid)
            except Exception:  # cleanup is best-effort
                _log.error(
                    "admission.embed_fail_cleanup_failed",
                    extra={
                        "event": "admission.embed_fail_cleanup_failed",
                        "episode_id": eid,
                        "request_id": request_id,
                    },
                    exc_info=True,
                )
            raise

        ok = self._store.complete(
            eid,
            value,
            expected_version=ep.version if ep is not None else 0,
            trust=PROVISIONAL,
            last_active_ts=self._now(),
        )
        if not ok:
            # On a FRESH stage the CAS version matches, so this is unreachable in the
            # single-writer path; a False here means a lost-update race left the row
            # NOT completed. Fail LOUD rather than return a silently-pending
            # (non-recallable) row the caller believes landed — and drop the dangling row.
            _log.error(
                "admission.complete_failed",
                extra={
                    "event": "admission.complete_failed",
                    "episode_id": eid,
                    "proposed_by": proposed_by,
                    "request_id": request_id,
                },
            )
            try:
                self._store.reject(eid)
            except Exception:
                _log.error(
                    "admission.complete_failed_cleanup_failed",
                    extra={
                        "event": "admission.complete_failed_cleanup_failed",
                        "episode_id": eid,
                        "request_id": request_id,
                    },
                    exc_info=True,
                )
            raise RuntimeError(
                f"admission complete failed for episode {eid} (lost-update CAS race)"
            )

        _log.info(
            "admission.captured",
            extra={
                "event": "admission.captured",
                "episode_id": eid,
                "content_hash": h,
                "deduped": deduped,
                "status": status,
                "proposed_by": proposed_by,
                "request_id": request_id,
            },
        )
        return WriteResult(
            status=status,
            episode_id=eid,
            content_hash=h,
            scan=verdict,
            deduped=deduped,
        )

    # ── capture: the unclear-value tail — lands embedded but UNSERVABLE ────────
    def capture(
        self,
        text: str,
        *,
        proposed_by: str,
        weight: float = 1.0,
        request_id: str = "-",
        polarity: str = "neutral",
        kind: str = DEFAULT_KIND,
        anchors: Sequence[AnchorRef] = (),
        repos: Sequence[tuple[str, str]] = (),
        meta: str = "",
    ) -> WriteResult:
        """Capture WITHOUT serving: scan → stage (dedup) → embed → complete
        ``trust='quarantined'`` (embedded but structurally unservable until
        measured demand promotes it) → synchronous promotion check → decay
        sweep. Neither verb here carries a retirement rider — retirement is the
        boundary's, and a quarantined capture never gains that power at all
        (``hive_capture`` has no ``replaces`` on its schema). With autonomy disabled, returns
        ``status='disabled'`` before anything (even the scan) runs — the store
        is untouched. The secret floor is IDENTICAL to write's (refuse raises,
        0 rows); ``meta`` (the already-normalized serialized carrier) is scanned
        refuse-only BEFORE the text gauntlet, and on a dedup hit the existing
        row's meta is preserved unmerged (identity is the text hash alone).
        ``anchors``/``repos`` are threaded to ``stage`` exactly as in ``write``
        — a captured memory can declare its line too.
        // O(1) DB ops + one embed + the O(Q·d) trigger."""
        if not self._autonomy_enabled:
            _log.info(
                "admission.capture_disabled",
                extra={
                    "event": "admission.capture_disabled",
                    "proposed_by": proposed_by,
                    "request_id": request_id,
                },
            )
            return WriteResult(
                status="disabled", episode_id=None, content_hash=None, scan=None
            )
        self._meta_gate(meta, proposed_by=proposed_by, request_id=request_id)
        verdict = self._scan_gate(text, proposed_by=proposed_by, request_id=request_id)
        staged_text = (
            (verdict.redacted_text or "") if verdict.action == REDACT else text
        )

        try:
            eid, deduped = self._store.stage(
                text=staged_text,
                weight=weight,
                proposed_by=proposed_by,
                ts=self._now(),
                polarity=polarity,
                kind=kind,
                meta=meta,
                anchors=self._anchor_tuples(anchors),
                repos=list(repos),
            )
        except Exception:
            _log.error(
                "admission.capture_stage_fail",
                extra={
                    "event": "admission.capture_stage_fail",
                    "proposed_by": proposed_by,
                    "request_id": request_id,
                },
                exc_info=True,
            )
            raise

        h = content_hash(staged_text)
        ep = self._store.get_episode(eid)
        if ep is not None and ep.status == "approved":
            # dedup onto an already-MATERIALIZED row (any trust): idempotent — a
            # capture never touches an existing row's trust (no re-embed, no
            # demotion, no promotion side door).
            _log.info(
                "admission.capture_dedup",
                extra={
                    "event": "admission.capture_dedup",
                    "episode_id": eid,
                    "content_hash": h,
                    "proposed_by": proposed_by,
                    "request_id": request_id,
                },
            )
            return WriteResult(
                status="quarantined",
                episode_id=eid,
                content_hash=h,
                scan=verdict,
                deduped=True,
            )

        try:
            value = self._embedder.encode(staged_text)
        except Exception:
            _log.error(
                "admission.capture_embed_fail",
                extra={
                    "event": "admission.capture_embed_fail",
                    "episode_id": eid,
                    "proposed_by": proposed_by,
                    "request_id": request_id,
                },
                exc_info=True,
            )
            try:
                self._store.reject(eid)
            except Exception:  # cleanup is best-effort
                _log.error(
                    "admission.capture_embed_fail_cleanup_failed",
                    extra={
                        "event": "admission.capture_embed_fail_cleanup_failed",
                        "episode_id": eid,
                        "request_id": request_id,
                    },
                    exc_info=True,
                )
            raise

        ok = self._store.complete(
            eid,
            value,
            expected_version=ep.version if ep is not None else 0,
            trust=QUARANTINED,
            last_active_ts=self._now(),
        )
        if not ok:
            _log.error(
                "admission.capture_complete_failed",
                extra={
                    "event": "admission.capture_complete_failed",
                    "episode_id": eid,
                    "proposed_by": proposed_by,
                    "request_id": request_id,
                },
            )
            try:
                self._store.reject(eid)
            except Exception:
                _log.error(
                    "admission.capture_complete_failed_cleanup_failed",
                    extra={
                        "event": "admission.capture_complete_failed_cleanup_failed",
                        "episode_id": eid,
                        "request_id": request_id,
                    },
                    exc_info=True,
                )
            raise RuntimeError(
                f"admission capture complete failed for episode {eid} "
                "(lost-update CAS race)"
            )

        _log.info(
            "admission.capture_landed",
            extra={
                "event": "admission.capture_landed",
                "episode_id": eid,
                "content_hash": h,
                "deduped": deduped,
                "proposed_by": proposed_by,
                "request_id": request_id,
            },
        )
        # synchronous lifecycle moments (both fail-open inside the service): does
        # existing demand already want this candidate, then the decay piggyback.
        if self._lifecycle is not None:
            self._lifecycle.on_capture(eid)
            self._lifecycle.sweep()
        return WriteResult(
            status="quarantined",
            episode_id=eid,
            content_hash=h,
            scan=verdict,
            deduped=deduped,
        )
