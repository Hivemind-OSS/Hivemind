"""Fail-fast access to required secrets, per the secrets-via-environment rule.

The MVP core needs no secret; this is the documented seam a consumer integration
uses to declare one. An unset or empty value is a hard startup failure — never a
silent default.
"""

from __future__ import annotations

import os


def require_env(name: str) -> str:
    """Return os.environ[name]; raise RuntimeError if it is unset or empty."""
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"missing required env var: {name}")
    return value
