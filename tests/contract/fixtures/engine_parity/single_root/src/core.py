"""Single-source-root fixture: the anchored caller module.

Imports the sibling helper by its root-relative module name (``util``), which is
resolvable only because matrix roots the corpus at ``src`` — the single-source-
root layout under test.
"""

from util import helper


def compute(n: int) -> int:
    """The anchored callable under test (``src/core.py:compute``). Its interface
    fingerprint (combdrift) and dependency-neighborhood fingerprint (matrix
    subgraph fp, spanning ``helper``) are both minted for the parity goldens."""
    return helper(n) * 2
