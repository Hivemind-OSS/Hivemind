"""Signature-change fixture — AFTER: the drifted interface.

``transform`` gains a second required positional parameter. This is an
interface-changing edit (BUG-048's lesson: a body-only change is fingerprint-
invisible, so the parameter — not the body — is what moves the token), so a
memory carrying the BEFORE fingerprint verifies as ``stale/signature_changed``.
"""


def transform(data: str, loud: bool) -> str:
    """Two required positional parameters — the drifted interface shape."""
    return data.upper() if loud else data
