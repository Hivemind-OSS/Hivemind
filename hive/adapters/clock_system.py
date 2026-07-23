"""SystemClock — the real wall-clock adapter behind the ``Clock`` port.

The pure domain (recall / admission / produce / attribution) never imports ``time``
(the purity gate forbids it); every ``now()`` is injected. This is the ONE place the
real monotonic-ish wall clock enters the system. Integer epoch seconds — the same unit
the settlement schedule, the exposure ledger ``injected_ts``, and the producer
watermark all compare against.
"""

from __future__ import annotations

import time


class SystemClock:
    """``now() -> int`` epoch seconds. Stateless; safe to share across the wired ports."""

    def now(self) -> int:
        return int(time.time())
