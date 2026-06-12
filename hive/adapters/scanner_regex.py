"""DefaultSecretScanner — the default adapter behind the SecretScanner port (M05).

Wraps the pure ``hive.domain.secret_scan.scan`` with the configured thresholds +
redact mode. The swap seam: a vendor DLP service or a stricter org ruleset
implements exactly ``scan(text) -> ScanVerdict`` and is selected by
``secret_scan.provider`` in config — no core change. The domain state machine is
provider-blind; the frozen ``ScanVerdict`` is the enforced shared contract.
"""
from __future__ import annotations

from hive.domain.secret_scan import (
    DEFAULT_ENTROPY_BITS_FLOOR, DEFAULT_ENTROPY_MIN_LEN, REFUSE, ScanVerdict, scan,
)


class DefaultSecretScanner:
    """Config-bound wrapper over the pure scan. ``redact_mode`` default ``"refuse"``
    is the fail-closed direction (a detected credential aborts the write rather
    than masking it)."""

    def __init__(self, *, redact_mode: str = REFUSE,
                 entropy_min_len: int = DEFAULT_ENTROPY_MIN_LEN,
                 entropy_bits_floor: float = DEFAULT_ENTROPY_BITS_FLOOR) -> None:
        self._mode = redact_mode
        self._entropy_min_len = int(entropy_min_len)
        self._entropy_bits_floor = float(entropy_bits_floor)

    def scan(self, text: str) -> ScanVerdict:
        return scan(text, mode=self._mode,
                    entropy_min_len=self._entropy_min_len,
                    entropy_bits_floor=self._entropy_bits_floor)
