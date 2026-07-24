"""Contract tests for receipt schema v0 — the versioned public artifact.

The schema is the receipt's write-boundary contract: the builder refuses to
emit a document the published artifact rejects. These tests pin the artifact's
shape (draft 2020-12, version const, tag vocabulary, required keys) and the
enforcement surface around it, including that the artifact ships as package
data of the hive wheel and loads via package resources, never a cwd path.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest

from hive.census import SCHEMA_VERSION, load_schema, validate_receipt
from hive.census.schema import RECEIPT_PREDICATE_TYPE, ReceiptSchemaError

REPO_ROOT = Path(__file__).resolve().parents[2]

ALL_TAGS = (
    "machine-checked",
    "bounded-estimate",
    "unverified",
    "unverified-judgment",
    "not-run",
)


def minimal_receipt() -> dict:
    return {"schema_version": "v0", "provenance": {}, "lines": [], "precision": []}


def make_line(**overrides) -> dict:
    line = {
        "class": "node",
        "tag": "machine-checked",
        "subject": "pkg/mod.py::f",
        "detail": {"drift": "unchanged"},
    }
    line.update(overrides)
    return line


class TestSchemaArtifact:
    def test_meta_validates_against_draft_2020_12(self) -> None:
        jsonschema.Draft202012Validator.check_schema(load_schema())

    def test_version_agreement_between_module_and_artifact(self) -> None:
        schema = load_schema()
        assert schema["properties"]["schema_version"]["const"] == SCHEMA_VERSION
        assert schema["$id"] == RECEIPT_PREDICATE_TYPE
        assert RECEIPT_PREDICATE_TYPE == f"urn:hive-census:receipt:{SCHEMA_VERSION}"

    def test_tag_vocabulary_pinned(self) -> None:
        schema = load_schema()
        assert tuple(schema["$defs"]["line"]["properties"]["tag"]["enum"]) == ALL_TAGS


class TestValidateReceipt:
    def test_minimal_receipt_passes(self) -> None:
        validate_receipt(minimal_receipt())

    def test_current_schema_version_constant_accepted(self) -> None:
        doc = minimal_receipt()
        doc["schema_version"] = SCHEMA_VERSION
        validate_receipt(doc)

    def test_every_published_tag_accepted(self) -> None:
        for tag in ALL_TAGS:
            doc = minimal_receipt()
            doc["lines"] = [make_line(tag=tag)]
            validate_receipt(doc)

    def test_wrong_schema_version_rejected(self) -> None:
        doc = minimal_receipt()
        doc["schema_version"] = "v999"
        with pytest.raises(ReceiptSchemaError):
            validate_receipt(doc)

    def test_missing_top_level_key_rejected(self) -> None:
        for key in ("schema_version", "provenance", "lines", "precision"):
            doc = minimal_receipt()
            del doc[key]
            with pytest.raises(ReceiptSchemaError):
                validate_receipt(doc)

    def test_extra_top_level_key_rejected(self) -> None:
        doc = minimal_receipt()
        doc["extra"] = 1
        with pytest.raises(ReceiptSchemaError):
            validate_receipt(doc)

    def test_unknown_tag_rejected(self) -> None:
        doc = minimal_receipt()
        doc["lines"] = [make_line(tag="vibes")]
        with pytest.raises(ReceiptSchemaError):
            validate_receipt(doc)

    @pytest.mark.parametrize("field", ["class", "tag", "subject", "detail"])
    def test_line_missing_required_field_rejected(self, field: str) -> None:
        line = make_line()
        del line[field]
        doc = minimal_receipt()
        doc["lines"] = [line]
        with pytest.raises(ReceiptSchemaError):
            validate_receipt(doc)

    def test_non_object_receipt_rejected(self) -> None:
        with pytest.raises(ReceiptSchemaError):
            validate_receipt([])  # type: ignore[arg-type]


def make_context_entry(**overrides) -> dict:
    entry = {
        "tag": "institutional-memory",
        "episode_id": 41,
        "trust": "established",
        "polarity": "dont",
        "kind": "gotcha",
        "anchor": "pkg/lib.py::greet",
        "ts": 1751000000,
        "sim": 0.91,
        "text": "a lesson",
    }
    entry.update(overrides)
    return entry


class TestContextBlock:
    """The optional institutional-memory block is additive within v0: absent
    in every receipt that predates it, fully labelled when present, and its
    tag never enters the evidence-line vocabulary."""

    def test_wellformed_context_accepted(self) -> None:
        doc = minimal_receipt()
        doc["context"] = [make_context_entry()]
        validate_receipt(doc)

    @pytest.mark.parametrize(
        "field",
        [
            "tag",
            "episode_id",
            "trust",
            "polarity",
            "kind",
            "anchor",
            "ts",
            "sim",
            "text",
        ],
    )
    def test_context_entry_missing_field_rejected(self, field: str) -> None:
        entry = make_context_entry()
        del entry[field]
        doc = minimal_receipt()
        doc["context"] = [entry]
        with pytest.raises(ReceiptSchemaError):
            validate_receipt(doc)

    def test_context_entry_foreign_tag_rejected(self) -> None:
        doc = minimal_receipt()
        doc["context"] = [make_context_entry(tag="machine-checked")]
        with pytest.raises(ReceiptSchemaError):
            validate_receipt(doc)

    def test_non_array_context_rejected(self) -> None:
        doc = minimal_receipt()
        doc["context"] = make_context_entry()
        with pytest.raises(ReceiptSchemaError):
            validate_receipt(doc)

    def test_context_tag_never_enters_the_line_vocabulary(self) -> None:
        doc = minimal_receipt()
        doc["lines"] = [make_line(tag="institutional-memory")]
        with pytest.raises(ReceiptSchemaError):
            validate_receipt(doc)


def make_propagation_entry(**overrides) -> dict:
    entry = {
        "seed": "pkg/lib.py::greet",
        "drift": "breaking",
        "depth": 2,
        "neighbors": ["pkg/app.py::caller"],
    }
    entry.update(overrides)
    return entry


class TestPropagationBlock:
    """The optional propagation block is additive within v0: absent in every
    receipt that predates it, seed/drift/depth/neighbors when present, and the
    drift vocabulary is pinned to the two drifts that justify suspicion."""

    def test_wellformed_propagation_accepted(self) -> None:
        doc = minimal_receipt()
        doc["propagation"] = [
            make_propagation_entry(),
            make_propagation_entry(drift="removed"),
        ]
        validate_receipt(doc)

    @pytest.mark.parametrize("field", ["seed", "drift", "depth", "neighbors"])
    def test_propagation_entry_missing_field_rejected(self, field: str) -> None:
        entry = make_propagation_entry()
        del entry[field]
        doc = minimal_receipt()
        doc["propagation"] = [entry]
        with pytest.raises(ReceiptSchemaError):
            validate_receipt(doc)

    def test_propagation_benign_drift_rejected(self) -> None:
        doc = minimal_receipt()
        doc["propagation"] = [make_propagation_entry(drift="additive")]
        with pytest.raises(ReceiptSchemaError):
            validate_receipt(doc)

    def test_non_array_propagation_rejected(self) -> None:
        doc = minimal_receipt()
        doc["propagation"] = make_propagation_entry()
        with pytest.raises(ReceiptSchemaError):
            validate_receipt(doc)


class TestPackagedSchema:
    """The census now ships inside the `hive` wheel: the schema artifact must be
    declared as setuptools package-data (a bare py-module build drops the .json)
    and must load through the package's resources, never a cwd-relative path."""

    def test_pyproject_declares_schema_as_package_data(self) -> None:
        import tomllib

        pyproject = REPO_ROOT / "pyproject.toml"
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        package_data = (
            data.get("tool", {}).get("setuptools", {}).get("package-data", {})
        )
        assert "receipt.v0.schema.json" in package_data.get("hive.census", ()), (
            "the receipt schema artifact must ship as hive.census package data "
            "or the built wheel loses it"
        )

    def test_schema_loads_from_package_resources_with_foreign_cwd(
        self, tmp_path: Path
    ) -> None:
        probe = (
            "import hive.census\n"
            "assert hive.census.SCHEMA_VERSION == 'v0'\n"
            "schema = hive.census.load_schema()\n"
            "assert schema['properties']['schema_version']['const'] == 'v0'\n"
            "hive.census.validate_receipt(\n"
            "    {'schema_version': 'v0', 'provenance': {}, 'lines': [], 'precision': []}\n"
            ")\n"
            "print('package-data-ok')\n"
        )
        run = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert run.returncode == 0, (
            f"schema resource probe failed:\n{run.stdout}\n{run.stderr}"
        )
        assert "package-data-ok" in run.stdout
