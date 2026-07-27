"""The meta-key registry — the ONE catalog of every episode-meta tag key (the U2 envelope law).

Governance artifact, NEVER a runtime gate: the server allowlists nothing, readers ignore
unknown keys, no key is required. Each row is a DELIBERATE independent copy of facts the
owning code states (version constants, token prefixes) so the suite can detect drift —
the cross-copy-tripwire idiom (BUG-034's denylist pin, the keystone golden). A new meta
key MUST land with its row in the same change; `tests/domain/test_meta_registry.py` reds
a minted-but-unregistered key, a registry/code version disagreement, and a shrunk
known-version set.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_KEY_RE = re.compile(
    r"^[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._-]*$"
)  # the server's meta grammar
_COMPARES = frozenset({"directional", "equality", "advisory"})
_TIERS = frozenset({"structural", "opaque-hash"})


@dataclass(frozen=True, slots=True)
class MetaKeySpec:
    """One registered episode-meta key. Frozen + validated: an illegal row is unconstructable."""

    key: str  # episode-meta key, e.g. "combdrift/fp"
    token_prefix: str  # the value's "<engine>-<kind>/" prefix
    current_version: str  # what mint emits today (mirror of the code constant)
    known_versions: tuple[str, ...]  # grows, never shrinks — every version ever minted
    # The minting owner, "path/file.py:symbol" — or "retired: <what it was>" for a key
    # whose minter is gone. A row outlives its minter by design (the namespace is
    # add-only, §5 clause 1/6), so this field must be able to say so rather than keep
    # naming a symbol that no longer exists.
    owner: str
    compare: str  # "directional" | "equality" | "advisory"
    tier: (
        str  # "structural" (full historical verify) | "opaque-hash" (recognize+silence)
    )
    on_unreadable: str  # failure routing, prose (e.g. "incomparable -> omit radius")

    def __post_init__(self) -> None:
        if _KEY_RE.match(self.key) is None:
            raise ValueError(f"registry key {self.key!r} violates the meta grammar")
        if not self.token_prefix.endswith("/"):
            raise ValueError(f"token_prefix {self.token_prefix!r} must end with '/'")
        if self.current_version not in self.known_versions:
            raise ValueError(
                f"current_version {self.current_version!r} not in known_versions"
            )
        if self.compare not in _COMPARES:
            raise ValueError(f"compare {self.compare!r} not in {sorted(_COMPARES)}")
        if self.tier not in _TIERS:
            raise ValueError(f"tier {self.tier!r} not in {sorted(_TIERS)}")


REGISTRY: tuple[MetaKeySpec, ...] = (
    MetaKeySpec(
        key="combdrift/fp",
        token_prefix="combdrift-fp/",
        current_version="1",
        known_versions=("1",),
        owner="hive/combdrift/fingerprint.py:render",
        compare="directional",
        tier="structural",
        on_unreadable="incomparable -> verdict unverifiable, never mismatch",
    ),
    MetaKeySpec(
        key="matrix/subgraph_fp",
        token_prefix="matrix-subgraph-fp/",
        current_version="1",
        known_versions=("1",),
        owner="retired: hive/edge/cli.py:_subgraph_fp_core",
        compare="equality",
        tier="opaque-hash",
        on_unreadable="incomparable -> omit radius (silence)",
    ),
    MetaKeySpec(
        key="git/branches",
        token_prefix="git-branches/",
        current_version="1",
        known_versions=("1",),
        owner="retired: hive/edge/cli.py:_branches_token",
        compare="advisory",
        tier="structural",
        on_unreadable="incomparable -> tag ignored (today's semantics), never a false off-branch",
    ),
    # Server-owned row, now RETIRED with the mint backfill that wrote it: the stamp
    # marked fingerprint keys the sync leg filled from the mirror, and the git-native
    # staleness ladder has no fingerprints to fill. The row stays because the namespace
    # is add-only and stored stamps must keep reading the same way forever.
    MetaKeySpec(
        key="hive-sync/minted",
        token_prefix="hive-sync-minted/",
        current_version="1",
        known_versions=("1",),
        owner="retired: hive/app/sync.py:_backfill",
        compare="advisory",
        tier="structural",
        on_unreadable="provenance only — never affects any verdict",
    ),
)

KEYS: frozenset[str] = frozenset(spec.key for spec in REGISTRY)
