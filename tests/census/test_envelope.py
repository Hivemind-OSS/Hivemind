"""Contract tests for the unsigned DSSE-shaped envelope.

Receipts are emitted unsigned by policy — there is no key, no signature, and no
verify path in this module. What is pinned here: the canonical serialization, the
in-toto statement shape, the exact unsigned wire dict, and the tamper-detection
PROPERTY the ingest door (kernel-side) relies on — the subject digest covers the
canonical predicate bytes, so any predicate mutation changes the digest.
"""

from __future__ import annotations

import base64
import hashlib
import json

from hive.census.envelope import (
    PAYLOAD_TYPE,
    STATEMENT_TYPE,
    SUBJECT_NAME,
    canonical_bytes,
    receipt_statement,
    unsigned_envelope,
)
from hive.census.schema import RECEIPT_PREDICATE_TYPE

# Any dict is emittable — the envelope layer transports receipts, it does not own
# their schema. Unicode content pins the ensure_ascii=False canonical form.
_RECEIPT = {
    "schema_version": "v0",
    "provenance": {"base_sha": "a" * 40, "head_sha": "b" * 40},
    "lines": [{"class": "contract", "tag": "machine-checked", "subject": "naïve-Ω"}],
    "precision": [],
}


def _decode_statement(envelope_doc: dict) -> dict:
    return json.loads(base64.b64decode(envelope_doc["payload"]))


class TestCanonicalBytes:
    def test_key_insertion_order_is_immaterial(self) -> None:
        one = {"b": 1, "a": {"y": 2, "x": [1, 2]}}
        two = {"a": {"x": [1, 2], "y": 2}, "b": 1}
        assert canonical_bytes(one) == canonical_bytes(two)

    def test_compact_utf8_form(self) -> None:
        assert canonical_bytes({"k": "naïve"}) == '{"k":"naïve"}'.encode()


class TestStatement:
    def test_shape_and_digest(self) -> None:
        statement = receipt_statement(_RECEIPT)
        assert statement["_type"] == STATEMENT_TYPE
        assert statement["predicateType"] == RECEIPT_PREDICATE_TYPE
        assert statement["predicate"] == _RECEIPT
        (subject,) = statement["subject"]
        assert subject["name"] == SUBJECT_NAME
        expected = hashlib.sha256(canonical_bytes(_RECEIPT)).hexdigest()
        assert subject["digest"] == {"sha256": expected}

    def test_subject_digest_covers_the_predicate(self) -> None:
        # The property the ingest-door tamper re-check (kernel-side) relies on: the
        # subject digest is sha256 over the canonical predicate bytes, so ANY change
        # to the predicate necessarily changes the digest — tamper is detectable.
        base = receipt_statement(_RECEIPT)["subject"][0]["digest"]["sha256"]
        mutated = receipt_statement({**_RECEIPT, "schema_version": "v-tampered"})[
            "subject"
        ][0]["digest"]["sha256"]
        assert base != mutated


class TestUnsignedEnvelope:
    def test_wire_shape_is_unsigned_dsse(self) -> None:
        envelope_doc = unsigned_envelope(_RECEIPT)
        assert envelope_doc["payloadType"] == PAYLOAD_TYPE
        assert envelope_doc["signatures"] == []
        assert envelope_doc["unsigned"] is True
        assert _decode_statement(envelope_doc)["predicate"] == _RECEIPT

    def test_payload_is_standard_b64_of_canonical_statement(self) -> None:
        # Byte-exact wire contract: payload = standard-base64(canonical_bytes(statement)),
        # and the dict carries exactly these four keys — no dropped or extra field. The
        # probe forces bytes whose base64 diverges between the standard and urlsafe
        # alphabets (a self-check pins that below), so a wrong-flavor slip is caught,
        # not masked by an input that happens to encode identically — the ingest door
        # decodes with the STANDARD alphabet, so census must emit it.
        receipt = {**_RECEIPT, "flavor_probe": "ÿ" * 24}
        statement_bytes = canonical_bytes(receipt_statement(receipt))
        standard = base64.standard_b64encode(statement_bytes).decode("ascii")
        urlsafe = base64.urlsafe_b64encode(statement_bytes).decode("ascii")
        assert standard != urlsafe, "probe must make the two alphabets diverge"
        envelope_doc = unsigned_envelope(receipt)
        assert envelope_doc["payload"] == standard
        assert set(envelope_doc) == {"payload", "payloadType", "signatures", "unsigned"}
