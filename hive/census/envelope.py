"""Unsigned DSSE-shaped envelope over an in-toto Statement v1.

The receipt travels as the predicate of a directly-constructed in-toto Statement
v1, wrapped in a DSSE-shaped envelope (base64 payload + payloadType) with an empty
signature list and a top-level ``"unsigned": true`` marker. One serialization
(`canonical_bytes`) owns every digest and payload byte, so the subject digest and
the ingest-door recomputation can only move together.

Receipts are emitted unsigned by policy: the ingest-door trust boundary is
transport/auth (loopback locally, per-seat tokens over a tunnel), not a receipt
signature. No key is loaded or required anywhere; the wire dict is built with the
standard library alone. Tamper detection is the subject digest's job — it covers
the canonical predicate bytes — and the ingest door (kernel-side) re-checks it.
"""

from __future__ import annotations

import base64
import hashlib
import json

from hive.census.schema import RECEIPT_PREDICATE_TYPE

PAYLOAD_TYPE = "application/vnd.in-toto+json"
STATEMENT_TYPE = "https://in-toto.io/Statement/v1"
SUBJECT_NAME = "hive-census-receipt"


def canonical_bytes(doc: dict) -> bytes:
    """The one serialization every digest and payload is computed over."""
    return json.dumps(
        doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def receipt_statement(receipt: dict) -> dict:
    """Wrap a receipt as the predicate of an in-toto Statement v1."""
    digest = hashlib.sha256(canonical_bytes(receipt)).hexdigest()
    return {
        "_type": STATEMENT_TYPE,
        "subject": [{"name": SUBJECT_NAME, "digest": {"sha256": digest}}],
        "predicateType": RECEIPT_PREDICATE_TYPE,
        "predicate": receipt,
    }


def unsigned_envelope(receipt: dict) -> dict:
    """The receipt's statement in a DSSE-shaped envelope, explicitly unsigned.

    The wire dict is ``{"payload", "payloadType", "signatures", "unsigned"}`` where
    ``payload`` is standard-base64 over ``canonical_bytes(receipt_statement(receipt))``
    — byte-identical to what a DSSE library emits for an unsigned envelope, built with
    stdlib alone. The empty signature list and ``"unsigned": true`` state the absence
    of a signature as a fact, never mistakable for a dropped one.
    """
    payload = base64.standard_b64encode(
        canonical_bytes(receipt_statement(receipt))
    ).decode("ascii")
    return {
        "payload": payload,
        "payloadType": PAYLOAD_TYPE,
        "signatures": [],
        "unsigned": True,
    }
