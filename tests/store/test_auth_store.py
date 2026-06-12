"""Chunk 2 — SqliteTokenStore: per-device bearer tokens, hashed at rest.

create() mints a 256-bit plaintext shown ONCE and stores only sha256(plaintext); verify()
maps a presented plaintext back to its device label (None ⇒ reject); revoke() deletes the
row. The conn is built through the PROD connect() (isolation_level=None) so a bare INSERT/
DELETE autocommits exactly as in production. The mutation (verify drops the
``WHERE token_hash=?`` predicate) makes the unknown/revoked-token tests red.
"""
from __future__ import annotations

import hashlib
import sqlite3

import pytest

from hive.adapters.auth_store_sqlite import (
    TOKEN_PREFIX, SqliteTokenStore, new_token, token_hash,
)
from hive.adapters.sqlite_db import connect


@pytest.fixture()
def store():
    return SqliteTokenStore(connect(":memory:"))


def test_create_then_verify_returns_label(store):
    tok = store.create("alice-laptop")
    assert store.verify(tok) == "alice-laptop"


def test_created_token_has_prefix_and_is_256_bit(store):
    tok = store.create("alice-laptop")
    assert tok.startswith(TOKEN_PREFIX)
    body = tok[len(TOKEN_PREFIX):]
    assert len(body) == 64               # token_hex(32) → 32 bytes → 64 hex chars (256-bit)
    int(body, 16)                        # body is valid hex (raises otherwise)


def test_stored_row_holds_only_the_hash_never_plaintext(store):
    tok = store.create("alice-laptop")
    rows = list(store.conn.execute("SELECT label, token_hash FROM access_tokens"))
    assert len(rows) == 1
    assert rows[0]["label"] == "alice-laptop"
    assert rows[0]["token_hash"] == token_hash(tok)   # the hash, not the token
    # exhaustive: the plaintext appears NOWHERE in the row (a DB leak yields no usable token)
    dump = "".join(str(v) for r in store.conn.execute("SELECT * FROM access_tokens")
                   for v in tuple(r))
    assert tok not in dump


def test_revoke_then_verify_returns_none(store):
    tok = store.create("alice-laptop")
    assert store.verify(tok) == "alice-laptop"
    assert store.revoke("alice-laptop") is True
    assert store.verify(tok) is None                  # revoked ⇒ simply stops verifying


def test_revoke_unknown_label_returns_false(store):
    assert store.revoke("nobody") is False


def test_unknown_or_empty_token_returns_none(store):
    store.create("alice-laptop")
    assert store.verify("hive_deadbeefdeadbeef") is None
    assert store.verify("") is None


def test_duplicate_label_rejected(store):
    store.create("alice-laptop")
    with pytest.raises(sqlite3.IntegrityError):       # label is the PRIMARY KEY
        store.create("alice-laptop")


def test_two_labels_verify_independently(store):
    a, b = store.create("alice"), store.create("bob")
    assert a != b
    assert store.verify(a) == "alice" and store.verify(b) == "bob"
    store.revoke("alice")
    assert store.verify(a) is None and store.verify(b) == "bob"   # revoking one spares the other


def test_labels_returns_sorted(store):
    assert store.labels() == []                       # empty store → empty list, not None
    store.create("bob")
    store.create("alice")
    store.create("carol")
    assert store.labels() == ["alice", "bob", "carol"]   # sorted, not insertion order
    store.revoke("bob")
    assert store.labels() == ["alice", "carol"]           # reflects revocation


def test_helpers_new_token_unique_and_token_hash_is_sha256():
    assert new_token() != new_token()                 # CSPRNG — distinct each call
    assert new_token().startswith(TOKEN_PREFIX)
    assert token_hash("x") == hashlib.sha256(b"x").hexdigest()
