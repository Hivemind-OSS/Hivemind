"""Receipt schema surface: the versioned public artifact and its enforcement.

The JSON Schema file shipped as package data is the single source of truth for
the receipt shape; the builder validates every receipt against it at the write
boundary, so an off-schema document is refused rather than emitted.
"""

from __future__ import annotations

import json
from functools import cache
from importlib import resources
from typing import Any

import jsonschema

SCHEMA_VERSION: str = "v0"
RECEIPT_PREDICATE_TYPE: str = "urn:hive-census:receipt:v0"

# The artifact's filename is derived from the version so the constant, the
# file, and the schema's own const/$id can only move together.
_SCHEMA_RESOURCE = f"receipt.{SCHEMA_VERSION}.schema.json"


class ReceiptSchemaError(Exception):
    """A receipt does not conform to the published receipt schema."""


@cache
def load_schema() -> dict[str, Any]:
    """Load the published receipt schema from the installed package data."""
    text = (
        resources.files("hive.census")
        .joinpath(_SCHEMA_RESOURCE)
        .read_text(encoding="utf-8")
    )
    schema: dict[str, Any] = json.loads(text)
    return schema


def validate_receipt(doc: dict[str, Any]) -> None:
    """Validate a receipt against schema v0; raise ReceiptSchemaError if off-schema."""
    try:
        jsonschema.validate(
            instance=doc,
            schema=load_schema(),
            cls=jsonschema.Draft202012Validator,
        )
    except jsonschema.ValidationError as exc:
        raise ReceiptSchemaError(
            f"receipt does not conform to schema {SCHEMA_VERSION}: {exc.message}"
        ) from exc
