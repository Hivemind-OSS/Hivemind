"""DefaultSecretScanner — the default adapter behind the SecretScanner port (M05).

Wraps the pure ``hive.domain.secret_scan.scan`` with the configured thresholds +
redact mode. The swap seam: a vendor DLP service or a stricter org ruleset
implements exactly ``scan(text) -> ScanVerdict`` and is selected by
``secret_scan.provider`` in config — no core change. The domain state machine is
provider-blind; the frozen ``ScanVerdict`` is the enforced shared contract.
"""

from __future__ import annotations

from hive.domain.secret_scan import (
    CLEAN,
    DEFAULT_ENTROPY_BITS_FLOOR,
    DEFAULT_ENTROPY_MIN_LEN,
    REFUSE,
    ScanVerdict,
    scan,
)

# The bypass verdict an operator-disabled floor returns: a structurally valid CLEAN (no
# findings, no redaction) — the can't-lie invariant holds, "not scanning" is surfaced
# out-of-band (boot WARN + hive_health), never encoded as a false finding.
_BYPASS = ScanVerdict(action=CLEAN, redacted_text=None, findings=())


class DefaultSecretScanner:
    """Config-bound wrapper over the pure scan. ``redact_mode`` default ``"refuse"``
    is the fail-closed direction (a detected credential aborts the write rather
    than masking it).

    ``enabled`` (the ``HIVE_SECRET_SCAN__ENABLED`` operator opt-OUT) defaults True — the
    always-on floor, byte-identical to a scan with no toggle. ``enabled=False`` BYPASSES the
    scan: ``scan`` returns CLEAN unconditionally, so raw text (including credentials) is staged
    unscanned by deliberate operator choice. The bypass lives here in the adapter so the pure
    domain stays unaware (Law 4) and the port + every consumer call site are unchanged — a
    single flag flows uniformly to admission, the recall miss-floor, and the conflict-flag note.
    Disabling LOOSENS a safety gate, so it is opt-in only (the default is the safe direction)."""

    def __init__(
        self,
        *,
        redact_mode: str = REFUSE,
        entropy_min_len: int = DEFAULT_ENTROPY_MIN_LEN,
        entropy_bits_floor: float = DEFAULT_ENTROPY_BITS_FLOOR,
        enabled: bool = True,
    ) -> None:
        self._mode = redact_mode
        self._entropy_min_len = int(entropy_min_len)
        self._entropy_bits_floor = float(entropy_bits_floor)
        self._enabled = bool(enabled)

    def scan(self, text: str) -> ScanVerdict:
        if not self._enabled:  # operator opt-OUT: the floor is bypassed
            return _BYPASS
        return scan(
            text,
            mode=self._mode,
            entropy_min_len=self._entropy_min_len,
            entropy_bits_floor=self._entropy_bits_floor,
        )
