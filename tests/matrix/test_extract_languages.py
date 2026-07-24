"""Per-language extraction tests: the golden pin.

For each of the eight supported languages, ``test_golden`` asserts the normalized
extraction equals a committed golden snapshot — the durable specification of
correct output — and ``test_extraction_nonempty`` guards against a silent
grammar-load failure degrading to an empty graph. Regenerate the goldens with
``tools/regen_golden.py`` after an intentional extraction change.
"""

from __future__ import annotations

import json

import pytest

from tests.matrix.fixtures_manifest import GOLDEN_ROOT, LANG_FILES, lang_paths
from tests.matrix.normalize import normalize_for_golden

import hive.matrix as matrix

LANGS = sorted(LANG_FILES)


@pytest.mark.parametrize("lang", LANGS)
def test_golden(lang):
    golden_file = GOLDEN_ROOT / f"{lang}.json"
    if not golden_file.exists():
        pytest.skip(f"golden for {lang} not generated yet")
    expected = json.loads(golden_file.read_text())
    actual = normalize_for_golden(matrix.extract(lang_paths(lang)))
    assert actual == expected, f"{lang} drifted from its golden snapshot"


@pytest.mark.parametrize("lang", LANGS)
def test_extraction_nonempty(lang):
    """Every fixture must produce at least one node and one edge — guards against
    a silent grammar-load failure degrading to an empty graph that would still
    'match' an empty golden."""
    result = matrix.extract(lang_paths(lang))
    assert result["nodes"], f"{lang} produced no nodes"
    assert result["edges"], f"{lang} produced no edges"
