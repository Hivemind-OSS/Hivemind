"""Single-source-root fixture: the callee helper module.

All source in this fixture lives under one top-level dir (``src/``), so matrix
infers its extraction root as ``src`` and its node ``source_file`` dialect drops
that prefix (``src/core.py`` -> ``core.py``) while a Hivemind anchor stays
repo-root-relative — the BUG-035/038 offset-bridge corner the subgraph fp must
survive byte-identically across the engine move.
"""


def helper(value: int) -> int:
    """Add one — the callee whose caller (``compute``) sits in its dependency
    neighborhood, so the subgraph fingerprint spans more than one member."""
    return value + 1
