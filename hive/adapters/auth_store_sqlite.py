"""SqliteTokenStore — per-device bearer-token identity, hashed at rest.

The whole token lifecycle in one cohesive adapter: it
owns its ``access_tokens`` table via ``executescript(_SCHEMA)`` on the shared WAL conn. Only
``sha256(token)`` is stored — the 256-bit plaintext is returned ONCE at ``create()`` and
never persisted, so a DB leak yields no usable token. ``verify`` maps a presented plaintext
to its device label (the authenticated ``proposed_by``); ``revoke`` is a row DELETE (a
revoked token simply stops verifying — no soft-delete state to reason about).

NOT a domain port: the pure domain (recall/admission) never touches tokens, so this stays
out of ``domain/ports.py``. It is an app-layer adapter used only by the transport (through a
``verify`` callable) and the ``authctl`` CLI. ``create()``/``revoke()`` run on the prod conn
(``isolation_level=None``) where a bare INSERT/DELETE autocommits — they do NOT join the
producer tick's ``BEGIN IMMEDIATE`` lane.
"""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
from typing import Optional

# A recognizable, greppable marker on the plaintext (NOT the secret — the entropy is the 256
# bits after it). Lets an operator eyeball a "hive_…" value as one of ours.
TOKEN_PREFIX = "hive_"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS access_tokens(
  label      TEXT PRIMARY KEY,            -- device identity → proposed_by (e.g. "alice-laptop")
  token_hash TEXT NOT NULL UNIQUE);       -- sha256(plaintext) hex; UNIQUE indexes the verify lookup
"""


def new_token() -> str:
    """A fresh 256-bit bearer token: ``TOKEN_PREFIX`` + 32 CSPRNG bytes as hex (64 chars).
    ``secrets.token_hex`` is cryptographically secure — never ``random``. // O(1)."""
    return TOKEN_PREFIX + secrets.token_hex(32)


def token_hash(plaintext: str) -> str:
    """The at-rest representation: lowercase-hex sha256 of the UTF-8 plaintext. // O(len)."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


class SqliteTokenStore:
    """Per-device token lifecycle on the shared WAL connection."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        conn.executescript(_SCHEMA)

    def create(self, label: str) -> str:
        """Mint a token for ``label``, store ONLY its hash, and return the plaintext ONCE
        (the caller shows it to the human, then discards it). Raises ``sqlite3.IntegrityError``
        on a duplicate label (the PK) — a device label is unique by construction. // O(1)."""
        plaintext = new_token()
        self.conn.execute(
            "INSERT INTO access_tokens(label, token_hash) VALUES(?, ?)",
            (label, token_hash(plaintext)))
        return plaintext

    def verify(self, plaintext: str) -> Optional[str]:
        """Map a presented plaintext to its device label, or ``None`` to REJECT (unknown OR
        revoked). The lookup is BY ``token_hash`` — dropping that predicate (returning the
        first row regardless) is the mutation the unknown/revoked tests catch.
        // O(1) on the UNIQUE index."""
        row = self.conn.execute(
            "SELECT label FROM access_tokens WHERE token_hash=?",
            (token_hash(plaintext),)).fetchone()
        return None if row is None else row["label"]

    def revoke(self, label: str) -> bool:
        """Delete the device's token. Returns True iff a row was removed (False ⇒ no such
        label). The device's next ``verify()`` then returns None. // O(1)."""
        cur = self.conn.execute("DELETE FROM access_tokens WHERE label=?", (label,))
        return cur.rowcount > 0

    def labels(self) -> list[str]:
        """All provisioned device labels, sorted — the operator's seat inventory. Labels
        only, never hashes: safe to surface in status output. // O(n)."""
        rows = self.conn.execute(
            "SELECT label FROM access_tokens ORDER BY label").fetchall()
        return [row["label"] for row in rows]
