"""Signature-change fixture — BEFORE: the interface baseline.

The combdrift fingerprint minted from this file is the ``old_token`` a stored
memory would carry. Verifying that token against the AFTER tree must read
``stale/signature_changed`` with a fingerprint delta.
"""


def transform(data: str) -> str:
    """One required positional parameter — the baseline interface shape."""
    return data.upper()
