"""Multi-root fixture: a cross-root caller of ``alpha.a.run``."""

from alpha.a import run


def go(y: int) -> int:
    """Calls ``run`` across the root boundary so ``run``'s dependency
    neighborhood provably spans two top-level dirs."""
    return run(y) + 1
