"""Multi-root fixture: the anchored callable, imported across a root boundary.

Source here spans two top-level dirs (``alpha/`` and ``beta/``), so matrix's
inferred corpus root IS the repo root and the graph dialect already agrees with
the repo-root-relative anchor (offset ``""``) — the "dialects agree" counterpart
to the single-source-root variant.
"""


def run(x: int) -> int:
    """The anchored callable (``alpha/a.py:run``); ``beta.b.go`` calls it, so its
    neighborhood spans both roots."""
    return x * 3
